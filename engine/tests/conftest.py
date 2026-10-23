"""Shared fixtures: synthetic analyses for planner tests and generated media for
end-to-end tests (so the suite needs no real family photos)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image, ImageDraw, ImageFilter

from reel_engine import ffmpeg
from reel_engine.analysis import ANALYSIS_VERSION, Analysis
from reel_engine.planner import Asset
from reel_engine.probe import MediaInfo

SR = 22050


# --- synthetic music --------------------------------------------------------------

def make_song(seconds: float = 30.0, bpm: float = 120.0, drop_at: float = 10.0,
              sr: int = SR) -> np.ndarray:
    """Kick on every beat, hats on off-beats; quiet intro, loud 'drop' with bass."""
    n = int(seconds * sr)
    y = np.zeros(n, np.float32)
    beat = 60.0 / bpm
    t_kick = np.arange(int(0.25 * sr)) / sr
    kick = np.sin(2 * np.pi * 55 * t_kick * (1 + 2 * np.exp(-t_kick * 30))) * np.exp(-t_kick * 12)
    rng = np.random.default_rng(0)
    hat = rng.normal(0, 1, int(0.05 * sr)) * np.exp(-np.arange(int(0.05 * sr)) / sr * 80)
    t = 0.0
    i = 0
    while t < seconds - 0.3:
        loud = 1.0 if t >= drop_at else 0.25
        s = int(t * sr)
        accent = 1.3 if i % 4 == 0 else 1.0
        y[s:s + len(kick)] += (kick * loud * accent)[: n - s]
        h = int((t + beat / 2) * sr)
        if h + len(hat) < n:
            y[h:h + len(hat)] += hat * 0.04 * loud
        t += beat
        i += 1
    tt = np.arange(n) / sr
    bass = 0.25 * np.sin(2 * np.pi * 82.4 * tt) * (tt >= drop_at)
    y += bass.astype(np.float32)
    return (y / np.max(np.abs(y)) * 0.8).astype(np.float32)


# --- synthetic analyses (no ffmpeg needed) -----------------------------------------

def fake_photo(idx: int, phash: int, quality: float = 0.7, interest: float = 0.5,
               when: str | None = None) -> Asset:
    info = MediaInfo(path=f"photo{idx}.jpg", content_hash=f"{idx:064x}", kind="photo",
                     width=1080, height=1440, capture_time=when,
                     capture_time_reliable=when is not None, upload_index=idx)
    an = Analysis(ANALYSIS_VERSION, "photo", phash, quality, quality, interest)
    return Asset(f"photo_{idx:03d}", info, an, f"proxy{idx}.jpg")


def fake_video(idx: int, duration: float, phash: int, motion: float = 0.4,
               when: str | None = None) -> Asset:
    n = int(duration * 4)
    info = MediaInfo(path=f"video{idx}.mp4", content_hash=f"{idx + 500:064x}", kind="video",
                     width=1080, height=1920, duration=duration, fps=30,
                     capture_time=when, capture_time_reliable=when is not None,
                     upload_index=idx)
    an = Analysis(ANALYSIS_VERSION, "video", phash, 0.7, 0.7, 0.5, sample_fps=4,
                  sharpness_series=[0.7] * n, exposure_series=[0.7] * n,
                  motion_series=[motion] * n, scene_cuts=[])
    return Asset(f"video_{idx:03d}", info, an, f"proxy{idx}.mp4")


def distinct_hash(i: int) -> int:
    rng = np.random.default_rng(1000 + i)
    return int(rng.integers(0, 2**63 - 1))


@pytest.fixture
def mixed_assets() -> list[Asset]:
    assets = [fake_photo(i, distinct_hash(i), quality=0.5 + 0.04 * i,
                         when=f"2026-08-0{1 + i // 4}T1{i % 10}:00:00+00:00") for i in range(8)]
    assets += [fake_video(8 + i, 6.0, distinct_hash(50 + i), motion=0.3 + 0.1 * i,
                          when=f"2026-08-02T0{i}:30:00+00:00") for i in range(3)]
    return assets


# --- generated media files (needs ffmpeg) ------------------------------------------

def _ff(*args: str) -> None:
    subprocess.run([ffmpeg.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True)


def _has_encoder(name: str) -> bool:
    out = subprocess.run([ffmpeg.ffmpeg_bin(), "-hide_banner", "-encoders"],
                         capture_output=True, text=True).stdout
    return f" {name} " in out


def _photo(path: Path, size: tuple[int, int], hue: int, seed: int, blur: float = 0.0,
           fmt: str = "JPEG", quality: int = 90) -> None:
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", size, (hue, 120, 255 - hue))
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x0, y0 = int(rng.integers(0, size[0])), int(rng.integers(0, size[1]))
        r = int(rng.integers(20, size[0] // 4))
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        d.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], fill=color, outline=(255, 255, 255), width=4)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    if fmt == "HEIF":
        import pillow_heif
        pillow_heif.from_pillow(im).save(path, quality=85)
    else:
        im.save(path, fmt, quality=quality)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory) -> Path:
    if not ffmpeg.available():
        pytest.skip("ffmpeg/ffprobe not available")
    d = tmp_path_factory.mktemp("media")
    _photo(d / "01_beach.jpg", (1080, 1440), 40, 1)
    # Same picture saved at another quality: different bytes, near-identical look.
    _photo(d / "02_beach_again.jpg", (1080, 1440), 40, 1, quality=70)
    (d / "12_exact_copy.jpg").write_bytes((d / "01_beach.jpg").read_bytes())
    _photo(d / "03_group_landscape.jpg", (2000, 1300), 200, 3)
    _photo(d / "04_iphone.heic", (1200, 1600), 120, 4, fmt="HEIF")
    _photo(d / "05_blurry.jpg", (1080, 1440), 90, 5, blur=12)
    _ff("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=6",
        "-f", "lavfi", "-i", "sine=f=440:d=6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(d / "06_portrait.mp4"))
    _ff("-f", "lavfi", "-i", "mandelbrot=s=720x1280:r=25", "-t", "5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "07_zoom.mov"))
    # Landscape-coded clip with a 90 degree display rotation, like a phone held upright.
    _ff("-display_rotation", "90", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=30:d=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "08_rotated.mp4"))
    if _has_encoder("libx265"):
        # HLG 10-bit HEVC: what iPhones record by default.
        _ff("-f", "lavfi", "-i", "smptehdbars=s=1920x1080:r=30:d=4",
            "-vf", "format=yuv420p10le", "-c:v", "libx265", "-x265-params",
            "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
            "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
            "-tag:v", "hvc1", str(d / "09_hdr_landscape.mov"))
    (d / "10_notes.txt").write_text("not media")
    (d / "11_corrupt.jpg").write_bytes(b"\xff\xd8\xff\xe0 truncated")
    return d


@pytest.fixture(scope="session")
def song_file(tmp_path_factory) -> Path:
    if not ffmpeg.available():
        pytest.skip("ffmpeg/ffprobe not available")
    d = tmp_path_factory.mktemp("song")
    wav = d / "song.wav"
    sf.write(wav, make_song(), SR)
    out = d / "song.m4a"
    _ff("-i", str(wav), "-c:a", "aac", "-b:a", "160k", str(out))
    return out
