"""Real FFmpeg runs on generated phone-like media."""

import json

import cv2
import numpy as np
import pytest

from reel_engine import ffmpeg
from reel_engine.analysis import hamming, read_frames
from reel_engine.pipeline import list_media, make_variants, prepare
from reel_engine.probe import probe_file

pytestmark = pytest.mark.ffmpeg


@pytest.fixture(scope="module")
def prepared(media_dir, tmp_path_factory):
    cache = tmp_path_factory.mktemp("cache")
    p = prepare(list_media([media_dir]), cache, progress=lambda *_: None)
    return p, cache


def test_probe_handles_phone_formats(media_dir):
    heic = probe_file(media_dir / "04_iphone.heic")
    assert heic.kind == "photo" and (heic.width, heic.height) == (1200, 1600)
    rotated = probe_file(media_dir / "08_rotated.mp4")
    assert rotated.kind == "video" and rotated.height > rotated.width
    hdr = media_dir / "09_hdr_landscape.mov"
    if hdr.exists():
        assert probe_file(hdr).is_hdr


def test_prepare_skips_bad_files_and_keeps_good_ones(prepared):
    p, _ = prepared
    names = {a.info.path.replace("\\", "/").rsplit("/", 1)[-1] for a in p.assets}
    assert {"01_beach.jpg", "04_iphone.heic", "06_portrait.mp4", "08_rotated.mp4"} <= names
    assert "10_notes.txt" not in names  # filtered by extension
    assert any("11_corrupt.jpg" in s for s in p.skipped)
    assert "12_exact_copy.jpg" not in names
    assert any("12_exact_copy.jpg: same file as 01_beach.jpg" in s for s in p.skipped)


def test_hdr_proxy_is_tone_mapped(prepared):
    p, _ = prepared
    hdr = [a for a in p.assets if a.info.is_hdr]
    if not hdr:
        pytest.skip("libx265 not available to create an HDR sample")
    probe = ffmpeg.probe(hdr[0].proxy)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert v.get("color_transfer") == "bt709"
    frames = read_frames(hdr[0].proxy, landscape=True, fps=1)
    gray = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
    # Washed-out (untone-mapped) HLG bars come out very flat and grey.
    assert gray.std() > 40


def test_duplicate_photos_share_a_hash(prepared):
    p, _ = prepared
    by_name = {a.info.path.replace("\\", "/").rsplit("/", 1)[-1]: a for a in p.assets}
    a, b = by_name["01_beach.jpg"], by_name["02_beach_again.jpg"]
    assert hamming(a.analysis.phash, b.analysis.phash) <= 10  # counts as a near-duplicate
    assert by_name["05_blurry.jpg"].analysis.sharpness < a.analysis.sharpness


def test_make_all_presets_with_music(prepared, song_file, tmp_path):
    p, cache = prepared
    results = make_variants(p, song_file, tmp_path, cache, length=10,
                            preset_names=["hype", "smooth", "cinematic"], progress=lambda *_: None)
    assert len(results) == 3
    for r in results:
        rep = r.render.report
        assert rep["with_music"]["video"] == "h264 1080x1920"
        assert rep["with_music"]["audio"] == "aac"
        assert rep["silent"]["audio"] is None
        assert abs(rep["with_music"]["duration"] - r.edl.output.duration_seconds) <= 0.1
        assert abs(r.edl.output.duration_seconds - 10) <= 0.3
        used = {c.asset_id for c in r.edl.clips}
        dup_ids = {a.id for a in p.assets
                   if a.info.path.endswith(("01_beach.jpg", "02_beach_again.jpg"))}
        assert not dup_ids <= used, "both duplicate photos were used"
    readme = (tmp_path / "README.txt").read_text(encoding="utf-8")
    assert "Instagram" in readme
    assert json.loads((tmp_path / "hype.edl.json").read_text())["music"]["bpm"] > 0


def test_render_has_no_black_frames(prepared, song_file, tmp_path):
    p, cache = prepared
    r = make_variants(p, song_file, tmp_path, cache, length=7, preset_names=["hype"],
                      progress=lambda *_: None)[0]
    frames = read_frames(r.render.silent, landscape=False, fps=10)
    means = [float(f.mean()) for f in frames]
    assert min(means) > 10, "found a near-black frame"
    assert len(frames) >= 65


def test_rerender_reuses_clip_cache(prepared, song_file, tmp_path):
    p, cache = prepared
    make_variants(p, song_file, tmp_path / "a", cache, length=7, preset_names=["smooth"],
                  progress=lambda *_: None)
    clips = sorted((cache / "render" / "clips").glob("*.mp4"))
    mtimes = [c.stat().st_mtime for c in clips]
    make_variants(p, song_file, tmp_path / "b", cache, length=7, preset_names=["smooth"],
                  progress=lambda *_: None)
    assert [c.stat().st_mtime for c in clips] == mtimes
