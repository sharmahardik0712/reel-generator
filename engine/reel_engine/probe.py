"""Identify media files and read the facts the planner needs.

File type is decided from content (magic bytes / ffprobe), never the extension.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import ExifTags, Image
import pillow_heif

from . import ffmpeg

pillow_heif.register_heif_opener()

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10/Dolby Vision) and HLG
PHOTO_FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "MPO"}
VIDEO_CODECS = {"h264", "hevc", "vp9", "av1", "mpeg4", "prores"}
IMAGE_CODECS = {"mjpeg", "png", "webp", "bmp", "tiff", "gif", "jpeg2000"}

MAX_FILE_BYTES = 500 * 1024 * 1024


class UnsupportedMedia(ValueError):
    pass


@dataclass
class MediaInfo:
    path: str
    content_hash: str
    kind: str  # "photo" | "video" | "audio"
    width: int = 0
    height: int = 0
    duration: float = 0.0
    fps: float = 0.0
    is_hdr: bool = False
    has_audio: bool = False
    capture_time: str | None = None  # ISO 8601, UTC when the offset is known
    capture_time_reliable: bool = False
    upload_index: int = 0

    @property
    def is_landscape(self) -> bool:
        return self.width > self.height

    def to_dict(self) -> dict:
        return asdict(self)


def content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_file(path: Path, upload_index: int = 0) -> MediaInfo:
    size = path.stat().st_size
    if size == 0:
        raise UnsupportedMedia(f"{path.name} is empty")
    if size > MAX_FILE_BYTES:
        raise UnsupportedMedia(f"{path.name} is larger than {MAX_FILE_BYTES // 2**20} MB")

    digest = content_hash(path)
    photo = _try_photo(path, digest)
    if photo:
        photo.upload_index = upload_index
        return photo
    info = _probe_av(path, digest)
    info.upload_index = upload_index
    return info


def _try_photo(path: Path, digest: str) -> MediaInfo | None:
    try:
        with Image.open(path) as im:
            if im.format not in PHOTO_FORMATS:
                return None
            if getattr(im, "is_animated", False) and im.format != "MPO":
                return None
            exif = im.getexif()
            w, h = im.size
            # EXIF orientations 5-8 rotate by 90 degrees.
            if exif.get(ExifTags.Base.Orientation, 1) in (5, 6, 7, 8):
                w, h = h, w
            when, reliable = _photo_capture_time(exif)
    except (OSError, SyntaxError, ValueError):
        return None
    return MediaInfo(
        path=str(path), content_hash=digest, kind="photo", width=w, height=h,
        capture_time=when, capture_time_reliable=reliable,
    )


def _photo_capture_time(exif: Image.Exif) -> tuple[str | None, bool]:
    try:
        sub = exif.get_ifd(ExifTags.IFD.Exif)
    except (KeyError, AttributeError):
        sub = {}
    raw = sub.get(ExifTags.Base.DateTimeOriginal) or exif.get(ExifTags.Base.DateTime)
    if not raw:
        return None, False
    try:
        local = datetime.strptime(str(raw).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None, False
    offset = sub.get(ExifTags.Base.OffsetTimeOriginal)
    if offset:
        try:
            sign = -1 if offset.startswith("-") else 1
            hh, mm = offset.lstrip("+-").split(":")
            tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
            return local.replace(tzinfo=tz).astimezone(timezone.utc).isoformat(), True
        except ValueError:
            pass
    # No offset: a local wall-clock time. Still reliable for ordering within one trip.
    return local.isoformat(), True


def _probe_av(path: Path, digest: str) -> MediaInfo:
    try:
        data = ffmpeg.probe(path)
    except ffmpeg.FFmpegError as e:
        raise UnsupportedMedia(f"{path.name} is not a readable photo, video or audio file") from e

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next(
        (s for s in streams if s.get("codec_type") == "video"
         and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(fmt.get("duration") or (video or audio or {}).get("duration") or 0)
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    if video is None:
        if audio is None:
            raise UnsupportedMedia(f"{path.name} has no video or audio stream")
        return MediaInfo(path=str(path), content_hash=digest, kind="audio",
                         duration=duration, has_audio=True)

    codec = video.get("codec_name", "")
    if codec in IMAGE_CODECS:
        # Pillow couldn't open it, but it is an image: damaged or an odd variant.
        raise UnsupportedMedia(f"{path.name} looks like a damaged or unsupported image")
    if codec not in VIDEO_CODECS:
        raise UnsupportedMedia(f"{path.name}: video codec '{codec}' is not supported")
    if duration <= 0:
        raise UnsupportedMedia(f"{path.name} has no duration")

    w, h = int(video.get("width", 0)), int(video.get("height", 0))
    if _rotation(video) in (90, 270):
        w, h = h, w
    when, reliable = _video_capture_time(tags)
    return MediaInfo(
        path=str(path), content_hash=digest, kind="video", width=w, height=h,
        duration=duration, fps=_fps(video),
        is_hdr=video.get("color_transfer") in HDR_TRANSFERS,
        has_audio=audio is not None, capture_time=when, capture_time_reliable=reliable,
    )


def _rotation(stream: dict) -> int:
    rot = 0
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if not rot:
        rot = int(float((stream.get("tags") or {}).get("rotate", 0)))
    return abs(rot) % 360


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(stream.get(key, "0/1")).partition("/")
        try:
            if float(den or 1):
                return float(num) / float(den or 1)
        except ValueError:
            continue
    return 0.0


def _video_capture_time(tags: dict) -> tuple[str | None, bool]:
    # Apple's creationdate carries the local offset; creation_time is UTC.
    for key in ("com.apple.quicktime.creationdate", "creation_time"):
        raw = tags.get(key)
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Many apps (WhatsApp, editors) write 1970/1904 or "now" placeholders.
        if when.year < 2000:
            continue
        if when.tzinfo:
            when = when.astimezone(timezone.utc)
        return when.isoformat(), True
    return None, False
