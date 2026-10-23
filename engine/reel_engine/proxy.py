"""Proxies: normalized working copies that every later stage reads instead of originals.

Video -> 30 fps CFR, SDR Rec.709, H.264, sized to cover a 1080x1920 frame, rotation applied.
Photo -> sRGB JPEG with orientation applied, sized for a 9:16 crop with zoom headroom.
Everything is cached under <cache>/<content_hash>/ so repeated runs and shuffles skip it.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from . import FPS, OUTPUT_HEIGHT, OUTPUT_WIDTH, ffmpeg
from .probe import MediaInfo

log = logging.getLogger(__name__)

PHOTO_HEADROOM = 1.25  # room for Ken Burns zoom/pan without upscaling
MAX_PHOTO_SIDE = 4096


def asset_dir(cache: Path, info: MediaInfo) -> Path:
    d = cache / info.content_hash[:32]
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_proxy(info: MediaInfo, cache: Path) -> Path:
    if info.kind == "photo":
        return _photo_proxy(info, cache)
    if info.kind == "video":
        return _video_proxy(info, cache)
    raise ValueError(f"no proxy for kind {info.kind}")


def thumbnail_path(info: MediaInfo, cache: Path) -> Path:
    return asset_dir(cache, info) / "thumb.jpg"


# --- photos -----------------------------------------------------------------

def _photo_proxy(info: MediaInfo, cache: Path) -> Path:
    out = asset_dir(cache, info) / "proxy.jpg"
    if out.exists():
        return out
    with Image.open(info.path) as im:
        im = ImageOps.exif_transpose(im)
        im = _to_srgb(im)
        w, h = im.size
        cover = max(OUTPUT_WIDTH * PHOTO_HEADROOM / w, OUTPUT_HEIGHT * PHOTO_HEADROOM / h)
        scale = min(1.0, cover, MAX_PHOTO_SIDE / max(w, h))
        if scale < 1.0:
            im = im.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
        tmp = out.with_suffix(".tmp.jpg")
        im.save(tmp, "JPEG", quality=92, subsampling=0)
        _save_thumb(im, thumbnail_path(info, cache))
    tmp.replace(out)
    return out


def _to_srgb(im: Image.Image) -> Image.Image:
    """Convert embedded color profiles (e.g. iPhone Display P3) to sRGB."""
    icc = im.info.get("icc_profile")
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            dst = ImageCms.createProfile("sRGB")
            im = ImageCms.profileToProfile(im.convert("RGB"), src, dst, outputMode="RGB")
        except (ImageCms.PyCMSError, OSError) as e:
            log.debug("ICC conversion skipped: %s", e)
    if im.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", im.size, (0, 0, 0))
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    return im.convert("RGB")


def _save_thumb(im: Image.Image, path: Path) -> None:
    t = im.copy()
    t.thumbnail((320, 320))
    t.save(path, "JPEG", quality=80)


# --- videos -----------------------------------------------------------------

def video_filter(info: MediaInfo) -> str:
    steps: list[str] = []
    if info.is_hdr:
        if ffmpeg.has_filter("zscale"):
            # Linearize, tone-map to SDR, convert to BT.709.
            steps += [
                "zscale=t=linear:npl=100",
                "format=gbrpf32le",
                "zscale=p=bt709",
                "tonemap=tonemap=hable:desat=0",
                "zscale=t=bt709:m=bt709:r=tv",
            ]
        else:
            log.warning("ffmpeg lacks zscale; HDR video %s will look washed out",
                        Path(info.path).name)
            steps.append("scale=in_color_matrix=bt2020:out_color_matrix=bt709")
    steps += [
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase"
        ":force_divisible_by=2:flags=bicubic",
        f"fps={FPS}",
        "setsar=1",
        "format=yuv420p",
    ]
    return ",".join(steps)


def _video_proxy(info: MediaInfo, cache: Path) -> Path:
    d = asset_dir(cache, info)
    out = d / "proxy.mp4"
    if out.exists():
        return out
    tmp = d / "proxy.tmp.mp4"
    ffmpeg.run([
        *ffmpeg.inp(info.path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", video_filter(info),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-g", str(FPS // 2), "-bf", "0",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-map_metadata", "-1", "-movflags", "+faststart",
        tmp,
    ])
    tmp.replace(out)
    ffmpeg.run([
        *ffmpeg.inp(out, "-ss", f"{min(1.0, info.duration / 2):.3f}"),
        "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", thumbnail_path(info, cache),
    ])
    return out
