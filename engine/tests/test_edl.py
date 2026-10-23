import copy

import pytest

from reel_engine.edl import EDL, EDL_VERSION, AssetRef, Clip, EDLError, Output, Transition, validate


def _edl() -> EDL:
    assets = {
        "photo_001": AssetRef("photo", "p.jpg", 1080, 1440, "h1", "p.jpg"),
        "video_001": AssetRef("video", "v.mp4", 1080, 1920, "h2", "v.mp4"),
    }
    clips = [
        Clip("photo_001", "photo", 0, 30),
        Clip("video_001", "video", 30, 45, source_start_seconds=1.0),
    ]
    return EDL(EDL_VERSION, "test", "hype", 0, Output(1080, 1920, 30, 75), None, assets, clips)


def test_valid_edl_round_trips(tmp_path):
    e = _edl()
    validate(e)
    p = tmp_path / "e.json"
    e.save(p)
    again = EDL.load(p)
    validate(again)
    assert again.to_dict() == e.to_dict()
    assert e.to_dict()["clips"][1]["timeline_start_seconds"] == 1.0


@pytest.mark.parametrize("mutate,message", [
    (lambda e: setattr(e.clips[1], "timeline_start_frame", 29), "expected 30"),
    (lambda e: setattr(e.output, "frames", 80), "cover 75 frames"),
    (lambda e: setattr(e.clips[0], "frame_count", 0), "frame_count"),
    (lambda e: setattr(e.clips[1], "source_start_seconds", None), "source_start"),
    (lambda e: setattr(e.clips[0], "source_start_seconds", 1.0), "photos have no"),
    (lambda e: setattr(e.clips[0], "asset_id", "nope"), "unknown asset"),
    (lambda e: setattr(e.clips[0], "transition_out", Transition("wipe", 3)), "unknown transition"),
    (lambda e: setattr(e.clips[0], "fit", "stretch"), "unknown fit"),
])
def test_invalid_edls_are_rejected(mutate, message):
    e = copy.deepcopy(_edl())
    mutate(e)
    with pytest.raises(EDLError, match=message):
        validate(e)
