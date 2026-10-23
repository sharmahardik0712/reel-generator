"""Deterministic planner: candidates -> selection -> order -> beat-locked timing -> EDL.

Same media + settings + seed always produce the same EDL. Shuffle = a new seed.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cache
from importlib import resources

import yaml

from . import ALGORITHM_VERSION, FPS, OUTPUT_HEIGHT, OUTPUT_WIDTH
from .analysis import Analysis, best_window, hamming
from .edl import EDL, EDL_VERSION, AssetRef, Clip, MusicRef, Output, PhotoMotion, validate
from .music import MusicPlan
from .probe import MediaInfo

DURATION_TOLERANCE = 0.25
NO_MUSIC_BPM = 120.0
SCARCE_STRETCH = 1.5


@cache
def presets() -> dict:
    text = resources.files("reel_engine").joinpath("presets.yaml").read_text()
    return yaml.safe_load(text)


@dataclass
class Asset:
    id: str
    info: MediaInfo
    analysis: Analysis
    proxy: str


@dataclass
class Candidate:
    asset: Asset
    seg_start: float = 0.0  # video segment bounds within the source (seconds)
    seg_end: float = 0.0
    quality: float = 0.0
    interest: float = 0.0
    motion: float = 0.0
    score: float = 0.0
    hook_score: float = 0.0
    when: float | None = None  # capture time (epoch seconds) of this moment

    @property
    def is_video(self) -> bool:
        return self.asset.info.kind == "video"

    @property
    def max_len(self) -> float:
        return self.seg_end - self.seg_start if self.is_video else math.inf

    @property
    def sort_key(self) -> tuple:
        return (self.asset.info.upload_index, self.seg_start, self.asset.id)


@dataclass
class Shot:
    cand: Candidate
    start: float
    end: float
    role: str = "body"


@dataclass
class PlanResult:
    edl: EDL
    shots: list[Shot] = field(default_factory=list)


# --- candidates --------------------------------------------------------------------

def build_candidates(assets: list[Asset], preset: dict, sel: dict, seed: int) -> list[Candidate]:
    rng = random.Random(seed)
    w = preset["weights"]
    hw = preset["hook_weights"]
    cands: list[Candidate] = []
    for a in sorted(assets, key=lambda a: (a.info.upload_index, a.id)):
        when = _epoch(a.info.capture_time)
        if a.info.kind == "photo":
            # Photos get a moderate motion value: Ken Burns gives them some movement.
            cands.append(Candidate(a, quality=a.analysis.quality,
                                   interest=a.analysis.interest, motion=0.3, when=when))
            continue
        for s, e in _segments(a, sel):
            cands.append(_video_candidate(a, s, e, when))
    for c in cands:
        jitter = rng.uniform(-1, 1) * sel["shuffle_jitter"]
        c.score = w["quality"] * c.quality + w["interest"] * c.interest + w["motion"] * c.motion + jitter
        c.hook_score = hw["quality"] * c.quality + hw["interest"] * c.interest + hw["motion"] * c.motion + jitter
    return cands


def _segments(a: Asset, sel: dict) -> list[tuple[float, float]]:
    dur = a.info.duration
    n = max(1, min(sel["max_segments_per_video"], int(dur // 5) * sel["segments_per_5s"]))
    cuts = [0.0] + [c for c in a.analysis.scene_cuts if 0.5 < c < dur - 0.5] + [dur]
    if len(cuts) - 1 >= n:
        # Natural boundaries: keep the n longest scenes.
        scenes = sorted(zip(cuts[:-1], cuts[1:]), key=lambda se: se[1] - se[0], reverse=True)[:n]
        return sorted(scenes)
    step = dur / n
    return [(round(i * step, 3), round((i + 1) * step, 3)) for i in range(n)]


def _video_candidate(a: Asset, s: float, e: float, when: float | None) -> Candidate:
    an = a.analysis
    fps = an.sample_fps or 4
    i0, i1 = int(s * fps), max(int(s * fps) + 1, int(e * fps))

    def mean(series):
        part = series[i0:i1] or series or [0.0]
        return sum(part) / len(part)

    sharp, expo, mot = mean(an.sharpness_series), mean(an.exposure_series), mean(an.motion_series)
    return Candidate(
        a, seg_start=s, seg_end=e, quality=0.6 * sharp + 0.4 * expo,
        interest=an.interest, motion=min(1.0, mot * 1.5),
        when=(when + s) if when is not None else None,
    )


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    # Naive times (no EXIF offset) are treated as UTC; fine for ordering within one event.
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


# --- selection and ordering ----------------------------------------------------

def similarity(a: Candidate, b: Candidate, sel: dict) -> float:
    if a.asset.id == b.asset.id:
        return 1.0
    d = hamming(a.asset.analysis.phash, b.asset.analysis.phash)
    if d <= sel["duplicate_hamming"]:
        return 1.0
    return max(0.0, 1.0 - d / 32.0)


def select(cands: list[Candidate], n: int, sel: dict) -> tuple[list[Candidate], list[Candidate]]:
    """Greedy maximal marginal relevance: good shots that don't look alike.

    Returns (chosen, reserve). Near-duplicates of an already-chosen shot go to the
    reserve, used only when the reel can't be filled without them.
    """
    pool = sorted(cands, key=lambda c: (-c.score, c.sort_key))
    chosen: list[Candidate] = []
    reserve: list[Candidate] = []
    while pool and len(chosen) < n:
        def mmr(c: Candidate) -> float:
            # Segments of one video are told apart by time, not by look: they get the
            # same-source penalty instead of a similarity penalty.
            others = [s for s in chosen if s.asset.id != c.asset.id]
            max_sim = max((similarity(c, s, sel) for s in others), default=0.0)
            same = len(chosen) - len(others)
            return c.score - sel["diversity"] * max_sim - sel["same_source_penalty"] * same
        best = max(pool, key=lambda c: (mmr(c), -c.sort_key[0], -c.sort_key[1]))
        pool.remove(best)
        if any(s.asset.id != best.asset.id and similarity(best, s, sel) >= 1.0 for s in chosen):
            reserve.append(best)
        else:
            chosen.append(best)
    return chosen, reserve


def order(chosen: list[Candidate]) -> list[Candidate]:
    """Hook first, then chronological (or upload order when times are unreliable)."""
    if not chosen:
        return []
    hook = max(chosen, key=lambda c: (c.hook_score, -c.sort_key[0], -c.sort_key[1]))
    rest = [c for c in chosen if c is not hook]
    reliable = all(c.when is not None and c.asset.info.capture_time_reliable for c in rest)
    if reliable:
        rest.sort(key=lambda c: (c.when, c.sort_key))
    else:
        rest.sort(key=lambda c: c.sort_key)
    return [hook, *rest]


# --- timing --------------------------------------------------------------------

def beat_grid(music: MusicPlan | None, target: float) -> tuple[list[float], list[float], float]:
    """Return (cut candidates, energy per cut candidate, reel end) on the reel timeline."""
    if music and len(music.beats) >= 4:
        beats = list(music.beats)
        energy = list(music.beat_energy)
        limit = min(target, music.length)
        available = music.available or music.length
    else:
        step = 60.0 / NO_MUSIC_BPM
        beats = [round(i * step, 4) for i in range(int(target / step) + 2)]
        energy = [0.5] * len(beats)
        limit = available = target
    if not beats or beats[0] > 0.05:
        beats.insert(0, 0.0)
        energy.insert(0, energy[0] if energy else 0.5)
    # End on the beat nearest the target that the song can still cover; if no beat is
    # close enough, end exactly at the limit (the music fades out there anyway).
    ends = [b for b in beats if 0 < b <= available + 1e-6]
    end = min(ends, key=lambda b: abs(b - limit)) if ends else limit
    if abs(end - limit) > DURATION_TOLERANCE:
        end = limit
    keep = [i for i, b in enumerate(beats) if b < end - 1e-6]
    beats = [beats[i] for i in keep] + [end]
    energy = [energy[i] for i in keep] + [energy[keep[-1]] if keep else 0.5]
    return beats, energy, end


def _level(e: float) -> str:
    return "high" if e >= 0.66 else "mid" if e >= 0.33 else "low"


def time_shots(seq: list[Candidate], beats: list[float], energy: list[float],
               preset: dict) -> list[Shot]:
    """Give each candidate a beat-aligned slot. Stops when the timeline is full."""
    shots: list[Shot] = []
    last = len(beats) - 1
    k = 0
    for idx, c in enumerate(seq):
        if k >= last:
            break
        b = preset["beats_per_shot"][_level(energy[k])]
        max_shot = preset["max_shot_seconds"]
        # Scarce media: stretch shots (up to 1.5x the preset maximum) rather than end early.
        needed = math.ceil((last - k) / (len(seq) - idx))
        if needed > b:
            b, max_shot = needed, max_shot * SCARCE_STRETCH
        j = min(k + b, last)
        dur = lambda j: beats[j] - beats[k]  # noqa: E731
        while j < last and dur(j) < preset["min_shot_seconds"]:
            j += 1
        while j > k + 1 and (dur(j) > max_shot or dur(j) > c.max_len):
            j -= 1
        if dur(j) > c.max_len + 1e-6:
            continue  # segment shorter than one beat here; try the next candidate
        # Don't leave a sliver at the end that no shot could fill.
        if 0 < beats[last] - beats[j] < preset["min_shot_seconds"]:
            if beats[last] - beats[k] <= c.max_len + 1e-6:
                j = last
        shots.append(Shot(c, beats[k], beats[j]))
        k = j
    return shots


# --- top level -----------------------------------------------------------------

def plan(
    assets: list[Asset],
    music: MusicPlan | None,
    music_ref: MusicRef | None,
    target_seconds: float,
    preset_name: str = "hype",
    seed: int = 0,
) -> PlanResult:
    if not assets:
        raise ValueError("no usable photos or videos")
    cfg = presets()
    preset, sel = cfg[preset_name], cfg["selection"]
    notes: list[str] = []

    beats, energy, end = beat_grid(music, target_seconds)
    if music and music.length < target_seconds - DURATION_TOLERANCE:
        notes.append(f"The song only has {music.length:.1f}s from its start point, "
                     f"so the reel is {end:.1f}s instead of {target_seconds:.0f}s.")

    cands = build_candidates(assets, preset, sel, seed)
    avg_beat = (beats[-1] - beats[0]) / max(len(beats) - 1, 1)
    avg_shot = min(preset["max_shot_seconds"],
                   max(preset["min_shot_seconds"], preset["beats_per_shot"]["mid"] * avg_beat))
    wanted = math.ceil(end / avg_shot) + 1

    ranked, reserve = select(cands, min(len(cands), math.ceil(wanted * 1.5) + 2), sel)
    shots = _fit(ranked, beats, energy, preset)
    if reserve and (not shots or shots[-1].end < end - DURATION_TOLERANCE):
        shots = _fit(ranked + reserve, beats, energy, preset)
    if not shots:
        raise ValueError("none of the media fits the beat grid (videos too short?)")

    reel_end = shots[-1].end
    if reel_end < end - DURATION_TOLERANCE:
        notes.append(f"Not enough usable media for {end:.1f}s; the reel is {reel_end:.1f}s. "
                     "Add more photos or videos for a longer reel.")
    shots[0].role = "hook"
    if len(shots) > 1:
        shots[-1].role = "ending"
    edl = _to_edl(shots, preset, preset_name, seed, music_ref, notes)
    validate(edl)
    return PlanResult(edl, shots)


def _fit(ranked: list[Candidate], beats: list[float], energy: list[float],
         preset: dict) -> list[Shot]:
    """Time the ranked picks; if some don't fit, drop the lowest-ranked and re-time,
    so dropped shots come from the weakest picks, not the end of the story."""
    keep = len(ranked)
    shots: list[Shot] = []
    for _ in range(4):
        shots = time_shots(order(ranked[:keep]), beats, energy, preset)
        if len(shots) >= keep or not shots:
            break
        keep = len(shots)
    return shots


def _to_edl(shots: list[Shot], preset: dict, preset_name: str, seed: int,
            music_ref: MusicRef | None, notes: list[str]) -> EDL:
    clips: list[Clip] = []
    assets: dict[str, AssetRef] = {}
    frame = 0
    for i, s in enumerate(shots):
        a = s.cand.asset
        assets[a.id] = AssetRef(kind=a.info.kind, proxy=a.proxy, width=a.info.width,
                                height=a.info.height, content_hash=a.info.content_hash,
                                original_name=a.info.path.replace("\\", "/").rsplit("/", 1)[-1])
        end_frame = round(s.end * FPS)
        count = end_frame - frame
        fit = "blur_fill" if (a.info.is_landscape and preset["landscape_mode"] == "blur_fill") else "crop"
        clip = Clip(asset_id=a.id, kind=a.info.kind, timeline_start_frame=frame,
                    frame_count=count, fit=fit, score=round(s.cand.score, 4), role=s.role)
        if s.cand.is_video:
            length = count / FPS
            start, _, _ = best_window(a.analysis, a.info.duration, length,
                                      s.cand.seg_start, s.cand.seg_end)
            clip.source_start_seconds = round(min(start, max(0.0, a.info.duration - length)), 3)
        else:
            z = 1.0 + preset["photo_zoom"]
            # Alternate zoom in / zoom out so consecutive photos don't feel identical.
            clip.motion = PhotoMotion(zoom_from=1.0, zoom_to=z) if i % 2 == 0 else \
                PhotoMotion(zoom_from=z, zoom_to=1.0)
            clip.motion.focus_y = 0.45  # faces sit slightly above center in most photos
        clips.append(clip)
        frame = end_frame
    return EDL(
        version=EDL_VERSION, algorithm_version=ALGORITHM_VERSION, preset=preset_name,
        seed=seed, output=Output(OUTPUT_WIDTH, OUTPUT_HEIGHT, FPS, frame),
        music=music_ref, assets=assets, clips=clips, notes=notes,
    )
