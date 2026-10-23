import json
import os
from pathlib import Path

import pytest

from conftest import distinct_hash, fake_photo, fake_video
from reel_engine.music import MusicPlan
from reel_engine.planner import DURATION_TOLERANCE, plan

GOLDEN = Path(__file__).parent / "golden"


def grid(bpm=120.0, length=30.0, drop_at=None, song_left=60.0) -> MusicPlan:
    step = 60.0 / bpm
    beats = [round(i * step, 4) for i in range(int(min(length + 2, song_left) / step))]
    energy = [0.9 if drop_at is None or b >= drop_at else 0.2 for b in beats]
    return MusicPlan(start=0.0, length=min(length, song_left), beats=beats, beat_energy=energy,
                     downbeat_indices=set(range(0, len(beats), 4)), available=song_left)


@pytest.mark.parametrize("preset", ["hype", "smooth", "cinematic"])
@pytest.mark.parametrize("length", [7, 15, 30])
def test_duration_and_contiguity(mixed_assets, preset, length):
    result = plan(mixed_assets, grid(), None, length, preset, seed=0)
    edl = result.edl
    assert abs(edl.output.duration_seconds - length) <= DURATION_TOLERANCE
    # validate() ran inside plan(); also check frames really are back to back.
    frame = 0
    for c in edl.clips:
        assert c.timeline_start_frame == frame
        frame += c.frame_count
    assert frame == edl.output.frames


def test_cuts_land_on_beats(mixed_assets):
    edl = plan(mixed_assets, grid(), None, 15, "hype").edl
    beat_frames = {round(b * 30) for b in grid().beats} | {edl.output.frames}
    for c in edl.clips:
        assert c.timeline_start_frame in beat_frames


def test_same_seed_same_plan_new_seed_new_plan(mixed_assets):
    a = plan(mixed_assets, grid(), None, 15, "hype", seed=1).edl.to_json()
    b = plan(mixed_assets, grid(), None, 15, "hype", seed=1).edl.to_json()
    assert a == b
    others = {plan(mixed_assets, grid(), None, 15, "hype", seed=s).edl.to_json() for s in range(2, 8)}
    assert any(o != a for o in others)


def test_presets_differ_in_pacing(mixed_assets):
    hype = plan(mixed_assets, grid(), None, 15, "hype").edl
    cine = plan(mixed_assets, grid(), None, 15, "cinematic").edl
    assert len(hype.clips) > len(cine.clips)


def test_near_duplicates_are_not_both_used(mixed_assets):
    dup_hash = distinct_hash(0)
    assets = mixed_assets + [fake_photo(20, dup_hash ^ 0b11, quality=0.99)]  # 2 bits off photo 0
    edl = plan(assets, grid(), None, 7, "smooth").edl
    used = {c.asset_id for c in edl.clips}
    assert not {"photo_000", "photo_020"} <= used


def test_hook_is_first_and_rest_is_chronological(mixed_assets):
    result = plan(mixed_assets, grid(), None, 15, "smooth")
    shots = result.shots
    assert shots[0].role == "hook"
    whens = [s.cand.when for s in shots[1:]]
    assert whens == sorted(whens)
    best_hook = max(s.cand.hook_score for s in shots)
    assert shots[0].cand.hook_score == best_hook


def test_video_segments_fit_their_source(mixed_assets):
    edl = plan(mixed_assets, grid(), None, 30, "cinematic").edl
    for c in edl.clips:
        if c.kind == "video":
            dur = 6.0
            assert c.source_start_seconds + c.frame_count / 30 <= dur + 1e-6


def test_too_little_media_gives_shorter_reel_with_note():
    assets = [fake_photo(0, distinct_hash(0)), fake_photo(1, distinct_hash(1))]
    edl = plan(assets, grid(), None, 30, "hype").edl
    assert edl.output.duration_seconds < 30
    assert any("Not enough usable media" in n for n in edl.notes)


def test_short_videos_only():
    assets = [fake_video(i, 1.2, distinct_hash(i)) for i in range(4)]
    edl = plan(assets, grid(), None, 15, "cinematic").edl
    for c in edl.clips:
        assert c.frame_count / 30 <= 1.2 + 1e-6


def test_song_ending_early_limits_reel(mixed_assets):
    edl = plan(mixed_assets, grid(length=15, song_left=9.7), None, 15, "hype").edl
    assert edl.output.duration_seconds <= 9.7 + 1e-6


def test_no_music_uses_fixed_grid(mixed_assets):
    edl = plan(mixed_assets, None, None, 15, "hype").edl
    assert abs(edl.output.duration_seconds - 15) <= DURATION_TOLERANCE


def test_golden_edl(mixed_assets):
    """Any planner change shows up as a reviewable diff of this file.
    Regenerate with UPDATE_GOLDEN=1 after an intended change."""
    edl = plan(mixed_assets, grid(drop_at=4.0), None, 15, "hype", seed=7).edl
    data = edl.to_dict()
    data["algorithm_version"] = "golden"
    path = GOLDEN / "hype_15s_seed7.json"
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    assert data == json.loads(path.read_text())
