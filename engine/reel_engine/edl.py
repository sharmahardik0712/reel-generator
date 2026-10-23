"""Edit-decision list: the planner's output and the renderer's only input.

Timing is stored in whole frames (authoritative) so durations never drift; seconds
are included for readability.

Transition rule: a clip's frame_count includes its outgoing transition, so the next
clip starts at prev.start + prev.frame_count - prev.transition_out.frames.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

EDL_VERSION = 2
# Crossfades are part of the format but arrive with the M2 renderer.
TRANSITIONS = {"cut"}
FITS = {"crop", "blur_fill"}


class EDLError(ValueError):
    pass


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float


@dataclass
class Transition:
    type: str = "cut"
    frames: int = 0


@dataclass
class PhotoMotion:
    zoom_from: float = 1.0  # 1.0 = the fitted frame, 1.08 = 8% tighter
    zoom_to: float = 1.0
    # Normalized focus point (0..1) the zoom moves toward.
    focus_x: float = 0.5
    focus_y: float = 0.5


@dataclass
class Clip:
    asset_id: str
    kind: str  # "photo" | "video"
    timeline_start_frame: int
    frame_count: int
    fit: str = "crop"
    crop: Rect | None = None  # in proxy pixels; None = centered 9:16
    source_start_seconds: float | None = None  # video only
    motion: PhotoMotion | None = None  # photo only
    source_audio_gain: float = 0.0
    transition_out: Transition = field(default_factory=Transition)
    # Debug: why the planner chose this clip.
    score: float = 0.0
    role: str = "body"  # "hook" | "body" | "ending"


@dataclass
class AssetRef:
    kind: str
    proxy: str
    width: int
    height: int
    content_hash: str
    original_name: str


@dataclass
class MusicRef:
    path: str
    source_start_seconds: float
    bpm: float
    beat_confidence: float
    fade_in_seconds: float
    fade_out_seconds: float
    is_preview: bool
    title: str = ""
    artist: str = ""
    instagram_hint: str = ""


@dataclass
class Output:
    width: int
    height: int
    fps: int
    frames: int

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.fps


@dataclass
class EDL:
    version: int
    algorithm_version: str
    preset: str
    seed: int
    output: Output
    music: MusicRef | None
    assets: dict[str, AssetRef]
    clips: list[Clip]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["output"]["duration_seconds"] = round(self.output.duration_seconds, 4)
        for c, cd in zip(self.clips, d["clips"]):
            cd["timeline_start_seconds"] = round(c.timeline_start_frame / self.output.fps, 4)
            cd["duration_seconds"] = round(c.frame_count / self.output.fps, 4)
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def save(self, path: Path) -> None:
        path.write_text(self.to_json())

    @classmethod
    def from_dict(cls, d: dict) -> "EDL":
        out = d["output"]
        clips = []
        for c in d["clips"]:
            clips.append(Clip(
                asset_id=c["asset_id"], kind=c["kind"],
                timeline_start_frame=c["timeline_start_frame"], frame_count=c["frame_count"],
                fit=c.get("fit", "crop"),
                crop=Rect(**c["crop"]) if c.get("crop") else None,
                source_start_seconds=c.get("source_start_seconds"),
                motion=PhotoMotion(**c["motion"]) if c.get("motion") else None,
                source_audio_gain=c.get("source_audio_gain", 0.0),
                transition_out=Transition(**c.get("transition_out", {})),
                score=c.get("score", 0.0), role=c.get("role", "body"),
            ))
        return cls(
            version=d["version"], algorithm_version=d["algorithm_version"],
            preset=d["preset"], seed=d["seed"],
            output=Output(out["width"], out["height"], out["fps"], out["frames"]),
            music=MusicRef(**d["music"]) if d.get("music") else None,
            assets={k: AssetRef(**v) for k, v in d["assets"].items()},
            clips=clips, notes=d.get("notes", []),
        )

    @classmethod
    def load(cls, path: Path) -> "EDL":
        return cls.from_dict(json.loads(path.read_text()))


def validate(edl: EDL) -> None:
    """Raise EDLError if the EDL is inconsistent. Run before every render."""
    if edl.version != EDL_VERSION:
        raise EDLError(f"unsupported EDL version {edl.version}")
    if not edl.clips:
        raise EDLError("EDL has no clips")
    if edl.output.frames <= 0 or edl.output.fps <= 0:
        raise EDLError("output must have positive frames and fps")

    expected_start = 0
    for i, c in enumerate(edl.clips):
        where = f"clip {i} ({c.asset_id})"
        if c.asset_id not in edl.assets:
            raise EDLError(f"{where}: unknown asset")
        if edl.assets[c.asset_id].kind != c.kind:
            raise EDLError(f"{where}: kind {c.kind} does not match asset")
        if c.frame_count < 1:
            raise EDLError(f"{where}: frame_count must be >= 1")
        if c.timeline_start_frame != expected_start:
            raise EDLError(
                f"{where}: starts at frame {c.timeline_start_frame}, expected {expected_start}")
        if c.fit not in FITS:
            raise EDLError(f"{where}: unknown fit {c.fit}")
        t = c.transition_out
        if t.type not in TRANSITIONS:
            raise EDLError(f"{where}: unknown transition {t.type}")
        if t.type == "cut" and t.frames != 0:
            raise EDLError(f"{where}: a cut has no transition frames")
        if t.frames < 0 or t.frames >= c.frame_count:
            raise EDLError(f"{where}: transition longer than the clip")
        if i == len(edl.clips) - 1 and t.frames:
            raise EDLError(f"{where}: the last clip cannot have an outgoing transition")
        if c.kind == "video":
            if c.source_start_seconds is None or c.source_start_seconds < 0:
                raise EDLError(f"{where}: video needs source_start_seconds >= 0")
        elif c.source_start_seconds is not None:
            raise EDLError(f"{where}: photos have no source_start_seconds")
        expected_start = c.timeline_start_frame + c.frame_count - t.frames

    if expected_start != edl.output.frames:
        raise EDLError(
            f"clips cover {expected_start} frames but output is {edl.output.frames}")
