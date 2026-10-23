"""Measurable, non-semantic signals for photos and video.

Scores describe technical quality and visual activity only; they say nothing about
what a shot means to the family, so they rank shots and never delete them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import ffmpeg
from .probe import MediaInfo
from .proxy import asset_dir

ANALYSIS_VERSION = 1
SAMPLE_FPS = 4
FRAME_SIZE = (144, 256)  # (w, h) for portrait; swapped for landscape


@dataclass
class FrameStats:
    sharpness: float  # 0..1
    exposure: float  # 0..1, 1 = well exposed
    interest: float  # 0..1, colorfulness + detail


@dataclass
class Analysis:
    version: int
    kind: str
    phash: int
    sharpness: float
    exposure: float
    interest: float
    # Video only: per-sample series at SAMPLE_FPS.
    sample_fps: float = 0.0
    sharpness_series: list[float] = field(default_factory=list)
    exposure_series: list[float] = field(default_factory=list)
    motion_series: list[float] = field(default_factory=list)
    scene_cuts: list[float] = field(default_factory=list)  # seconds

    @property
    def quality(self) -> float:
        return 0.6 * self.sharpness + 0.4 * self.exposure

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def analyze(info: MediaInfo, proxy: Path, cache: Path) -> Analysis:
    out = asset_dir(cache, info) / "analysis.json"
    if out.exists():
        data = json.loads(out.read_text())
        if data.get("version") == ANALYSIS_VERSION:
            return Analysis(**data)
    result = _analyze_photo(proxy) if info.kind == "photo" else _analyze_video(info, proxy)
    out.write_text(result.to_json())
    return result


# --- per-frame metrics ----------------------------------------------------------

def frame_stats(bgr_or_gray: np.ndarray) -> FrameStats:
    gray = bgr_or_gray if bgr_or_gray.ndim == 2 else cv2.cvtColor(bgr_or_gray, cv2.COLOR_BGR2GRAY)
    # Variance of Laplacian on a fixed-size frame; ~300+ is crisp at this size.
    lap = cv2.Laplacian(gray, cv2.CV_64F).var()
    sharpness = float(np.clip(np.log1p(lap) / np.log1p(600.0), 0, 1))

    mean = gray.mean() / 255.0
    clipped = float(((gray < 8) | (gray > 247)).mean())
    exposure = float(np.clip(1.0 - abs(mean - 0.48) * 1.8 - clipped * 1.5, 0, 1))

    interest = _detail(gray)
    if bgr_or_gray.ndim == 3:
        interest = 0.5 * interest + 0.5 * _colorfulness(bgr_or_gray)
    return FrameStats(sharpness, exposure, float(interest))


def _colorfulness(bgr: np.ndarray) -> float:
    """Hasler & Suesstrunk colorfulness, scaled so ~100 maps to 1."""
    b, g, r = (bgr[..., i].astype(np.float32) for i in range(3))
    rg = r - g
    yb = 0.5 * (r + g) - b
    c = np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean())
    return float(np.clip(c / 100.0, 0, 1))


def _detail(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 80, 160)
    return float(np.clip(edges.mean() / 255.0 * 6.0, 0, 1))


def phash(gray: np.ndarray) -> int:
    """64-bit perceptual hash (DCT of a 32x32 thumbnail)."""
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    dct = cv2.dct(small)[:8, :8].flatten()
    med = np.median(dct[1:])
    bits = dct > med
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


# --- photos -------------------------------------------------------------------

def _analyze_photo(proxy: Path) -> Analysis:
    img = cv2.imread(str(proxy), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot read {proxy}")
    h, w = img.shape[:2]
    size = FRAME_SIZE if h >= w else FRAME_SIZE[::-1]
    # Analyze at a larger size than video samples: photos are shown longer.
    small = cv2.resize(img, (size[0] * 2, size[1] * 2), interpolation=cv2.INTER_AREA)
    stats = frame_stats(small)
    return Analysis(
        version=ANALYSIS_VERSION, kind="photo",
        phash=phash(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)),
        sharpness=stats.sharpness, exposure=stats.exposure, interest=stats.interest,
    )


# --- videos -------------------------------------------------------------------

def read_frames(proxy: Path, landscape: bool, fps: float = SAMPLE_FPS) -> np.ndarray:
    w, h = FRAME_SIZE[::-1] if landscape else FRAME_SIZE
    raw = ffmpeg.read_raw([
        *ffmpeg.inp(proxy), "-an",
        "-vf", f"fps={fps},scale={w}:{h}:flags=area",
        "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
    ])
    n = len(raw) // (w * h * 3)
    return np.frombuffer(raw[: n * w * h * 3], np.uint8).reshape(n, h, w, 3)


def _analyze_video(info: MediaInfo, proxy: Path) -> Analysis:
    frames = read_frames(proxy, info.is_landscape)
    if len(frames) == 0:
        raise ValueError(f"no frames decoded from {Path(info.path).name}")
    stats = [frame_stats(f) for f in frames]
    grays = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in frames]

    motion = [0.0]
    cuts: list[float] = []
    prev_hist = _hist(frames[0])
    for i in range(1, len(frames)):
        diff = cv2.absdiff(grays[i], grays[i - 1]).mean() / 255.0
        motion.append(float(np.clip(diff * 8.0, 0, 1)))
        hist = _hist(frames[i])
        # Bhattacharyya distance between colour histograms; spikes mark hard scene cuts.
        if cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA) > 0.45:
            cuts.append(round(i / SAMPLE_FPS, 3))
        prev_hist = hist

    mid = grays[len(grays) // 2]
    return Analysis(
        version=ANALYSIS_VERSION, kind="video", phash=phash(mid),
        sharpness=float(np.mean([s.sharpness for s in stats])),
        exposure=float(np.mean([s.exposure for s in stats])),
        interest=float(np.mean([s.interest for s in stats])),
        sample_fps=SAMPLE_FPS,
        sharpness_series=[round(s.sharpness, 4) for s in stats],
        exposure_series=[round(s.exposure, 4) for s in stats],
        motion_series=[round(m, 4) for m in motion],
        scene_cuts=cuts,
    )


def _hist(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
    return cv2.normalize(h, h).flatten()


def best_window(a: Analysis, duration: float, length: float,
                start_min: float = 0.0, end_max: float | None = None) -> tuple[float, float, float]:
    """Best [start, start+length) inside [start_min, end_max] of a video.

    Returns (start_seconds, score, peak_motion_offset). Score favours sharp, well-exposed,
    moving footage and penalizes windows that straddle a scene cut or shake hard.
    """
    end_max = duration if end_max is None else min(end_max, duration)
    span = end_max - start_min
    if length > span + 1e-6:
        return start_min, -1.0, 0.0
    fps = a.sample_fps or SAMPLE_FPS
    n = len(a.sharpness_series)
    win = max(1, int(round(length * fps)))
    best = (start_min, -1.0, 0.0)
    step = 1.0 / fps
    t = start_min
    # Keep 0.15 s clear of the very end: last frames of phone clips are often a fumble.
    while t + length <= end_max - min(0.15, max(0.0, span - length)) + 1e-6:
        i0 = min(n - 1, int(round(t * fps)))
        i1 = min(n, i0 + win)
        sh = float(np.mean(a.sharpness_series[i0:i1]))
        ex = float(np.mean(a.exposure_series[i0:i1]))
        mo_slice = a.motion_series[i0:i1] or [0.0]
        mo = float(np.mean(mo_slice))
        shake = float(np.std(mo_slice))
        crosses_cut = any(t + 0.05 < c < t + length - 0.05 for c in a.scene_cuts)
        score = 0.45 * sh + 0.25 * ex + 0.30 * min(1.0, mo * 1.5) - 0.3 * shake
        if crosses_cut:
            score -= 0.5
        if score > best[1] + 1e-9:
            peak = (int(np.argmax(mo_slice)) / fps) if mo_slice else 0.0
            best = (round(t, 3), score, round(peak, 3))
        t += step
    return best
