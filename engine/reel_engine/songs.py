"""Song lookup: YouTube link or song name -> official ~30 s preview clip.

Nothing is downloaded from YouTube. A link is only used to read the video title
through YouTube's public oEmbed endpoint; the audio comes from the iTunes Search
API's preview clips.
"""

from __future__ import annotations

import difflib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

OEMBED_URL = "https://www.youtube.com/oembed"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ALLOWED_PREVIEW_HOSTS = ("audio-ssl.itunes.apple.com", ".mzstatic.com")
TIMEOUT = 10
MAX_PREVIEW_BYTES = 5 * 1024 * 1024
USER_AGENT = "family-reel-generator/0.1"

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

# Noise commonly found in YouTube music video titles.
_NOISE = re.compile(
    r"""[\(\[][^\)\]]*(official|video|audio|lyric|lyrics|visualizer|hd|4k|hq|mv|m/v|
        remaster(ed)?|live|full\s+song|explicit|clean)[^\)\]]*[\)\]]""",
    re.IGNORECASE | re.VERBOSE,
)
_FEAT = re.compile(r"\s+(ft\.?|feat\.?|featuring)\s+.*$", re.IGNORECASE)
_VERSION_WORDS = ("remix", "live", "acoustic", "cover", "instrumental", "karaoke",
                  "sped up", "slowed", "version", "edit", "mix")
_CHANNEL_SUFFIX = re.compile(r"(vevo|official|\s+-\s+topic|music)$", re.IGNORECASE)
# Noise outside brackets, common in Indian film-song uploads ("... Full Song | Singer | Film").
_BARE_NOISE = re.compile(
    r"\b(full\s+(video\s+)?song|video\s+song|full\s+video|official\s+(music\s+)?video|"
    r"lyrical(\s+video)?|lyrics?|audio\s+song|hd|4k)\b", re.IGNORECASE)
# Label/compilation channels ("Dard Bhare Songs", "Sony Music India") aren't artists.
_GENERIC_CHANNEL = re.compile(r"(songs|music|records|hits|films|entertainment|series|tv)\b",
                              re.IGNORECASE)
_NOT_A_NAME = re.compile(r"\d|song|video|hindi|punjabi|tamil|telugu|lyrics|audio|hd", re.IGNORECASE)
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
WATCH_PAGE_BYTES = 1_500_000


@dataclass
class Track:
    title: str
    artist: str
    preview_url: str
    artwork_url: str
    duration_seconds: float
    source: str = "itunes_preview"

    def to_dict(self) -> dict:
        return asdict(self)


def is_youtube_url(text: str) -> bool:
    try:
        host = urllib.parse.urlparse(text.strip()).hostname or ""
    except ValueError:
        return False
    return host.lower() in YOUTUBE_HOSTS


def clean_title(title: str, channel: str = "") -> tuple[str, str]:
    """Turn a YouTube title into (artist, song).

    'The Weeknd - Blinding Lights (Official Video)' -> ('The Weeknd', 'Blinding Lights')
    'Tujhe Suraj Kahun Ya Chanda Full Song | Manna Dey | Ek Phool Do Mali'
        -> ('Manna Dey', 'Tujhe Suraj Kahun Ya Chanda')
    """
    t = _NOISE.sub("", title)
    parts = [p.strip() for p in t.split("|") if p.strip()]
    head = parts[0] if parts else t.strip()
    artist, song = "", head
    for sep in (" - ", " – ", " — "):
        if sep in head:
            artist, song = (p.strip() for p in head.split(sep, 1))
            break
    if not artist and len(parts) > 1 and _looks_like_name(parts[1]):
        artist = parts[1]
    if not artist and channel and not _GENERIC_CHANNEL.search(channel):
        artist = _CHANNEL_SUFFIX.sub("", channel.strip()).strip()
    song = re.sub(r"\s{2,}", " ", _BARE_NOISE.sub("", song))
    song = _FEAT.sub("", song).strip(" \"'-–—")
    artist = _FEAT.sub("", artist).strip()
    return artist, song


def _looks_like_name(text: str) -> bool:
    return len(text) <= 40 and len(text.split()) <= 4 and not _NOT_A_NAME.search(text)


