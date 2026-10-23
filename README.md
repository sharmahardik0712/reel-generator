# Family Reel Generator

Turns a folder of phone photos and videos plus a song into short, beat-synced
vertical reels (1080×1920, H.264/AAC) ready for Instagram. Deterministic, CPU only,
no AI. See [docs/plan.md](docs/plan.md) for the full plan.

**Status:** Milestone 1 (CLI engine) is done. The Cloudflare backend (M3) and
mobile web app (M4) aren't built yet.

## Setup (Windows)

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -e "engine[dev]"
```

FFmpeg: put `ffmpeg.exe` and `ffprobe.exe` in `tools/ffmpeg/` (the gyan.dev
"essentials" build includes `zscale`, needed for HDR), or have them on `PATH`,
or set `REEL_FFMPEG` / `REEL_FFPROBE`.

## Web app

```bash
.venv/Scripts/reel ui
```

This opens http://localhost:8765. Add photos and videos, search for a song (YouTube
link or name) or upload one, pick a length and styles, then **Make my reels**.
That opens the result screen (`/r/<id>`): progress, then each reel with Download,
Silent and Share buttons.

Storage is short-lived, the same way it will work on Cloudflare:

- **Uploads and the song** (`reel-data/projects/`) are deleted as soon as the reels
  are made. If rendering fails they're kept, so you can go back and retry.
- **Working files** (`reel-data/work/`) are deleted when each render ends.
- **Finished reels** (`reel-data/results/`) stay downloadable for 1 hour (`--keep-hours N` to
  change), or until you press *Delete these reels now*.
- Drafts you never render are deleted after 24 hours. Cleanup runs at startup and
  every 5 minutes.

To put it on the internet for free (workers.dev + Cloudflare Tunnel), see [DEPLOY.md](DEPLOY.md).

To use it from phones on the same Wi-Fi: `reel ui --host 0.0.0.0`, then open
`http://<this PC's IP>:8765` on the phone. Anyone on that Wi-Fi can reach it, and
Windows may ask to allow it through the firewall.

## Command line

Find a song from a YouTube link or a name. Nothing is downloaded from YouTube:
the link is only used to read the title, and the audio is the official 30 s
iTunes preview.

```bash
.venv/Scripts/reel song "https://www.youtube.com/watch?v=4NRXx6U8ABQ"
```

Make reels (all three presets, 15 s, song found automatically):

```bash
.venv/Scripts/reel make path/to/trip --song-search "blinding lights" --preset all --out out
```

Use your own audio file instead with `--song song.mp3`. Other options:
`--length 7|15|30|60`, `--seed N` (shuffle), `--music-start SECONDS`, `--plan-only`
(write EDLs without rendering). `reel render out/hype.edl.json` re-renders a saved plan.

Each preset writes `<preset>.mp4` (with music), `<preset>-silent.mp4` (post this and
add the song in Instagram so it isn't muted), `<preset>.edl.json`, and `README.txt`
with the Instagram instructions and any notes.

## How it works

`engine/reel_engine/`:

| Module | Role |
|---|---|
| `probe.py` | Detect photo/video/audio from content; dimensions, rotation, HDR, capture time |
| `proxy.py` | HEIC decode, sRGB, HDR→SDR tone-mapping, 30 fps CFR proxies; cached by content hash |
| `analysis.py` | Sharpness, exposure, colorfulness/detail, motion, scene cuts, perceptual hash |
| `music.py` | Beats, confidence + fixed-grid fallback, bars, energy, start point at the drop |
| `songs.py` | YouTube oEmbed title → iTunes Search preview |
| `planner.py` | Candidates → MMR selection (duplicates held back) → hook-first chronological order → beat-locked timing → EDL |
| `edl.py` | The edit-decision list format and its validator |
| `render.py` | Per-clip render (cached, parallel), concat, loudness-normalized music, ffprobe validation |
| `presets.yaml` | Pacing and scoring weights for hype / smooth / cinematic |

## Tests

```bash
.venv/Scripts/python -m pytest engine/tests            # everything (~2 min)
.venv/Scripts/python -m pytest engine/tests -m "not ffmpeg and not network"   # fast unit tests
```

The end-to-end tests generate phone-like media (HEIC, rotated video, HLG HDR HEVC,
duplicates, a corrupt file) and a synthetic song with a drop. `tests/golden/` holds
an EDL snapshot; after an intended planner change, regenerate with `UPDATE_GOLDEN=1`.

## Not done yet (next milestones)

- M2: crossfades/flash transitions, punch-ins, speed ramps, LUTs, saliency crop,
  loopable ending, energy matching.
- M0/M3: Cloudflare spike and backend (Workers, Access, R2, D1, Queues, Containers).
- M4: mobile web app.
