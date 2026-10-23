# Family Reel Generator — Implementation Plan (v2)

## 1. Product overview

A private website, hosted on Cloudflare, where family members upload photos and videos from their phones, pick a song, and get back short, punchy, beat-synced vertical videos ready to post as Instagram Reels.

**Core principles**

- **Deterministic, non-AI editing.** Conventional image/video processing, audio DSP, heuristics, and FFmpeg. No generative AI or vision-language models.
- **CPU only.** No GPU anywhere in the stack.
- **Optimized for engagement, not just validity.** The default preset is built around what holds attention in short-form video: a strong hook, music entering at its most energetic point, beat-synced cuts with varying pace, and a loopable ending.
- **Private and small.** Only invited family members can sign in. Media is deleted automatically after a retention period.

### Users and devices

- A handful of family members, signing in with their email addresses.
- Mixed iPhone and Android phones. The phone browser is the primary client; desktop also works.
- iPhone defaults (HEIC photos, HEVC 10-bit HDR video) are first-class inputs, not edge cases.

### User flow

1. Sign in (email one-time code via Cloudflare Access).
2. Pick photos/videos straight from the phone gallery. Uploads go directly to storage in resumable chunks.
3. Choose music: paste a YouTube link **or** type a song name. The app identifies the song and fetches its official 30 s preview (see §3.1); the user confirms the match by cover art and a play button. Alternatively upload an audio file (required for 60 s reels) or pick from a small royalty-free library.
4. Choose length: **7–15 s (default)**, 30 s, or 60 s.
5. Tap **Make my Reel**. Media is converted to proxies and analyzed once.
6. See a **storyboard within seconds**, then **three rendered variants** side by side (Hype / Smooth / Cinematic).
7. Optionally lock or remove clips, or tap **Shuffle** on the storyboard for a new variation (before rendering).
8. **Share** straight to Instagram/Photos via the phone's share sheet, or download.
   - **With-music** MP4, for preview or other platforms.
   - **Silent, beat-synced** MP4 plus an instruction such as "add *Song X* in Instagram starting at 0:42", so the reel uses Instagram's licensed audio and isn't muted.
9. Tapping Generate opens a separate **result screen** (`/r/<job>`): live progress, then the reels with Download / Silent / Share. As soon as the render succeeds, **all uploaded media, the song and working files are deleted**; only the finished MP4s remain, for **20 minutes** (or until the user taps *Delete these reels now*). If a render fails, the uploads are kept so the user can go back and retry.

### Success criteria