def youtube_id(url: str) -> str | None:
    try:
        u = urllib.parse.urlparse(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif u.path.startswith(("/shorts/", "/live/", "/embed/")):
        vid = u.path.split("/")[2]
    else:
        vid = urllib.parse.parse_qs(u.query).get("v", [""])[0]
    return vid if _VIDEO_ID.match(vid or "") else None


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def youtube_title(url: str) -> tuple[str, str]:
    """Return (title, channel) for a YouTube link. No media is downloaded.

    Uses oEmbed; videos with embedding disabled answer 401/403 there, so fall back
    to the public watch page's title tags.
    """
    vid = youtube_id(url)
    if not vid:
        raise ValueError("that doesn't look like a YouTube video link")
    watch = f"https://www.youtube.com/watch?v={vid}"
    try:
        q = urllib.parse.urlencode({"url": watch, "format": "json"})
        data = _get_json(f"{OEMBED_URL}?{q}")
        return data.get("title", ""), data.get("author_name", "")
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403, 404):
            raise
    req = urllib.request.Request(watch, headers={"User-Agent": USER_AGENT,
                                                 "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        page = resp.read(WATCH_PAGE_BYTES).decode("utf-8", "replace")
    title = (_meta(page, r'<meta name="title" content="([^"]*)"')
             or _meta(page, r'<meta property="og:title" content="([^"]*)"'))
    if not title:
        raise ValueError("couldn't read that video's title; type the song name instead")
    return title, _meta(page, r'<link itemprop="name" content="([^"]*)"')


def _meta(page: str, pattern: str) -> str:
    m = re.search(pattern, page)
    return html.unescape(m.group(1)).strip() if m else ""


def search(query: str, limit: int = 5, country: str = "US") -> list[Track]:
    """Search for tracks. `query` may be a YouTube link or free text."""
    query = query.strip()
    fallback = ""
    if is_youtube_url(query):
        title, channel = youtube_title(query)
        artist, song = clean_title(title, channel)
        query, fallback = f"{artist} {song}".strip(), song
    if not query:
        return []
    tracks = _itunes(query, limit, country)
    if not tracks and fallback and fallback != query:
        tracks = _itunes(fallback, limit, country)  # the guessed artist may be wrong
    return rank(tracks, query)


def _itunes(query: str, limit: int, country: str) -> list[Track]:
    q = urllib.parse.urlencode({
        "term": query, "entity": "song", "media": "music",
        "limit": limit, "country": country,
    })
    data = _get_json(f"{ITUNES_SEARCH_URL}?{q}")
    tracks = []
    for r in data.get("results", []):
        if not r.get("previewUrl"):
            continue
        tracks.append(Track(
            title=r.get("trackName", ""),
            artist=r.get("artistName", ""),
            preview_url=r["previewUrl"],
            artwork_url=(r.get("artworkUrl100") or "").replace("100x100", "600x600"),
            duration_seconds=round((r.get("trackTimeMillis") or 0) / 1000, 1),
        ))
    return tracks


def rank(tracks: list[Track], query: str) -> list[Track]:
    """Prefer the original recording: penalize remix/live/etc. unless the query asks."""
    q = query.lower()

    def key(t: Track) -> tuple:
        title = t.title.lower()
        variant = any(w in title and w not in q for w in _VERSION_WORDS)
        sim = difflib.SequenceMatcher(None, q, f"{t.artist} {t.title}".lower()).ratio()
        return (variant, -round(sim, 2))

    return sorted(tracks, key=key)  # stable: ties keep the API's order


def download_preview(track: Track, out_dir: Path) -> Path:
    """Save the preview clip plus a sidecar JSON so the CLI knows it's a preview."""
    host = urllib.parse.urlparse(track.preview_url).hostname or ""
    if urllib.parse.urlparse(track.preview_url).scheme != "https" or not any(
        host == h or (h.startswith(".") and host.endswith(h)) for h in ALLOWED_PREVIEW_HOSTS
    ):
        raise ValueError(f"unexpected preview host: {host}")
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]+", "-", f"{track.artist}-{track.title}").strip("-")[:60]
    out = out_dir / f"{safe or 'song'}.m4a"
    req = urllib.request.Request(track.preview_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_PREVIEW_BYTES + 1)
    if len(data) > MAX_PREVIEW_BYTES:
        raise ValueError("preview is unexpectedly large")
    out.write_bytes(data)
    sidecar(out).write_text(json.dumps(track.to_dict(), indent=2))
    return out


def sidecar(audio: Path) -> Path:
    return audio.with_suffix(audio.suffix + ".json")


def load_sidecar(audio: Path) -> Track | None:
    p = sidecar(audio)
    if not p.exists():
        return None
    try:
        return Track(**json.loads(p.read_text()))
    except (TypeError, ValueError):
        return None
