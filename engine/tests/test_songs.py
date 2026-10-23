import pytest

from reel_engine import songs
from reel_engine.songs import Track, clean_title, is_youtube_url, rank


@pytest.mark.parametrize("title,channel,expected", [
    ("The Weeknd - Blinding Lights (Official Video)", "TheWeekndVEVO", ("The Weeknd", "Blinding Lights")),
    ("Dua Lipa - Levitating Featuring DaBaby (Official Music Video)", "Dua Lipa", ("Dua Lipa", "Levitating")),
    ("Kesariya [Lyrics] | Brahmastra", "Sony Music India", ("Brahmastra", "Kesariya")),
    ("Tujhe Suraj Kahun Ya Chanda Full Song | Manna Dey | Balraj Sahni | Ek Phool Do Mali | Hindi Song",
     "Dard Bhare Songs", ("Manna Dey", "Tujhe Suraj Kahun Ya Chanda")),
    ("Channa Mereya Full Video Song", "T-Series", ("", "Channa Mereya")),  # a label, not an artist
    ("Shape of You", "Ed Sheeran", ("Ed Sheeran", "Shape of You")),
    ("Calm Down", "Rema - Topic", ("Rema", "Calm Down")),
])
def test_clean_title(title, channel, expected):
    assert clean_title(title, channel) == expected


@pytest.mark.parametrize("url,vid", [
    ("https://youtu.be/wzfcvDPDxfw?si=BZeeu4OKWbq_dfMU", "wzfcvDPDxfw"),
    ("https://www.youtube.com/watch?v=4NRXx6U8ABQ&t=10", "4NRXx6U8ABQ"),
    ("https://youtube.com/shorts/abcdefghijk", "abcdefghijk"),
    ("https://www.youtube.com/watch?v=bad", None),
])
def test_youtube_id(url, vid):
    assert songs.youtube_id(url) == vid


def test_is_youtube_url():
    assert is_youtube_url("https://www.youtube.com/watch?v=4NRXx6U8ABQ")
    assert is_youtube_url("https://youtu.be/4NRXx6U8ABQ")
    assert not is_youtube_url("blinding lights")
    assert not is_youtube_url("https://evil.example/youtube.com")


def _t(title, artist="A"):
    return Track(title, artist, "https://audio-ssl.itunes.apple.com/x.m4a", "", 200)


def test_rank_prefers_original_unless_asked():
    tracks = [_t("Song (Remix)"), _t("Song"), _t("Song (Live)")]
    assert rank(tracks, "a song")[0].title == "Song"
    assert rank(tracks, "a song remix")[0].title == "Song (Remix)"


def test_download_rejects_unexpected_hosts(tmp_path):
    bad = Track("x", "y", "https://evil.example/file.m4a", "", 30)
    with pytest.raises(ValueError):
        songs.download_preview(bad, tmp_path)
    http = Track("x", "y", "http://audio-ssl.itunes.apple.com/file.m4a", "", 30)
    with pytest.raises(ValueError):
        songs.download_preview(http, tmp_path)


@pytest.mark.network
def test_live_lookup_from_youtube_link():
    tracks = songs.search("https://www.youtube.com/watch?v=4NRXx6U8ABQ")
    assert tracks and tracks[0].artist == "The Weeknd" and "Blinding Lights" in tracks[0].title
    assert tracks[0].preview_url.startswith("https://")


@pytest.mark.network
def test_live_lookup_when_embedding_is_disabled():
    # oEmbed answers 401 for this video; the watch-page fallback must still find it.
    tracks = songs.search("https://youtu.be/wzfcvDPDxfw?si=BZeeu4OKWbq_dfMU")
    assert tracks and tracks[0].artist == "Manna Dey"
    assert "Tujhe Suraj" in tracks[0].title