- Phone uploads of mixed photos/videos (including HEIC and HDR HEVC) produce a valid 1080×1920 MP4 with correct colors and orientation.
- The first 1.5 s contains a strong shot; cuts land on beats; music enters at a high-energy section.
- Near-duplicates are not repeated when alternatives exist; blurry or shaky segments are rarely chosen.
- Output duration is within ±0.25 s of the target (or deliberately shorter when there isn't enough media, with an explanation).
- A storyboard appears in under 10 s after analysis; a 15 s render completes in about 1–2 minutes.
- A failed render can be retried without re-uploading; after a successful render, nothing but the reels is stored.
- Monthly Cloudflare cost stays around $5–15 for typical family use.

---

## 2. Scope

### In scope

- **Photos:** JPEG, PNG, WebP, **HEIC/HEIF**.
- **Videos:** MP4 and MOV; H.264 and **HEVC (including 10-bit HDR: HLG / Dolby Vision profile 8.4)**; variable frame rate phone footage.
- **Audio:** song lookup from a YouTube link or song name (30 s official preview); MP3, M4A/AAC, WAV uploads; a small royalty-free library.
- Chunked, resumable direct-to-storage uploads from phones.
- Target durations: 7–15 s (default), 30 s, 60 s.
- Three presets rendered together: **Hype** (default), **Smooth**, **Cinematic**.
- Storyboard preview, three variants, lock/remove clips, Shuffle (new seed).
- Optional hook text overlay in the safe zone.
- Dual export (with music / silent + Instagram start time).
- Per-person projects, a small job queue, automatic cleanup.

### Out of scope

- Generative AI or semantic scene understanding.
- Direct Instagram API publishing (the share sheet covers it).
- Public sign-up, payments, or a full manual timeline editor.
- GPU rendering or large-scale distributed rendering.

---

## 3. Architecture on Cloudflare

### Product choices

| Need | Cloudflare product | Role |
|---|---|---|
| Web app + API | **Workers + Static Assets** (TypeScript) | Serves the mobile web app (PWA) and the JSON API |
| Sign-in | **Cloudflare Access** | Email one-time-code login, allow-list of family emails (free for up to 50 users) |
| Media storage | **R2** | Originals, proxies, analysis frames, renders. No egress fees |
| Metadata | **D1** (SQLite) | Projects, assets, jobs, edit plans |
| Job queue | **Queues** | Buffers analysis/render jobs; limits concurrency |
| Media processing | **Containers** (Python + FFmpeg image) | Proxying, analysis, planning, rendering; CPU only |
| Cleanup | **Cron Triggers** | Daily deletion of expired projects |

**Requires the Workers Paid plan ($5/month)** for Containers.

### Diagram

```text
Phone browser (PWA)
  | 1. sign in via Cloudflare Access (email OTP)
  | 2. API calls ----------------------------------> Worker (API + static app)
  | 3. chunked upload direct to R2 (presigned URLs)      |  D1: projects/assets/jobs
  v                                                      |  Queues: jobs
 R2  <--------------------------------------------------+
  ^                                                      | queue consumer
  |                                                      v
  +---- read originals / write proxies, renders --- Container (Python + FFmpeg)
                                                     reel_engine:
                                                     probe -> proxy -> analyze
                                                     -> plan (EDL) -> render
                                                     -> validate
                                                     reports status back to Worker -> D1
```

### Key platform constraints and how the design handles them

- **Request body limit (100 MB on Free/Pro zones).** Phone videos exceed this, so **uploads never pass through the Worker**. The Worker issues presigned **R2 multipart upload** URLs, and the browser uploads chunks (e.g. 10 MB) directly to R2 using the S3-compatible API. This also makes uploads resumable on flaky mobile data. Configure R2 CORS for the app's origin.
- **Container disk is ephemeral.** Each job downloads what it needs from R2 into local scratch, processes, uploads results to R2, then cleans up. Nothing durable lives on the container disk.
- **Container instance size.** Use **`standard-4` (4 vCPU, 12 GiB RAM, 20 GB disk)** for render/proxy jobs. This is the largest predefined type; keep per-project limits so a job fits in 20 GB of scratch.
- **Containers run behind a Durable Object** and stop when idle. Long jobs must keep the instance alive for their duration (activity renewal / `sleepAfter` per current Containers docs). Set a hard per-job timeout (e.g. 15 minutes).
- **Cold starts.** **Pre-warm** the container when a user opens the Create screen or starts uploading, and keep it alive about 10 minutes after the last job (`sleepAfter`). It is warm before they tap Generate.
- **Work happens early.** Proxy + analysis for each asset is queued **as soon as that file's upload completes**, in parallel with the user choosing music. Generate then only plans and renders.
- **Resumable jobs.** Every stage writes its output to R2 keyed by content hash (proxy, analysis, each per-clip render). A retried job skips finished stages, so an idle shutdown or crash costs only the stage in progress. Queues retries failed jobs automatically.
- **Concurrency.** Cap containers with `max_instances` (e.g. 2) so a burst of family requests queues rather than running up cost. The UI shows queue position.
- **No YouTube downloads.** YouTube blocks datacenter IPs and downloading conflicts with its terms, so the app never downloads from YouTube. A pasted link is used only to **identify** the song (see §3.1).
- **Thumbnails.** Some Android browsers can't decode HEVC, so thumbnails are generated server-side in the proxy step; the UI shows a placeholder until they're ready.
- **Login friction.** Access session duration 30 days.

Check current Cloudflare limits and pricing before implementation; they change.

### 3.1 Song lookup (YouTube link or song name → official preview)

1. **Identify.** For a YouTube link, the Worker calls YouTube's public oEmbed endpoint (`https://www.youtube.com/oembed?url=<link>&format=json`), which returns the video title and channel without downloading media. For typed input, use the text as-is.
2. **Clean the title.** Strip noise such as `(Official Video)`, `[Lyrics]`, `ft.`, `HD`, `| Topic`, and channel suffixes like `VEVO`; split on ` - ` into artist and title.
3. **Search** the iTunes Search API (`https://itunes.apple.com/search?term=<q>&entity=song&limit=5`). Each result has `trackName`, `artistName`, `artworkUrl100`, `trackTimeMillis`, and `previewUrl` (a ~30 s AAC clip, usually around the hook/chorus). Optional secondary source: Deezer's search API (`preview` field); verify availability from the deployment region first.
4. **Confirm.** Show the top matches with cover art and a play button; the user taps the right one.
5. **Fetch** the preview into R2 and analyze it like any audio upload.

**Limits:** previews are ~30 s, so they serve 7–15 s and 30 s reels. 60 s reels need an uploaded audio file. The preview's offset within the full track is unknown, so the Instagram instruction for previews is "add *Song* in Instagram and start it at the chorus"; because tempo is constant, cuts stay on beat when the start is near a downbeat. Previews are for personal planning/preview use; posting uses Instagram's licensed audio via the silent export.

**Optional later:** a home helper (e.g. Raspberry Pi with yt-dlp behind Cloudflare Tunnel) for full-length songs, owner's call regarding YouTube's terms.

### Expected cost (rough)

Included in Workers Paid each month: 375 vCPU-minutes, 25 GiB-hours of memory, 200 GB-hours of disk. A `standard-4` render of about 2 minutes uses about 8 vCPU-minutes and 0.4 GiB-hours, so roughly **40–60 reels per month fit in the included usage**. Beyond that it costs about 1–2 cents per reel. R2 includes 10 GB storage free, and the 7-day cleanup keeps usage low. Expect **$5–15/month** total.

---

## 4. Editing engine (`reel_engine`, Python)

Three independent layers:

1. **Analysis** — measurable features of media and music.
2. **Planner** — deterministic selection, ordering, and timing → an **edit-decision list (EDL)**.
3. **Renderer** — executes the EDL with FFmpeg; makes no editorial decisions.

The engine is usable as a **CLI** (`reel make ./media --song song.m4a --length 15 --preset hype`) before any web wiring. All development and regression testing happens through the CLI.

### Stage 1 — Probe and ingest (once per asset, cached)

- Validate actual content (magic bytes + FFprobe), not extensions.
- FFprobe: streams, dimensions, duration, frame rate (detect VFR), rotation/display matrix, color transfer/primaries (detect HDR), audio presence.
- **Capture time:** EXIF `DateTimeOriginal` + `OffsetTimeOriginal` for photos; QuickTime `creationdate` (has a timezone) before `creation_time` (UTC) for videos. Flag missing or unreliable times (common for WhatsApp-forwarded media). Fall back to upload order.
- Key every cached artifact by **file content hash (SHA-256)**, so re-uploads and shuffles reuse work.

### Stage 2 — Proxies (once per asset, cached)

All analysis and rendering use proxies, never originals.

- **Video proxy:** scaled so the 9:16 crop can still fill 1080×1920 (portrait: 1080×1920; landscape: 1920 px tall, i.e. 3414×1920, or the source size if smaller), constant 30 fps, H.264, yuv420p, Rec.709 SDR, AAC 48 kHz.
  - **HDR → SDR tone-mapping:** `zscale` (linearize) → `tonemap=hable` (or `mobius`) → `zscale` to BT.709. Without this, iPhone footage looks washed out.
  - Apply rotation once here so nothing downstream handles it.
- **Photo proxy:** decode HEIC via `pillow-heif`, apply EXIF orientation, convert to sRGB, save a high-quality JPEG sized for a 9:16 crop with Ken Burns headroom (about 1.3× the output size).
- **Analysis frames:** 360 px-wide frames sampled at 2–4 fps.

### Stage 3 — Photo analysis

- Sharpness (variance of Laplacian, normalized by resolution).
- Exposure (histogram clipping, mean luminance).
- Colorfulness (Hasler–Süsstrunk metric), entropy, edge density → **visual_interest**.
- Perceptual hash (pHash) for near-duplicate clustering.
- Crop suitability for 9:16; subject location (see Stage 9).

Quality affects ranking, never hard deletion. A blurry photo can still be the only photo of grandma.

### Stage 4 — Video analysis and candidate segments

- Scene detection (PySceneDetect content detector or FFmpeg `scdet`).
- Split scenes into candidate windows of 0.5–4 s (preset-configurable).
- Per window: sharpness, exposure, **motion energy** (frame difference / optical-flow magnitude), **shake proxy** (global motion variance), stability.
- Penalize blurry, dark, or shaky windows; fall back to fixed windows if scene detection fails.
- Record the motion peak inside each window. The renderer uses it as a speed-ramp anchor.

### Stage 5 — Music analysis

Music drives the edit, so it runs **before** duration allocation.

- Beat tracking (librosa `beat_track`) and onset strength.
- **Beat confidence** (librosa provides none): regularity of inter-beat intervals plus tempogram peak strength. Below a threshold, fall back to a fixed tempo grid at the estimated BPM, or to preset pacing.
- **Bars:** group beats in fours, choosing the phase with the strongest onsets on the downbeats.
- **Energy curve:** RMS + onset strength smoothed per bar.
- **Music start point:** choose the window of the target length with the highest energy that **starts on a bar boundary**, preferring a section that begins just before an energy jump (the "drop"). This becomes `music.source_start_seconds` and the Instagram start time.
- Loudness: two-pass `loudnorm` to about **−14 LUFS integrated, −1 dBTP**.

### Stage 6 — Scoring

```text
score = w_q * quality + w_i * visual_interest + w_m * motion_fit
      + w_c * chronology_fit - redundancy_penalty - unusable_penalty
```

- Normalize every component to [0, 1]. Weights live in `configs/presets.yaml`.
- `redundancy_penalty` depends on what is already selected (same pHash cluster or scene).
- Log every component per candidate for debugging.

### Stage 7 — Selection, then ordering

1. Estimate the clip count from target length, preset pacing, and the beat grid.
2. **Select the set** greedily with **maximal marginal relevance** (score minus similarity to already-selected items) so the reel is varied.
3. **Order the set:**
   - **Hook:** the single highest "hook score" clip (high interest + high motion, or the sharpest photo) goes first, regardless of chronology.
   - **Body:** chronological when timestamps are reliable; otherwise upload order.
   - **Energy matching:** high-motion clips go into high-energy music sections (allowing small, local reorders only).
   - **Ending:** a visually strong clip whose last frame makes a clean loop back to the hook.
4. **Seed:** ties and small random perturbations are driven by an explicit seed stored in the EDL. Same media + settings + seed = identical EDL. Shuffle = new seed.

### Stage 8 — Timing (beat-locked)

- Cut points are chosen from the beat grid, not computed then snapped.
- Each preset defines beats-per-shot by energy level, e.g. **Hype:** 2 beats normally, 1 beat at the drop, occasional half-beat bursts just before it. **Cinematic:** 4–8 beats.
- Respect each video segment's usable length; never freeze-frame to fill time.
- If media cannot fill the target, produce a shorter reel ending on a bar boundary and say why.
- The final cut lands on a downbeat; total duration within ±0.25 s of the target.

### Stage 9 — Look and motion

- **Crop to 9:16:** the crop rectangle is computed in the planner and stored in the EDL.
  - v1: center crop biased by an OpenCV saliency map (spectral residual).
  - Optional: face-aware crop with OpenCV's YuNet detector. This is a small pretrained CNN; decide whether it fits the "non-AI" rule (see decision log).
  - Landscape video with no good crop: blurred-background fill (scaled copy, blurred behind a letterboxed frame).
- **Transitions:** hard cut (default for Hype), crossfade, dip-to-white flash (on drops), whip/zoom blur (sparingly). Never stack effects.
- **Punch-ins:** 100%→106% zoom over 2–4 frames on strong beats, used at most every few cuts.
- **Speed ramps:** on video segments with a motion peak, ramp slow-motion into normal speed around the peak (Hype only).
- **Ken Burns** on photos: slow zoom or pan between two crop rectangles.
- **Color:** light auto-levels per clip plus one LUT per preset, so mixed phones look consistent.
- **Original audio on video clips:** muted by default; per-clip `source_audio_gain` in the EDL, with the music ducked when a clip's own sound is kept.
- **Hook text overlay** (optional): bold sans-serif in the upper safe zone during the first 2 s.
- **Safe zones:** keep key content out of the top ~14% and bottom ~20% (covered by Instagram's interface).

### Stage 10 — Render and validate

1. **Render each clip independently** to a normalized intermediate (1080×1920, 30 fps CFR, yuv420p, identical timebase and SAR, short GOP) with its crop, motion, speed ramp, and color applied. Clips render in parallel on the container's 4 vCPU.
2. **Assemble:** `xfade` chains for transitions and the concat demuxer for hard-cut runs.
3. **Audio:** music trimmed from `source_start_seconds`, loudness-normalized, 0.1 s fade-in, fade-out on the final bar, plus any kept source audio with ducking.
4. **Encode:** libx264 (`-preset medium -crf 20`, High profile), AAC 48 kHz 192 kbps, `-movflags +faststart`. Produce both the **with-music** and **silent** files.
5. **Ken Burns implementation:** avoid `zoompan` (it jitters on fractional steps). Upscale the photo proxy first and animate a `crop` with expressions, or render motion frames in Python (Pillow) and pipe them to FFmpeg.
6. **Validate with FFprobe:** codecs, 1080×1920, duration tolerance, audio present (with-music file), non-zero size. A black-frame check (`blackdetect`) flags unexpected gaps.
7. Every FFmpeg call uses argument arrays (no shell strings), generated local paths, and `-protocol_whitelist file,pipe`.

---

## 5. Edit-decision list (EDL)

The renderer's only input. Validated with a JSON Schema before rendering.

```json
{
  "version": 2,
  "algorithm_version": "0.3.0",
  "seed": 1842,
  "preset": "hype",
  "output": { "width": 1080, "height": 1920, "fps": 30, "duration_seconds": 15.0 },
  "music": {
    "asset_id": "music_001",
    "source_start_seconds": 42.0,
    "instagram_start_label": "0:42",
    "bpm": 124.0,
    "beat_confidence": 0.86,
    "fade_in_seconds": 0.1,
    "fade_out_seconds": 0.9
  },
  "overlay": { "text": "Goa '26", "start_seconds": 0.0, "end_seconds": 2.0 },
  "clips": [
    {
      "asset_id": "video_007",
      "kind": "video",
      "timeline_start_seconds": 0.0,
      "timeline_duration_seconds": 0.968,
      "source_start_seconds": 3.40,
      "speed": [{ "at": 0.0, "rate": 0.5 }, { "at": 0.6, "rate": 1.0 }],
      "crop": { "x": 420, "y": 0, "w": 608, "h": 1080 },
      "motion": null,
      "punch_in": true,
      "source_audio_gain": 0.0,
      "transition_out": { "type": "cut", "duration_seconds": 0.0 }
    },
    {
      "asset_id": "photo_003",
      "kind": "photo",
      "timeline_start_seconds": 0.968,
      "timeline_duration_seconds": 1.935,
      "source_start_seconds": null,
      "speed": null,
      "crop": null,
      "motion": {
        "from": { "x": 0, "y": 120, "w": 1215, "h": 2160 },
        "to":   { "x": 60, "y": 200, "w": 1100, "h": 1956 }
      },
      "punch_in": false,
      "source_audio_gain": 0.0,
      "transition_out": { "type": "crossfade", "duration_seconds": 0.25 }
    }
  ]
}
```

**Transition semantics (fixed rule):** a clip's `timeline_duration_seconds` **includes** its outgoing transition. The next clip starts at `previous.start + previous.duration - previous.transition_out.duration`. The validator checks that the clips' total length minus all overlaps equals the output duration within tolerance.

---

## 6. API (Worker)

All routes sit behind Cloudflare Access. The Worker reads the verified user email from the Access JWT (`Cf-Access-Jwt-Assertion`, validated against the team's public keys) and scopes every query by it.

- `POST /api/projects` — create a project.
- `POST /api/projects/:id/uploads` — start an R2 multipart upload; returns upload ID and presigned part URLs.
- `POST /api/projects/:id/uploads/:uploadId/complete` — finish the upload; registers the asset.
- `DELETE /api/projects/:id/assets/:assetId`
- `PATCH /api/projects/:id/assets/order`
- `GET /api/songs/search?q=<youtube link or text>` — oEmbed + iTunes lookup; returns candidate tracks with artwork and preview URLs.
- `POST /api/projects/:id/music` — chosen track (preview URL), uploaded audio asset, or library track ID.
- `POST /api/warm` — pre-warm the container (called when the Create screen opens).
- `POST /api/projects/:id/prepare` — queue proxy + analysis for unprocessed assets.
- `GET /api/projects/:id/storyboard?preset=&seed=` — plan only (fast), returns the EDL with thumbnails.
- `POST /api/projects/:id/renders` — render variants `{ presets: [...], seed, locks: [...], length }`. Idempotent per (project, settings, seed).
- `GET /api/renders/:id` — status, stage, progress, queue position.
- `GET /api/renders/:id/files` — short-lived presigned R2 URLs for the with-music and silent MP4s.
- `GET /r/:renderId` — result screen (progress, then videos and downloads).
- `DELETE /api/renders/:id` — delete the finished reels now.
- `DELETE /api/projects/:id`
- Internal (container → Worker, authenticated with a shared secret): `POST /internal/jobs/:id/status`.

**Asset states:** `uploading → uploaded → preparing → ready | failed`
**Render states:** `queued → starting → planning → rendering → validating → completed | failed`

---

## 7. Data model (D1)

- **users:** `email` (from Access), `created_at`
- **projects:** `id`, `owner_email`, `created_at`, `expires_at` (draft TTL 24 h), `length_seconds`, `ordering_mode`, `hook_text`. Row and R2 objects deleted when a render succeeds.
- **assets:** `id`, `project_id`, `kind` (photo / video / music), `content_hash`, `r2_original_key`, `r2_proxy_key`, `original_filename`, `mime_type`, `size_bytes`, `width`, `height`, `duration_seconds`, `capture_time`, `capture_time_reliable`, `upload_index`, `is_hdr`, `has_audio`, `state`, `analysis_r2_key`
- **renders:** `id`, `project_id`, `preset`, `seed`, `state`, `stage`, `progress`, `error_code`, `queued_at`, `started_at`, `completed_at`, `r2_music_key`, `r2_silent_key`, `duration_seconds`
- **edit_plans:** `id`, `render_id`, `algorithm_version`, `edl_json`, `created_at`

Large analysis JSON (per-frame metrics, beat grid) lives in R2, keyed by content hash. D1 holds only pointers.

---

## 8. Frontend (mobile-first PWA)

- **Plain TypeScript + Vite** (or a small React app), served as Worker static assets. Installable to the home screen.
- **Create screen:** gallery picker (`<input type="file" multiple accept="image/*,video/*">`), thumbnail grid, per-file upload progress (Uppy with the AWS S3 multipart plugin pointed at the Worker's presign endpoints), remove/reorder.
- **Music:** paste a YouTube link, upload a file, or browse the library. Show the detected BPM and the chosen start time.
- **Length:** 7–15 s (default) / 30 s / 60 s. Optional hook text.
- **Storyboard:** horizontal strip of thumbnails sized to their duration, beat markers underneath. Tap to lock or remove; **Shuffle** button.
- **Results:** three variants as autoplaying muted loops; tap to unmute.
- **Share:** Web Share API Level 2 (`navigator.share({ files: [mp4] })`) opens the native share sheet, so users can send to Instagram, Photos, or WhatsApp. Fall back to a download link. Show the "add Song X at 0:42 in Instagram" tip next to the silent version.
- **Progress:** stage-based (Uploading → Preparing media → Planning → Rendering → Checking). Polls every 2 s while visible.
- **Notify when done** (optional): Web Push, so users can leave the page.

---

## 9. Security, privacy, and limits

Small trusted audience, but these are family photos:

- **Access policy:** allow-list of family emails only. The Worker also verifies the Access JWT (defense in depth) and never trusts a client-supplied identity.
- **Per-owner scoping** on every D1 query and every presigned URL.
- **R2 bucket private.** Only short-lived presigned URLs (about 15 minutes); random object keys.
- **Retention (short-lived by design):**
  - Uploads, the song, proxies and analysis are deleted **immediately after a successful render** (the container also wipes its scratch disk after every job).
  - Finished reels live under `results/<render>/` in R2 for **20 minutes**, then are deleted; users can delete them sooner from the result screen. The result screen counts down and switches to "expired" by itself, and the Worker refuses downloads past `expires_at` (presigned URLs are issued for at most the remaining time).
  - Abandoned drafts (never rendered) are deleted after 24 hours; failed renders keep their uploads for the same 24 hours so the user can retry.
  - Enforced twice: a **Cron Trigger every 5 minutes** deletes expired reels, drafts and D1 rows (R2 lifecycle rules only work in whole days, so they can't enforce 20 minutes). An R2 lifecycle rule is the backstop: delete `uploads/` and `results/` objects older than 1 day and abort incomplete multipart uploads after 1 day.
- **Limits** (protect cost and container disk): 60 files per project, 3 GB total, 500 MB per file, 10 minutes total video, 10 renders per user per day.
- **Container hardening:** FFmpeg `-protocol_whitelist file,pipe`; Pillow decompression-bomb limit; per-job timeout; disk-space check before downloading; scratch wiped after every job.
- **Secrets:** R2 S3 credentials and the internal job secret as Worker/Container secrets, never in the repository.

---

## 10. Testing

### Unit (engine)
- Score normalization, MMR selection, pHash clustering thresholds.
- Beat grid, bar phase, confidence fallback, music start-point selection.
- Beat-locked duration allocation; ±0.25 s tolerance; short-media handling.
- EDL schema validation and transition-overlap arithmetic.
- Capture-time parsing across EXIF/QuickTime/timezone cases.

### Golden EDL snapshots
Fixed sample media + fixed seed → the stored EDL must match exactly. Any planner change shows up as a reviewable diff.

### Integration (CLI, real FFmpeg)
- iPhone HEIC + HDR HEVC (Dolby Vision 8.4 and HLG), Android HDR10+ and SDR clips, VFR footage.
- Portrait/landscape mix, a single photo, only videos, video without audio.
- Music shorter than the target; low-confidence beats (ambient track).
- Corrupt file, unsupported codec, insufficient media for 60 s.
- Output checks: dimensions, codecs, duration, audio presence, no black gaps, correct colors (a mean-luminance check against the proxy catches failed HDR conversion).

### End-to-end (deployed)
- Upload a 1.5 GB video from a real phone over mobile data; interrupt and resume.
- Two family members rendering at once (queue behavior, `max_instances`).
- Share sheet on iOS Safari and Android Chrome.
- Cron cleanup removes R2 objects and rows.

### Subjective review
Keep a fixed corpus of 5–10 real family events. After each tuning change, watch the variants and track: duplicate rate, blurry-clip rate, cuts-on-beat %, duration error, render time, and which variant family members actually post.

---

## 11. Repository layout

```text
reel-generator/
├── engine/                     # Python, runs locally and in the container
│   ├── reel_engine/
│   │   ├── probe.py            # ffprobe, capture time, HDR detection
│   │   ├── proxy.py            # HEIC decode, tone-mapping, CFR proxies
│   │   ├── photo_analysis.py
│   │   ├── video_analysis.py   # scenes, motion, shake, candidate windows
│   │   ├── music.py            # beats, bars, energy, drop/start point, loudness
│   │   ├── scoring.py
│   │   ├── selection.py        # MMR selection, hook/body/ending ordering
│   │   ├── timing.py           # beat-locked durations
│   │   ├── look.py             # crop, motion, punch-ins, speed ramps, LUTs
│   │   ├── edl.py              # dataclasses + JSON Schema validation
│   │   ├── render.py           # per-clip render, assembly, audio, validation
│   │   └── cli.py
│   ├── configs/presets.yaml
│   ├── luts/
│   ├── tests/
│   │   ├── golden/             # EDL snapshots
│   │   └── media/              # small sample corpus
│   ├── server.py               # container HTTP entry: run job, report status
│   ├── Dockerfile              # pinned FFmpeg build with zscale (libzimg), x264
│   └── pyproject.toml
├── web/                        # Cloudflare Worker + PWA
│   ├── src/
│   │   ├── worker.ts           # API routes, Access JWT check, presigning
│   │   ├── queue.ts            # queue consumer → container
│   │   ├── container.ts        # Container / Durable Object class
│   │   └── cron.ts             # retention cleanup
│   ├── app/                    # frontend (Vite)
│   ├── migrations/             # D1 SQL
│   └── wrangler.jsonc
├── music-library/              # royalty-free tracks + license notes
└── README.md
```

Pin the FFmpeg build (it must include `libzimg` for `zscale`, plus `libx264`) and all Python dependencies. Record each music library track's license.

---

## 12. Milestones

### M0 — Risk spike on real Cloudflare (1–2 days)
1. **FFmpeg in Containers:** render a 15 s reel from real iPhone HDR footage on `standard-4`; record wall time and vCPU-minutes.
2. **Phone uploads:** a 1 GB video from iOS Safari and Android Chrome via R2 multipart presigned URLs, including resume after a dropped connection.
3. **Song lookup:** YouTube link → oEmbed → iTunes preview, executed from a Worker.

**Done when:** all three pass. Any failure changes the plan before further building.

### M1 — CLI renderer, music-first (≈1 week)
- Probe, proxies (HEIC, HDR → SDR tone-mapping, CFR), capture-time ordering.
- Music analysis: beats, bars, energy, start point at the drop.
- Simple selection: hook first, then chronological; beat-locked hard cuts.
- Per-clip render + assembly; with-music + silent exports; FFprobe validation.

**Done when:** `reel make ./trip --song song.m4a --length 15` reliably produces a postable, beat-synced reel from real iPhone and Android media with correct colors.

### M2 — Smarter planner and look (1–2 weeks)
- Photo/video quality metrics, pHash duplicates, scene-based candidates.
- MMR selection, energy matching, loopable ending, seed + golden EDL tests.
- Saliency crop, blurred-background fill, Ken Burns, punch-ins, speed ramps, flashes, LUTs.
- Three presets in `presets.yaml`.

**Done when:** the three presets are visibly different, reels rarely repeat or include bad shots, and the same seed gives the same EDL.

### M3 — Cloudflare backend (≈1 week)
- Container image (`engine/` + `server.py`), Worker API, D1 schema, Queues, R2 multipart presigning.
- Access in front of the app; JWT verification; per-owner scoping.
- Job status reporting, `max_instances`, timeouts, Cron cleanup.

**Done when:** a render can be triggered and downloaded through the API from a deployed environment.

### M4 — Mobile web app (1–2 weeks)
- PWA: gallery picker, resumable uploads, music input, storyboard, three variants, lock/remove, Shuffle, share sheet, hook text.

**Done when:** a family member can go from phone gallery to a shared Instagram Reel without help.

### M5 — Family beta and tuning (ongoing)
- Real-event testing on iOS and Android, cost monitoring, preset weight tuning, limit adjustments.

**Total:** roughly 4–6 weeks part-time to a family-ready v1.

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| HDR iPhone video looks washed out | Tone-map in the proxy stage; luminance check in tests |
| Phone uploads exceed request limits / fail on mobile data | Direct-to-R2 multipart uploads, resumable |
| YouTube blocks downloads from Cloudflare | Never download: identify via oEmbed, fetch the iTunes 30 s preview; uploads for full songs |
| Song lookup picks the wrong track | Show top matches with cover art + preview; user confirms |
| 30 s preview too short | Serves 7–15 s and 30 s; 60 s requires an uploaded file |
| Instagram mutes reels with copyrighted music | Silent beat-synced export + Instagram start-time instruction |
| Container cold starts / idle shutdown mid-job | Pre-warm on Create screen, `sleepAfter` ≈10 min, keep-alive during jobs, stage outputs cached in R2 so retries resume |
| 20 GB container disk exceeded | Per-project size limits; process from proxies; stream downloads; disk check before jobs |
| Costs creep up | `max_instances`, daily render cap per user, uploads deleted after rendering, 20 min reel retention, cost check monthly |
| Rule-based editing lacks meaning | Strong hook, chronology, variety, energy matching; storyboard locks and Shuffle (before rendering) cover the rest |
| Beat tracking wrong on some songs | Confidence score + fallback grid; show detected BPM so it's visible |
| Bad crops of landscape footage | Saliency crop, blurred-background fill, optional face detection |

---

## 14. Decision log (revisit after v1)

- **Face-aware cropping:** is a small pretrained CNN face detector (OpenCV YuNet) acceptable under the "non-AI" rule? It noticeably improves crops of people.
- **Full-length songs:** if 30 s previews feel limiting, add the optional home helper for full tracks; Access, R2, and the UI stay unchanged.
- **Default preset:** Hype is the default; confirm after family usage.
- **Retention:** reels 20 minutes, uploads deleted right after rendering; revisit if family members want longer.
- **Royalty-free library:** which sources (e.g. Pixabay Music, Free Music Archive), with license notes stored per track.
- **Web Push notifications:** keep or drop based on how long renders actually take.

**Definition of success:** family members can turn a phone gallery of mixed photos and videos into a short, beat-synced, hook-first vertical reel in a couple of minutes, entirely on CPU, for a few dollars a month.
