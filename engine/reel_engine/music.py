"""Music analysis: beats, bars, energy, and where the reel's music should start.

The music drives the edit, so this runs before any timing decisions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from . import ffmpeg

log = logging.getLogger(__name__)

SR = 22050
MUSIC_VERSION = 1
MIN_CONFIDENCE = 0.45
FALLBACK_BPM = 120.0


@dataclass
class MusicAnalysis:
    version: int
    duration: float
    bpm: float
    beats: list[float]  # seconds in the song
    confidence: float  # 0..1, how regular the detected beats are
    downbeat_phase: int  # beats[phase::4] are bar starts
    beat_energy: list[float]  # 0..1 per beat, relative within the song
    synthetic_grid: bool = False

    @property
    def downbeats(self) -> list[float]:
        return self.beats[self.downbeat_phase::4]


@dataclass
class MusicPlan:
    start: float  # seconds into the song where the reel's music begins
    length: float  # seconds of music for the reel (<= requested)
    beats: list[float]  # beat times on the reel timeline (start subtracted)
    beat_energy: list[float]
    downbeat_indices: set[int]
    # Seconds of song left after `start`; beats run a little past `length` so the
    # reel can end on the beat nearest the target even if that beat is just after it.
    available: float = 0.0


def decode(path: Path, sr: int = SR) -> np.ndarray:
    raw = ffmpeg.read_raw([*ffmpeg.inp(path), "-vn", "-ac", "1", "-ar", str(sr),
                           "-f", "f32le", "pipe:1"])
    return np.frombuffer(raw, np.float32).copy()


def analyze(path: Path, cache_file: Path | None = None) -> MusicAnalysis:
    if cache_file and cache_file.exists():
        data = json.loads(cache_file.read_text())
        if data.get("version") == MUSIC_VERSION:
            return MusicAnalysis(**data)
    result = analyze_signal(decode(path))
    if cache_file:
        cache_file.write_text(json.dumps(asdict(result)))
    return result


def analyze_signal(y: np.ndarray, sr: int = SR) -> MusicAnalysis:
    import librosa  # imported lazily: slow to load and only needed here

    duration = len(y) / sr
    if duration < 3.0:
        raise ValueError("the song is shorter than 3 seconds")
    hop = 512
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=hop)
    bpm = float(np.atleast_1d(tempo)[0])
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop).tolist()

    confidence = beat_confidence(beats)
    synthetic = False
    if confidence < MIN_CONFIDENCE:
        log.info("low beat confidence (%.2f); using a fixed %s BPM grid", confidence, bpm)
        if not 60 <= bpm <= 200:
            bpm = FALLBACK_BPM
        first = float(librosa.frames_to_time(int(np.argmax(onset[: int(sr / hop * 2)])),
                                             sr=sr, hop_length=hop))
        beats = np.arange(first, duration, 60.0 / bpm).tolist()
        synthetic = True

    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    energy = _beat_energy(beats, rms, onset, sr, hop, duration)
    phase = _downbeat_phase(beats, onset, sr, hop)
    return MusicAnalysis(
        version=MUSIC_VERSION, duration=round(duration, 3), bpm=round(bpm, 2),
        beats=[round(b, 4) for b in beats], confidence=round(confidence, 3),
        downbeat_phase=phase, beat_energy=[round(e, 4) for e in energy],
        synthetic_grid=synthetic,
    )


def beat_confidence(beats: list[float]) -> float:
    """librosa gives no confidence, so use regularity of inter-beat intervals."""
    if len(beats) < 8:
        return 0.0
    ibi = np.diff(beats)
    cv = float(np.std(ibi) / max(np.mean(ibi), 1e-6))
    return float(np.clip(1.0 - cv * 5.0, 0.0, 1.0))


def _beat_energy(beats, rms, onset, sr, hop, duration) -> list[float]:
    if not beats:
        return []
    import librosa

    bounds = list(beats) + [duration]
    frames = librosa.time_to_frames(bounds, sr=sr, hop_length=hop)
    n = min(len(rms), len(onset))
    rms_n = rms[:n] / max(float(np.percentile(rms[:n], 95)), 1e-9)
    onset_n = onset[:n] / max(float(np.percentile(onset[:n], 95)), 1e-9)
    raw = []
    for a, b in zip(frames[:-1], frames[1:]):
        a = int(min(a, n - 1))
        b = int(min(max(b, a + 1), n))
        raw.append(0.65 * float(rms_n[a:b].mean()) + 0.35 * float(onset_n[a:b].mean()))
    raw = np.array(raw)
    # Smooth over a bar so single hits don't flip the pacing.
    smooth = np.convolve(raw, np.ones(4) / 4, mode="same")
    # Rank-normalize: energy is relative to the rest of this song.
    ranks = smooth.argsort().argsort()
    return (ranks / max(len(ranks) - 1, 1)).tolist()


def _downbeat_phase(beats, onset, sr, hop) -> int:
    import librosa

    if len(beats) < 8:
        return 0
    idx = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=hop), 0, len(onset) - 1)
    strength = onset[idx]
    sums = [float(strength[p::4].mean()) for p in range(4)]
    return int(np.argmax(sums))


def plan_music(m: MusicAnalysis, target: float, start: float | None = None) -> MusicPlan:
    """Pick where the music starts: the highest-energy stretch that begins on a bar,
    preferring one that starts right before an energy jump (the drop)."""
    beats = np.array(m.beats)
    energy = np.array(m.beat_energy) if m.beat_energy else np.zeros(len(beats))

    if start is None:
        start = _best_start(m, beats, energy, target)
    available = m.duration - start
    length = min(target, available)
    sel = (beats >= start - 1e-6) & (beats < min(start + length + 2.0, m.duration) - 1e-6)
    idxs = np.nonzero(sel)[0]
    downbeat_set = set(range(m.downbeat_phase, len(beats), 4))
    return MusicPlan(
        start=round(float(start), 4),
        length=round(float(length), 4),
        beats=[round(float(b - start), 4) for b in beats[idxs]],
        beat_energy=[float(energy[i]) for i in idxs],
        downbeat_indices={j for j, i in enumerate(idxs) if i in downbeat_set},
        available=round(float(available), 4),
    )


def _best_start(m: MusicAnalysis, beats: np.ndarray, energy: np.ndarray, target: float) -> float:
    downs = [d for d in m.downbeats if d + target <= m.duration + 0.05]
    if not downs:
        # The song is about as long as the reel (e.g. a 30 s preview for a 30 s reel):
        # use all of it rather than waiting for the first bar line.
        return 0.0
    best, best_score = downs[0], -1e9
    for d in downs:
        inside = energy[(beats >= d) & (beats < d + target)]
        before = energy[(beats < d) & (beats >= d - 4.0)]
        after = energy[(beats >= d) & (beats < d + 4.0)]
        jump = max(0.0, float(after.mean() - before.mean())) if len(before) and len(after) else 0.0
        score = (float(inside.mean()) if len(inside) else 0.0) + 0.5 * jump
        if score > best_score + 1e-9:  # earliest wins ties: deterministic
            best, best_score = d, score
    return float(best)


def format_timestamp(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"
