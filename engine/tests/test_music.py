import numpy as np

from conftest import SR, make_song
from reel_engine.music import analyze_signal, beat_confidence, format_timestamp, plan_music


def test_beat_confidence_regular_vs_irregular():
    regular = list(np.arange(0, 20, 0.5))
    rng = np.random.default_rng(0)
    irregular = list(np.cumsum(rng.uniform(0.2, 0.9, 40)))
    assert beat_confidence(regular) > 0.95
    assert beat_confidence(irregular) < 0.45
    assert beat_confidence([0.0, 0.5]) == 0.0


def test_analysis_finds_tempo_and_drop():
    m = analyze_signal(make_song(seconds=30, bpm=120, drop_at=10), SR)
    assert abs(m.bpm - 120) < 3 or abs(m.bpm - 60) < 2  # half-time is acceptable
    assert m.confidence > 0.6 and not m.synthetic_grid
    plan = plan_music(m, target=15)
    # The quiet intro ends at 10 s; the reel's music should start at or near the drop.
    assert 7.5 <= plan.start <= 12.5, plan.start
    assert plan.length == 15
    assert plan.beats[0] >= 0 and plan.beats[-1] < 15 + 2.0  # a few beats past the end
    # Starts on a bar line.
    assert any(abs(plan.start - d) < 1e-3 for d in m.downbeats)


def test_short_song_limits_length():
    m = analyze_signal(make_song(seconds=12, drop_at=0), SR)
    plan = plan_music(m, target=30)
    assert plan.length < 12.5


def test_format_timestamp():
    assert format_timestamp(42.4) == "0:42"
    assert format_timestamp(75) == "1:15"
