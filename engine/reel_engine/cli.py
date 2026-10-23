"""Command line: `reel song`, `reel make`, `reel render`."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import ffmpeg, songs
from .edl import EDL
from .pipeline import list_media, make_variants, prepare
from .planner import presets
from .render import render

DEFAULT_CACHE = Path(".reel_cache")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reel", description="Make beat-synced vertical reels.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("song", help="find a song from a YouTube link or name; download its preview")
    s.add_argument("query", help="YouTube link or song name")
    s.add_argument("--pick", type=int, help="download result number N (1-based)")
    s.add_argument("--out", type=Path, default=Path("songs"))

    m = sub.add_parser("make", help="make reels from a folder of photos/videos")
    m.add_argument("media", nargs="+", type=Path, help="folder(s) or files")
    song = m.add_mutually_exclusive_group()
    song.add_argument("--song", type=Path, help="audio file (or a preview from `reel song`)")
    song.add_argument("--song-search", metavar="QUERY",
                      help="YouTube link or song name; uses the top match's preview")
    m.add_argument("--length", type=float, default=15.0, help="target seconds (default 15)")
    preset_names = [k for k in presets() if k != "selection"]
    m.add_argument("--preset", default="cinematic", choices=[*preset_names, "all"])
    m.add_argument("--seed", type=int, default=0, help="change for a different variation")
    m.add_argument("--music-start", type=float, help="force the song start (seconds)")
    m.add_argument("--out", type=Path, default=Path("out"))
    m.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    m.add_argument("--plan-only", action="store_true", help="write EDLs without rendering")

    r = sub.add_parser("render", help="render a saved EDL")
    r.add_argument("edl", type=Path)
    r.add_argument("--out", type=Path, default=Path("out"))
    r.add_argument("--cache", type=Path, default=DEFAULT_CACHE)

    u = sub.add_parser("ui", help="open the web app in your browser")
    u.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 lets phones on your Wi-Fi connect (anyone on it can)")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--data", type=Path, default=Path("reel-data"),
                   help="where uploads, cache and reels are kept")
    u.add_argument("--keep-hours", type=float, default=1,
                   help="how long finished reels stay downloadable (default 1)")
    u.add_argument("--no-browser", action="store_true")

    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        commands = {"song": cmd_song, "make": cmd_make, "render": cmd_render, "ui": cmd_ui}
        return commands[args.cmd](args)
    except (ValueError, ffmpeg.FFmpegError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def cmd_song(args) -> int:
    tracks = songs.search(args.query)
    if not tracks:
        print("No matches. Try typing 'artist - song'.")
        return 1
    if args.pick is None:
        for i, t in enumerate(tracks, 1):
            print(f"{i}. {t.artist} - {t.title} ({t.duration_seconds:.0f}s)")
        print("\nRun again with --pick N to download that preview.")
        return 0
    if not 1 <= args.pick <= len(tracks):
        print(f"--pick must be between 1 and {len(tracks)}", file=sys.stderr)
        return 1
    path = songs.download_preview(tracks[args.pick - 1], args.out)
    print(f"saved {path}")
    return 0


def cmd_make(args) -> int:
    t0 = time.perf_counter()
    song = args.song
    if args.song_search:
        tracks = songs.search(args.song_search)
        if not tracks:
            raise ValueError("no song found for that search")
        t = tracks[0]
        print(f"song: {t.artist} - {t.title} (30 s preview)")
        song = songs.download_preview(t, args.cache / "songs")
    if song and not song.exists():
        raise ValueError(f"song not found: {song}")
    if song is None:
        print("note: no song given; cutting to a 120 BPM grid with no audio track")

    files = list_media(args.media)
    if not files:
        raise ValueError("no media files found")
    print(f"preparing {len(files)} files...")
    prepared = prepare(files, args.cache)
    for s in prepared.skipped:
        print(f"  skipped {s}")
    if not prepared.assets:
        raise ValueError("none of the files could be used")

    names = [k for k in presets() if k != "selection"] if args.preset == "all" else [args.preset]
    results = make_variants(prepared, song, args.out, args.cache, args.length, names,
                            args.seed, args.music_start, args.plan_only)
    print()
    for r in results:
        for n in r.edl.notes:
            print(f"note: {n}")
        if r.render:
            files = [f for f in (r.render.with_music, r.render.silent) if f]
            print(f"{r.preset}: " + ", ".join(str(f) for f in files))
        else:
            print(f"{r.preset}: plan written to {r.edl_path}")
    if results and results[0].edl.music:
        print(f"\n{results[0].edl.music.instagram_hint}")
    print(f"done in {time.perf_counter() - t0:.1f}s")
    return 0


def cmd_render(args) -> int:
    edl = EDL.load(args.edl)
    result = render(edl, args.out, args.cache / "render", name=edl.preset)
    print(f"rendered {result.with_music or result.silent}")
    return 0


def cmd_ui(args) -> int:
    import threading
    import webbrowser

    import uvicorn

    from .webapp import create_app

    if not ffmpeg.available():
        raise ValueError("FFmpeg not found; see README.md")
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}"
    busy = _port_owner(args.port)
    if busy == "reel":
        print(f"Reel Maker is already running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    if busy == "other":
        raise ValueError(f"port {args.port} is used by another program; "
                         f"try `reel ui --port {args.port + 1}`")
    app = create_app(args.data.resolve(), result_ttl_hours=args.keep_hours)
    print(f"Reel Maker running at {url}  (Ctrl+C to stop)")
    if args.host == "0.0.0.0":
        print("Phones on the same Wi-Fi can open http://<this PC's IP>:" + str(args.port))
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _port_owner(port: int) -> str | None:
    """None if the port is free, "reel" if Reel Maker already serves it, else "other"."""
    import json
    import socket
    import urllib.request

    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=2) as r:
            return "reel" if "presets" in json.loads(r.read()) else "other"
    except (OSError, ValueError):
        return "other"


if __name__ == "__main__":
    sys.exit(main())
