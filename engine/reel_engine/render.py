"""Renderer: executes an EDL. It makes no editorial decisions.

1. Render each clip to a normalized intermediate (1080x1920, 30 fps, yuv420p, exact
   frame count). Clips render in parallel and are cached by their spec, so a retry
   only redoes clips that changed.
2. Join intermediates with the concat demuxer (all cuts, identical encoding).
3. Mux loudness-normalized music -> with-music MP4; also write a silent MP4.
4. Validate both files with ffprobe.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageFilter

from . import ffmpeg
from .edl import EDL, Clip, validate

log = logging.getLogger(__name__)

RENDER_VERSION = 1
X264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high",
        "-pix_fmt", "yuv420p", "-g", "30", "-bf", "2"]
COLOR_TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
LOUDNESS = "I=-14:TP=-1.0:LRA=11"


@dataclass
class RenderResult:
    with_music: Path | None
    silent: Path
    duration: float
    report: dict


class ValidationError(RuntimeError):
    pass


def render(edl: EDL, out_dir: Path, work_dir: Path, name: str = "reel",
           workers: int | None = None) -> RenderResult:
    validate(edl)
    out_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = work_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    workers = workers or max(1, min(4, (os.cpu_count() or 2) // 2))
    with ThreadPoolExecutor(workers) as pool:
        parts = list(pool.map(lambda c: _render_clip(edl, c, clips_dir), edl.clips))

    joined = work_dir / f"{name}.video.mp4"
    _concat(parts, joined, work_dir)

    silent = out_dir / f"{name}-silent.mp4"
    ffmpeg.run([*ffmpeg.inp(joined), "-map", "0:v:0", "-c", "copy",
                "-movflags", "+faststart", silent])

    with_music = None
    if edl.music:
        with_music = out_dir / f"{name}.mp4"
        _mux_music(edl, joined, with_music)

    expected = edl.output.duration_seconds
    report = {"expected_seconds": round(expected, 3)}
    report["silent"] = check_output(silent, expected, want_audio=False, edl=edl)
    if with_music:
        report["with_music"] = check_output(with_music, expected, want_audio=True, edl=edl)
    return RenderResult(with_music, silent, expected, report)


# --- per-clip -------------------------------------------------------------------

def _clip_key(edl: EDL, clip: Clip) -> str:
    ref = edl.assets[clip.asset_id]
    spec = {"v": RENDER_VERSION, "clip": asdict(clip), "hash": ref.content_hash,
            "out": asdict(edl.output)}
    for k in ("timeline_start_frame", "score", "role"):  # don't affect pixels
        spec["clip"].pop(k)
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:24]


def _render_clip(edl: EDL, clip: Clip, clips_dir: Path) -> Path:
    out = clips_dir / f"{_clip_key(edl, clip)}.mp4"
    if out.exists():
        return out
    tmp = out.with_suffix(".tmp.mp4")
    if clip.kind == "photo":
        _render_photo(edl, clip, tmp)
    else:
        _render_video(edl, clip, tmp)
    tmp.replace(out)
    return out


def _render_video(edl: EDL, clip: Clip, out: Path) -> None:
    ref = edl.assets[clip.asset_id]
    W, H, fps = edl.output.width, edl.output.height, edl.output.fps
    if clip.fit == "blur_fill":
        graph = (
            f"[0:v]split=2[a][b];"
            f"[a]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,"
            f"crop={W // 4}:{H // 4},boxblur=12:2,scale={W}:{H},eq=brightness=-0.08[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={fps},format=yuv420p[v]"
        )
    else:
        if clip.crop:
            c = clip.crop
            crop = f"crop={int(c.w)}:{int(c.h)}:{int(c.x)}:{int(c.y)},"
        else:
            crop = f"crop='min(iw,ih*{W}/{H})':'min(ih,iw*{H}/{W})',"
        graph = f"[0:v]{crop}scale={W}:{H}:flags=bicubic,setsar=1,fps={fps},format=yuv420p[v]"
    # Pad by cloning the last frame in case the proxy ends a few frames early (VFR sources).
    graph = graph.replace("format=yuv420p[v]", "tpad=stop_mode=clone:stop_duration=1,format=yuv420p[v]")
    ffmpeg.run([
        *ffmpeg.inp(ref.proxy, "-ss", f"{clip.source_start_seconds:.3f}"),
        "-filter_complex", graph, "-map", "[v]", "-an",
        "-frames:v", str(clip.frame_count), "-r", str(fps),
        *X264, *COLOR_TAGS, out,
    ])
    _ensure_frames(out, clip.frame_count, edl.output.fps)


def _render_photo(edl: EDL, clip: Clip, out: Path) -> None:
    """Ken Burns in Python: sub-pixel accurate, so no zoompan jitter."""
    ref = edl.assets[clip.asset_id]
    W, H, fps = edl.output.width, edl.output.height, edl.output.fps
    with Image.open(ref.proxy) as im:
        src = im.convert("RGB")
    canvas = _photo_canvas(src, clip.fit, W, H)
    cw, ch = canvas.size
    m = clip.motion
    z0, z1 = (m.zoom_from, m.zoom_to) if m else (1.0, 1.0)
    fx, fy = (m.focus_x, m.focus_y) if m else (0.5, 0.5)

    enc = ffmpeg.popen_rawvideo_encoder([
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
        *ffmpeg.inp("pipe:0"), "-frames:v", str(clip.frame_count),
        *X264, *COLOR_TAGS, out,
    ])
    try:
        n = clip.frame_count
        for i in range(n):
            t = i / max(n - 1, 1)
            t = t * t * (3 - 2 * t)  # ease in/out
            zoom = z0 + (z1 - z0) * t
            # Visible window: the 9:16 base window (whole canvas) shrunk by zoom,
            # drifting toward the focus point as it zooms.
            w, h = cw / zoom, ch / zoom
            cx = cw / 2 + (fx - 0.5) * (cw - w)
            cy = ch / 2 + (fy - 0.5) * (ch - h)
            box = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            frame = canvas.transform((W, H), Image.Transform.EXTENT, box,
                                     Image.Resampling.BICUBIC)
            enc.stdin.write(frame.tobytes())
        enc.stdin.close()
        err = enc.stderr.read().decode("utf-8", "replace")
        if enc.wait() != 0:
            raise ffmpeg.FFmpegError("photo encode failed", err)
    finally:
        if enc.poll() is None:
            enc.kill()
    _ensure_frames(out, clip.frame_count, fps)


def _photo_canvas(src: Image.Image, fit: str, W: int, H: int) -> Image.Image:
    """A 9:16 image the motion window moves inside. It is 15% larger than the output,
    so zooming up to 1.15x never upscales below output resolution."""
    cw, ch = round(W * 1.15), round(H * 1.15)
    sw, sh = src.size
    if fit == "crop":
        if sw / sh > W / H:
            w, h = sh * W / H, sh
        else:
            w, h = sw, sw * H / W
        left, top = (sw - w) / 2, (sh - h) / 2
        canvas = src.crop((round(left), round(top), round(left + w), round(top + h)))
        return canvas if canvas.width >= cw else canvas.resize((cw, ch), Image.Resampling.LANCZOS)
    # blur_fill: blurred, darkened cover background with the whole photo fitted on top.
    small = (cw // 8, ch // 8)
    s = max(small[0] / sw, small[1] / sh)
    bg = src.resize((max(small[0], round(sw * s)), max(small[1], round(sh * s))),
                    Image.Resampling.BILINEAR)
    left, top = (bg.width - small[0]) // 2, (bg.height - small[1]) // 2
    bg = bg.crop((left, top, left + small[0], top + small[1]))
    bg = bg.filter(ImageFilter.GaussianBlur(4)).resize((cw, ch), Image.Resampling.BICUBIC)
    bg = bg.point(lambda v: int(v * 0.8))
    s = min(cw / sw, ch / sh)
    fg = src.resize((round(sw * s), round(sh * s)), Image.Resampling.LANCZOS)
    bg.paste(fg, ((cw - fg.width) // 2, (ch - fg.height) // 2))
    return bg


def _ensure_frames(path: Path, frames: int, fps: int) -> None:
    info = ffmpeg.probe(path)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    n = int(v.get("nb_frames") or round(float(info["format"]["duration"]) * fps))
    if n != frames:
        raise ValidationError(f"{path.name}: rendered {n} frames, expected {frames}")


# --- assembly -------------------------------------------------------------------

def _concat(parts: list[Path], out: Path, work_dir: Path) -> None:
    lst = work_dir / "concat.txt"
    # The concat demuxer parses this file; generated paths only, quotes escaped.
    lines = []
    for p in parts:
        escaped = p.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{escaped}'\n")
    lst.write_text("".join(lines))
    ffmpeg.run([
        "-f", "concat", "-safe", "0", *ffmpeg.inp(lst),
        "-c", "copy", "-movflags", "+faststart", out,
    ])


def _mux_music(edl: EDL, video: Path, out: Path) -> None:
    m = edl.music
    dur = edl.output.duration_seconds
    fade_out = min(m.fade_out_seconds, dur / 3)
    trim = ["-ss", f"{m.source_start_seconds:.3f}", "-t", f"{dur:.3f}"]

    # Pass 1: measure loudness of exactly the part we use.
    stats = ffmpeg.run([
        *ffmpeg.inp(m.path, *trim), "-vn",
        "-af", f"loudnorm={LOUDNESS}:print_format=json", "-f", "null", "-",
    ])
    measured = _parse_loudnorm(stats)
    ln = f"loudnorm={LOUDNESS}"
    if measured:
        ln += (f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
               f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
               f":offset={measured['target_offset']}:linear=true")
    af = (f"{ln},aresample=48000,"
          f"afade=t=in:st=0:d={m.fade_in_seconds:.3f},"
          f"afade=t=out:st={max(0.0, dur - fade_out):.3f}:d={fade_out:.3f},"
          f"apad,atrim=0:{dur:.3f}")
    ffmpeg.run([
        *ffmpeg.inp(video), *ffmpeg.inp(m.path, *trim),
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
        "-af", af, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-t", f"{dur:.3f}", "-movflags", "+faststart", out,
    ])


def _parse_loudnorm(stderr: str) -> dict | None:
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if any(str(data.get(k, "")).lstrip("-").startswith("inf") for k in ("input_i", "input_tp")):
        return None  # silent input: let single-pass loudnorm handle it
    return data


# --- validation -----------------------------------------------------------------

def check_output(path: Path, expected_seconds: float, want_audio: bool, edl: EDL) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        raise ValidationError(f"{path.name} is missing or empty")
    info = ffmpeg.probe(path)
    streams = info.get("streams", [])
    v = next((s for s in streams if s["codec_type"] == "video"), None)
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    if v is None:
        raise ValidationError(f"{path.name} has no video stream")
    problems = []
    if (v["width"], v["height"]) != (edl.output.width, edl.output.height):
        problems.append(f"size {v['width']}x{v['height']}")
    if v["codec_name"] != "h264":
        problems.append(f"video codec {v['codec_name']}")
    duration = float(info["format"]["duration"])
    if abs(duration - expected_seconds) > 0.1:
        problems.append(f"duration {duration:.3f}s vs {expected_seconds:.3f}s")
    if want_audio and (a is None or a["codec_name"] != "aac"):
        problems.append("missing AAC audio")
    if not want_audio and a is not None:
        problems.append("unexpected audio stream")
    if problems:
        raise ValidationError(f"{path.name}: " + ", ".join(problems))
    return {"file": path.name, "duration": round(duration, 3), "size_bytes": path.stat().st_size,
            "video": f"{v['codec_name']} {v['width']}x{v['height']}",
            "audio": a["codec_name"] if a else None}
