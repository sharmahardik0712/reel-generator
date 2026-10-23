"""End-to-end orchestration: files -> probe -> proxy -> analysis -> plan -> render."""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import music as music_mod
from .analysis import analyze
from .edl import EDL, MusicRef
from .planner import Asset, plan, presets
from .probe import MediaInfo, UnsupportedMedia, probe_file
from .proxy import asset_dir, make_proxy
from .render import RenderResult, render
from .songs import load_sidecar

log = logging.getLogger(__name__)

MAX_FILES = 60
MAX_TOTAL_VIDEO_SECONDS = 600
SKIP_SUFFIXES = {".json", ".txt", ".md", ".db", ".ini"}


@dataclass
class Prepared:
    assets: list[Asset]
    skipped: list[str] = field(default_factory=list)


@dataclass
class VariantResult:
    preset: str
    edl: EDL
    edl_path: Path
    render: RenderResult | None


def list_media(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files += sorted((f for f in p.iterdir() if f.is_file()), key=lambda f: f.name.lower())
        elif p.is_file():
            files.append(p)
    return [f for f in files
            if not f.name.startswith(".") and f.suffix.lower() not in SKIP_SUFFIXES]


def prepare(files: list[Path], cache: Path, workers: int | None = None,
            progress=print) -> Prepared:
    """Probe, proxy and analyze each file once (cached by content hash)."""
    if len(files) > MAX_FILES:
        raise ValueError(f"too many files ({len(files)}); the limit is {MAX_FILES}")
    cache.mkdir(parents=True, exist_ok=True)
    infos: list[MediaInfo] = []
    skipped: list[str] = []
    for i, f in enumerate(files):
        try:
            info = probe_file(f, upload_index=i)
        except UnsupportedMedia as e:
            skipped.append(str(e))
            continue
        if info.kind == "audio":
            skipped.append(f"{f.name}: audio files go in --song, skipped")
            continue
        # The same file twice (byte for byte) is never wanted, and the copies would
        # share one cache entry.
        first = next((x for x in infos if x.content_hash == info.content_hash), None)
        if first:
            skipped.append(f"{f.name}: same file as {Path(first.path).name}")
            continue
        infos.append(info)
    video_total = sum(i.duration for i in infos if i.kind == "video")
    if video_total > MAX_TOTAL_VIDEO_SECONDS:
        raise ValueError(f"{video_total:.0f}s of video; the limit is {MAX_TOTAL_VIDEO_SECONDS}s")

    counters = {"photo": 0, "video": 0}
    ids = []
    for info in infos:
        counters[info.kind] += 1
        ids.append(f"{info.kind}_{counters[info.kind]:03d}")

    def work(pair: tuple[str, MediaInfo]) -> Asset | str:
        aid, info = pair
        name = Path(info.path).name
        t0 = time.perf_counter()
        try:
            proxy = make_proxy(info, cache)
            an = analyze(info, proxy, cache)
        except Exception as e:  # one bad file shouldn't sink the reel
            log.debug("prepare failed", exc_info=True)
            return f"{name}: could not process ({e.__class__.__name__}: {str(e).splitlines()[0]})"
        hdr = " HDR->SDR" if info.is_hdr else ""
        progress(f"  prepared {name} ({info.kind}{hdr}, {time.perf_counter() - t0:.1f}s)")
        return Asset(aid, info, an, str(proxy))

    workers = workers or max(1, min(4, (os.cpu_count() or 2) // 2))
    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(work, zip(ids, infos)))
    assets = [r for r in results if isinstance(r, Asset)]
    skipped += [r for r in results if isinstance(r, str)]
    return Prepared(assets, skipped)


def prepare_music(song: Path, cache: Path, target: float,
                  start: float | None = None) -> tuple[music_mod.MusicPlan, MusicRef, music_mod.MusicAnalysis]:
    info = probe_file(song)
    if info.kind not in ("audio", "video") or not info.has_audio:
        raise ValueError(f"{song.name} has no audio")
    analysis = music_mod.analyze(song, asset_dir(cache, info) / "music.json")
    mplan = music_mod.plan_music(analysis, target, start)
    track = load_sidecar(song)
    is_preview = bool(track and track.source == "itunes_preview")
    if is_preview:
        hint = (f'In Instagram, add "{track.title}" by {track.artist} and start it at the '
                f"chorus (the part this preview plays). Use the silent video.")
    else:
        hint = (f"In Instagram, add this song and set its start to "
                f"{music_mod.format_timestamp(mplan.start)}. Use the silent video.")
    ref = MusicRef(
        path=str(song), source_start_seconds=mplan.start, bpm=analysis.bpm,
        beat_confidence=analysis.confidence, fade_in_seconds=0.1, fade_out_seconds=0.9,
        is_preview=is_preview, title=track.title if track else song.stem,
        artist=track.artist if track else "", instagram_hint=hint,
    )
    return mplan, ref, analysis


def make_variants(
    prepared: Prepared,
    song: Path | None,
    out_dir: Path,
    cache: Path,
    length: float = 15.0,
    preset_names: list[str] | None = None,
    seed: int = 0,
    music_start: float | None = None,
    plan_only: bool = False,
    progress=print,
) -> list[VariantResult]:
    preset_names = preset_names or ["hype"]
    unknown = [p for p in preset_names if p not in presets() or p == "selection"]
    if unknown:
        raise ValueError(f"unknown preset(s): {', '.join(unknown)}")
    out_dir.mkdir(parents=True, exist_ok=True)

    mplan, mref = None, None
    if song:
        mplan, mref, ma = prepare_music(song, cache, length, music_start)
        grid = " (fixed grid: low beat confidence)" if ma.synthetic_grid else ""
        progress(f"  music: {ma.bpm:.0f} BPM, confidence {ma.confidence:.2f}{grid}, "
                 f"starts at {music_mod.format_timestamp(mplan.start)}")

    results = []
    for i, name in enumerate(preset_names):
        # Each style gets its own seed so the variants don't all open on the same shot.
        result = plan(prepared.assets, mplan, mref, length, name, seed + i)
        edl_path = out_dir / f"{name}.edl.json"
        result.edl.save(edl_path)
        progress(f"  planned {name}: {len(result.edl.clips)} shots, "
                 f"{result.edl.output.duration_seconds:.2f}s")
        rendered = None
        if not plan_only:
            t0 = time.perf_counter()
            rendered = render(result.edl, out_dir, cache / "render", name=name)
            progress(f"  rendered {name} in {time.perf_counter() - t0:.1f}s")
        results.append(VariantResult(name, result.edl, edl_path, rendered))

    _write_instructions(out_dir, results, prepared.skipped)
    return results


def _write_instructions(out_dir: Path, results: list[VariantResult], skipped: list[str]) -> None:
    lines = ["Your reels", "=========", ""]
    for r in results:
        lines.append(f"{r.preset}: {r.edl.output.duration_seconds:.1f}s, {len(r.edl.clips)} shots")
        if r.render:
            if r.render.with_music:
                lines.append(f"  with music : {r.render.with_music.name}")
            lines.append(f"  silent     : {r.render.silent.name}")
    music = results[0].edl.music if results else None
    if music:
        lines += ["", "Posting on Instagram", "--------------------", music.instagram_hint]
    notes = sorted({n for r in results for n in r.edl.notes})
    if notes or skipped:
        lines += ["", "Notes", "-----", *notes, *(f"Skipped {s}" for s in skipped)]
    (out_dir / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
