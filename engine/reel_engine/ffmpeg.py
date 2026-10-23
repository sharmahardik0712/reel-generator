"""Thin, safe wrappers around the ffmpeg/ffprobe binaries.

Every call uses an argument list (never a shell string) and restricts inputs to
local files and pipes, so user-supplied names can't inject options or fetch URLs.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

PROTOCOLS = "file,pipe"


class FFmpegError(RuntimeError):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        # Keep only the tail: it holds the actual error and avoids dumping paths.
        self.stderr_tail = "\n".join(stderr.strip().splitlines()[-15:])

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base}\n{self.stderr_tail}" if self.stderr_tail else base


@functools.cache
def ffmpeg_bin() -> str:
    return _find("ffmpeg", "REEL_FFMPEG")


@functools.cache
def ffprobe_bin() -> str:
    return _find("ffprobe", "REEL_FFPROBE")


def _find(name: str, env_var: str) -> str:
    override = os.environ.get(env_var)
    if override:
        return override
    found = shutil.which(name)
    if not found:
        # A project-local copy: <repo>/tools/ffmpeg/ffmpeg(.exe)
        local = Path(__file__).resolve().parents[2] / "tools" / "ffmpeg"
        for candidate in (local / f"{name}.exe", local / name):
            if candidate.exists():
                return str(candidate)
        raise FFmpegError(
            f"{name} not found. Install FFmpeg and put it on PATH, or set {env_var}."
        )
    return found


def available() -> bool:
    try:
        ffmpeg_bin()
        ffprobe_bin()
        return True
    except FFmpegError:
        return False


@functools.cache
def has_filter(name: str) -> bool:
    out = subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-filters"], capture_output=True, text=True
    ).stdout
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


def inp(path: str | Path, *opts: str | float) -> list[str]:
    """Input arguments for one file. The protocol whitelist is a per-input option,
    so every input must go through here."""
    return [*(str(o) for o in opts), "-protocol_whitelist", PROTOCOLS, "-i", str(path)]


def run(args: Sequence[str | Path], *, input_bytes: bytes | None = None) -> str:
    """Run ffmpeg with the given arguments; returns stderr (ffmpeg logs there)."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y"]
    cmd += [str(a) for a in args]
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True)
    stderr = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed (exit {proc.returncode})", stderr)
    return stderr


def popen_rawvideo_encoder(
    args: Sequence[str | Path],
) -> subprocess.Popen:
    """Start ffmpeg reading raw frames from stdin (caller writes frames, then closes)."""
    # No -nostdin here: stdin carries the frames.
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"]
    cmd += [str(a) for a in args]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def read_raw(args: Sequence[str | Path]) -> bytes:
    """Run ffmpeg writing raw data to stdout (used for frames and PCM audio)."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error"]
    cmd += [str(a) for a in args]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FFmpegError("ffmpeg decode failed", proc.stderr.decode("utf-8", "replace"))
    return proc.stdout


def probe(path: Path) -> dict:
    cmd = [
        ffprobe_bin(), "-v", "error", "-protocol_whitelist", PROTOCOLS,
        "-print_format", "json", "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe could not read {path.name}", proc.stderr.decode("utf-8", "replace"))
    return json.loads(proc.stdout.decode("utf-8", "replace"))
