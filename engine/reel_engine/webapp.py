"""Web UI: upload media, pick a song, generate reels, then download them.

Storage is short-lived by design, so the same model works on Cloudflare (R2 + an
ephemeral container):

- <data>/projects/<id>/  uploads and the chosen song while the user is editing.
  Deleted as soon as a render succeeds; abandoned drafts after PROJECT_TTL_HOURS.
- <data>/work/<job>/     proxies, analysis, per-clip renders. Deleted when the job ends.
- <data>/results/<job>/  the finished MP4s and job.json, shown at /r/<job>.
  Deleted after the result TTL (default 20 min) or when the user deletes them.

One render runs at a time; others wait in a queue.
"""

from __future__ import annotations

import asyncio
import collections
import hmac
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from . import songs
from .pipeline import (MAX_FILES, MAX_TOTAL_VIDEO_SECONDS, MAX_VIDEO_SECONDS, list_media,
                       make_variants, prepare)
from .probe import UnsupportedMedia, probe_file
from .planner import presets

log = logging.getLogger(__name__)

MB = 1024 * 1024
GB = 1024 * MB
PROJECT_TTL_HOURS = 2  # unrendered drafts
RESULT_TTL_MINUTES = 20
WORK_TTL_HOURS = 6  # orphaned work dirs (e.g. after a crash)
CLEANUP_EVERY_SECONDS = 2 * 60  # expired reels already answer 410; this frees disk

# Limits that stop one visitor from filling the disk or hogging the renderer.
# Sized for family reels (7-60 s): generous for real use, tight for abuse.
MAX_PHOTO_BYTES = 30 * MB
MAX_VIDEO_BYTES = 400 * MB  # about a minute of 4K from a phone
MAX_UPLOAD_BYTES = MAX_VIDEO_BYTES
MAX_PROJECT_BYTES = 2 * GB  # one reel's uploads
MAX_SONG_BYTES = 50 * MB
MAX_DRAFT_BYTES = 8 * GB  # all unrendered uploads on the server together
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
CHUNK = 1024 * 1024
# Uploads arrive in pieces so no single request exceeds Cloudflare's 100 MB limit.
UPLOAD_CHUNK_BYTES = 8 * MB
MIN_FREE_DISK_BYTES = 3 * GB

# Per-visitor limits: the public site has no login, so keep strangers from tying up
# the renderer or the disk. A visitor is the client IP the Cloudflare Worker reports
# (see client_id). Each action has one or more (amount, window seconds) budgets.
LIMITS = {
    "render": [(6, 3600), (20, 86400)],
    "project": [(10, 3600)],
    "upload": [(300, 3600)],  # files started
    "upload_bytes": [(2 * GB, 3600), (6 * GB, 86400)],
}
LIMIT_WORDS = {"render": "reels", "project": "new reels", "upload": "uploads",
               "upload_bytes": "data"}
MAX_QUEUED_JOBS = 8
# A pending upload with no new piece for this long was abandoned (tab closed, network
# gone). It no longer blocks rendering and is discarded when a render starts.
UPLOAD_IDLE_SECONDS = 10 * 60
PROXY_SECRET_ENV = "REEL_PROXY_SECRET"
MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".mp4", ".mov", ".m4v"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav"}
ID_RE = re.compile(r"^[0-9a-f]{32}$")
PRESETS = [k for k in presets() if k != "selection"]
DEFAULT_PRESETS = ["cinematic"]
# Songs found by search / YouTube link are ~30 s previews, so those reels max out at 30 s.
PREVIEW_MAX_SECONDS = 30


# --- state ------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    project_id: str
    length: float
    presets: list[str]
    seed: int
    status: str = "queued"  # queued | preparing | rendering | done | failed
    progress: float = 0.0
    message: str = "Waiting in line…"
    log: list[str] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    hint: str = ""
    notes: list[str] = field(default_factory=list)
    error: str = ""
    queued_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    expires_at: float | None = None


class Store:
    def __init__(self, data: Path, result_ttl_minutes: float = RESULT_TTL_MINUTES):
        self.data = data
        self.projects = data / "projects"
        self.work = data / "work"
        self.results = data / "results"
        for d in (self.projects, self.work, self.results):
            d.mkdir(parents=True, exist_ok=True)
        self.result_ttl = result_ttl_minutes * 60
        self.jobs: dict[str, Job] = {}
        self.uploads: dict[str, dict] = {}  # in-progress chunked uploads
        self.hits: dict[tuple[str, str], collections.deque] = {}
        self.project_locks: dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()

    # projects
    def project_dir(self, pid: str) -> Path:
        if not ID_RE.match(pid):
            raise HTTPException(404, "project not found")
        d = self.projects / pid
        if not d.is_dir():
            raise HTTPException(404, "project not found")
        return d

    def manifest(self, pid: str) -> dict:
        p = self.project_dir(pid) / "project.json"
        return json.loads(p.read_text(encoding="utf-8"))

    def save_manifest(self, pid: str, m: dict) -> None:
        p = self.project_dir(pid) / "project.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(m, indent=2), encoding="utf-8")
        tmp.replace(p)

    def create_project(self) -> str:
        pid = uuid.uuid4().hex
        d = self.projects / pid
        (d / "media").mkdir(parents=True)
        (d / "song").mkdir()
        (d / "project.json").write_text(json.dumps(
            {"id": pid, "created_at": time.time(), "files": [], "song": None, "next": 0}))
        return pid

    def allow(self, client: str, action: str, amount: int = 1) -> None:
        """Sliding-window budgets per visitor; raises 429 when one would be exceeded.
        `amount` is 1 for counted actions and the byte size for "upload_bytes"."""
        now = time.time()
        with self.lock:
            q = self.hits.setdefault((client, action), collections.deque())
            longest = max(window for _, window in LIMITS[action])
            while q and q[0][0] < now - longest:
                q.popleft()
            for limit, window in LIMITS[action]:
                recent = [(t, n) for t, n in q if t >= now - window]
                if sum(n for _, n in recent) + amount > limit:
                    wait = _wait_text(recent[0][0] + window - now if recent else window)
                    period = "hour" if window <= 3600 else "day"
                    if action == "upload_bytes":
                        msg = (f"You've uploaded a lot recently: the limit is {limit // GB} GB per "
                               f"{period}. Try again in {wait}.")
                    else:
                        msg = (f"Too many {LIMIT_WORDS[action]} from you (limit {limit} per "
                               f"{period}). Try again in {wait}.")
                    raise HTTPException(429, msg)
            q.append((now, amount))

    def draft_bytes(self) -> int:
        """Bytes held by unrendered uploads across all projects (plus ones arriving)."""
        total = sum(u["size"] for u in self.uploads.values())
        for d in self.projects.iterdir():
            try:
                m = json.loads((d / "project.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            total += sum(f.get("size", 0) for f in m.get("files", []))
        return total

    # jobs
    def save_job(self, job: Job) -> None:
        d = self.results / job.id
        d.mkdir(parents=True, exist_ok=True)
        data = asdict(job)
        data.pop("log")
        tmp = d / "job.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(d / "job.json")

    def load_job(self, jid: str) -> Job | None:
        p = self.results / jid / "job.json"
        if not p.exists():
            return None
        try:
            return Job(**json.loads(p.read_text(encoding="utf-8")))
        except (TypeError, ValueError):
            return None

    def delete_results(self, jid: str) -> None:
        shutil.rmtree(self.results / jid, ignore_errors=True)
        with self.lock:
            self.jobs.pop(jid, None)

    def cleanup(self) -> None:
        now = time.time()
        with self.lock:
            stale = [u for u, up in self.uploads.items() if now - up["started"] > 2 * 3600]
        for uid in stale:
            up = self.uploads.pop(uid, None)
            if up:
                (self.projects / up["pid"] / "media" / f"{uid}.part").unlink(missing_ok=True)
        busy = {j.id for j in self.jobs.values() if j.status not in ("done", "failed")}
        busy_projects = {self.jobs[j].project_id for j in busy if j in self.jobs}
        for d in self.projects.iterdir():
            meta = d / "project.json"
            age = now - (meta.stat().st_mtime if meta.exists() else d.stat().st_mtime)
            if d.is_dir() and d.name not in busy_projects and age > PROJECT_TTL_HOURS * 3600:
                shutil.rmtree(d, ignore_errors=True)
        for d in self.work.iterdir():
            if d.name not in busy and now - d.stat().st_mtime > WORK_TTL_HOURS * 3600:
                shutil.rmtree(d, ignore_errors=True)
        for d in self.results.iterdir():
            job = self.load_job(d.name)
            expired = job.expires_at is not None and job.expires_at < now if job else \
                now - d.stat().st_mtime > self.result_ttl
            if expired and d.name not in busy:
                self.delete_results(d.name)

    def queue_position(self, job: Job) -> int:
        with self.lock:
            waiting = sorted((j for j in self.jobs.values() if j.status == "queued"),
                             key=lambda j: j.queued_at)
        return next((i + 1 for i, j in enumerate(waiting) if j.id == job.id), 0)


# --- API models -------------------------------------------------------------------

class TrackIn(BaseModel):
    title: str = Field(max_length=300)
    artist: str = Field(max_length=300)
    preview_url: str = Field(max_length=1000)
    artwork_url: str = Field(default="", max_length=1000)
    duration_seconds: float = 0


class UploadIn(BaseModel):
    name: str = Field(max_length=300)
    size: int = Field(gt=0)


class RenderIn(BaseModel):
    length: float = Field(default=15, ge=5, le=60)
    presets: list[str] = Field(default_factory=lambda: list(DEFAULT_PRESETS))
    seed: int = Field(default=0, ge=0, le=2**31)
    order: list[str] | None = None  # file ids in the order the user arranged them


# --- app --------------------------------------------------------------------------

def create_app(data: Path, result_ttl_minutes: float = RESULT_TTL_MINUTES) -> FastAPI:
    store = Store(data, result_ttl_minutes)
    store.cleanup()
    app = FastAPI(title="Reel Generator", docs_url=None, redoc_url=None)
    app.state.store = store
    threading.Thread(target=_worker, args=(store,), daemon=True, name="reel-worker").start()
    threading.Thread(target=_cleaner, args=(store,), daemon=True, name="reel-cleanup").start()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _page("index.html")

    @app.get("/static/app.css")
    def stylesheet() -> Response:
        return Response(_page("app.css"), media_type="text/css")

    @app.get("/r/{jid}", response_class=HTMLResponse)
    def result_page(jid: str) -> str:
        if not ID_RE.match(jid):
            raise HTTPException(404, "not found")
        return _page("result.html")

    @app.get("/api/config")
    def config() -> dict:
        return {"presets": PRESETS, "max_files": MAX_FILES, "chunk_bytes": UPLOAD_CHUNK_BYTES,
                "max_upload_mb": MAX_UPLOAD_BYTES // MB,
                "max_photo_mb": MAX_PHOTO_BYTES // MB, "max_video_mb": MAX_VIDEO_BYTES // MB,
                "max_video_seconds": MAX_VIDEO_SECONDS,
                "max_total_video_seconds": MAX_TOTAL_VIDEO_SECONDS,
                "video_exts": sorted(VIDEO_EXTS),
                "media_exts": sorted(MEDIA_EXTS), "audio_exts": sorted(AUDIO_EXTS)}

    @app.get("/api/songs")
    def search_songs(q: str) -> list[dict]:
        if not q.strip() or len(q) > 500:
            raise HTTPException(400, "type a song name or paste a YouTube link")
        try:
            return [t.to_dict() for t in songs.search(q)]
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except OSError as e:
            raise HTTPException(502, f"song search failed: {e}") from e

    @app.post("/api/projects")
    def create_project(request: Request) -> dict:
        store.allow(client_id(request), "project")
        return {"id": store.create_project()}

    @app.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict:
        m = store.manifest(pid)
        return {"id": pid, "files": m["files"], "song": m["song"]}

    @app.post("/api/projects/{pid}/uploads")
    def start_upload(pid: str, body: UploadIn, request: Request) -> dict:
        """Begin a chunked upload: the browser then PUTs UPLOAD_CHUNK_BYTES pieces."""
        store.project_dir(pid)
        ext = Path(body.name).suffix.lower()
        _check_new_file(store, body.name, ext, body.size)
        visitor = client_id(request)
        store.allow(visitor, "upload")
        with store.project_locks[pid]:
            m = store.manifest(pid)
            pending = [u for u in store.uploads.values() if u["pid"] == pid]
            if len(m["files"]) + len(pending) >= MAX_FILES:
                raise HTTPException(413, f"at most {MAX_FILES} files per reel")
            used = sum(f["size"] for f in m["files"]) + sum(u["size"] for u in pending)
            if used + body.size > MAX_PROJECT_BYTES:
                raise HTTPException(413, f"at most {MAX_PROJECT_BYTES // GB} GB per reel")
            # Counted when the upload starts, so cancelled uploads still count.
            store.allow(visitor, "upload_bytes", body.size)
            uid = uuid.uuid4().hex
            chunks = -(-body.size // UPLOAD_CHUNK_BYTES)
            store.uploads[uid] = {"pid": pid, "name": _display_name(body.name), "ext": ext,
                                  "size": body.size, "chunks": chunks, "received": set(),
                                  "started": time.time(), "active": time.time()}
            # Pre-size the file so chunks can land in any order.
            with (store.project_dir(pid) / "media" / f"{uid}.part").open("wb") as f:
                f.truncate(body.size)
        return {"upload_id": uid, "chunk_size": UPLOAD_CHUNK_BYTES, "chunks": chunks}

    @app.put("/api/projects/{pid}/uploads/{uid}/{index}")
    async def upload_chunk(pid: str, uid: str, index: int, request: Request) -> dict:
        up = _pending_upload(store, pid, uid)
        if not 0 <= index < up["chunks"]:
            raise HTTPException(400, "bad chunk number")
        offset = index * UPLOAD_CHUNK_BYTES
        expected = min(UPLOAD_CHUNK_BYTES, up["size"] - offset)
        data = bytearray()
        async for piece in request.stream():
            data += piece
            if len(data) > expected:
                raise HTTPException(413, "chunk too large")
        if len(data) != expected:
            raise HTTPException(400, f"chunk {index} is {len(data)} bytes, expected {expected}")
        part = store.project_dir(pid) / "media" / f"{uid}.part"
        if uid not in store.uploads:
            raise HTTPException(404, "upload was cancelled")
        try:
            with part.open("r+b") as f:  # retries just overwrite the same bytes
                f.seek(offset)
                f.write(data)
        except FileNotFoundError as e:
            raise HTTPException(404, "upload was cancelled") from e
        up["received"].add(index)
        up["active"] = time.time()
        return {"received": len(up["received"]), "chunks": up["chunks"]}

    @app.delete("/api/projects/{pid}/uploads/{uid}")
    def cancel_upload(pid: str, uid: str) -> dict:
        """The user removed a file that was still uploading: forget it and its bytes."""
        store.project_dir(pid)
        _discard_upload(store, uid, pid)
        return {"ok": True}

    @app.post("/api/projects/{pid}/uploads/{uid}/complete")
    def complete_upload(pid: str, uid: str) -> dict:
        up = _pending_upload(store, pid, uid)
        missing = up["chunks"] - len(up["received"])
        if missing:
            raise HTTPException(400, f"{missing} piece(s) still missing")
        part = store.project_dir(pid) / "media" / f"{uid}.part"
        try:
            return _add_media(store, pid, part, up["name"], up["ext"], up["size"])
        finally:
            store.uploads.pop(uid, None)

    @app.post("/api/projects/{pid}/files")
    async def upload_file(pid: str, request: Request, file: UploadFile = File(...)) -> dict:
        """Single-request upload (CLI/scripts); same limits as the page's chunked uploads."""
        d = store.project_dir(pid)
        name = _display_name(file.filename)
        ext = Path(name).suffix.lower()
        size_hint = file.size or 0
        _check_new_file(store, name, ext, size_hint)
        visitor = client_id(request)
        store.allow(visitor, "upload")
        m = store.manifest(pid)
        if len(m["files"]) >= MAX_FILES:
            raise HTTPException(413, f"at most {MAX_FILES} files per reel")
        used = sum(f["size"] for f in m["files"])
        limit = min(MAX_VIDEO_BYTES if ext in VIDEO_EXTS else MAX_PHOTO_BYTES,
                    MAX_PROJECT_BYTES - used)
        tmp = d / "media" / f"{uuid.uuid4().hex}.part"
        size = await _save_upload(file, tmp, limit)
        try:
            store.allow(visitor, "upload_bytes", size)
        except HTTPException:
            tmp.unlink(missing_ok=True)
            raise
        return await asyncio.to_thread(_add_media, store, pid, tmp, name, ext, size)

    @app.get("/api/projects/{pid}/files/{fid}/thumb")
    def thumb(pid: str, fid: str):
        """Server-made thumbnail: browsers can't show HEIC, and many can't play HEVC."""
        d = store.project_dir(pid)
        entry = next((f for f in store.manifest(pid)["files"] if f["id"] == fid), None)
        if entry is None:
            raise HTTPException(404, "no such file")
        out = d / "thumbs" / f"{fid}.jpg"
        if not out.exists():
            out.parent.mkdir(exist_ok=True)
            try:
                _make_thumb(d / "media" / entry["stored"], out)
            except Exception as e:
                raise HTTPException(404, "no preview") from e
        return FileResponse(out, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.delete("/api/projects/{pid}/files/{fid}")
    def delete_file(pid: str, fid: str) -> dict:
        d = store.project_dir(pid)
        m = store.manifest(pid)
        keep = [f for f in m["files"] if f["id"] != fid]
        for f in m["files"]:
            if f["id"] == fid:
                (d / "media" / f["stored"]).unlink(missing_ok=True)
        m["files"] = keep
        store.save_manifest(pid, m)
        return {"ok": True}

    @app.post("/api/projects/{pid}/song/preview")
    def choose_preview(pid: str, track: TrackIn) -> dict:
        d = store.project_dir(pid)
        _clear(d / "song")
        try:
            path = songs.download_preview(songs.Track(**track.model_dump()), d / "song")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except OSError as e:
            raise HTTPException(502, f"could not download the preview: {e}") from e
        m = store.manifest(pid)
        m["song"] = {"kind": "preview", "stored": path.name,
                     "title": track.title, "artist": track.artist, "artwork": track.artwork_url}
        store.save_manifest(pid, m)
        return m["song"]

    @app.post("/api/projects/{pid}/song/upload")
    async def upload_song(pid: str, request: Request, file: UploadFile = File(...)) -> dict:
        d = store.project_dir(pid)
        ext = Path(file.filename or "").suffix.lower()
        if ext not in AUDIO_EXTS:
            raise HTTPException(415, "use an MP3, M4A, AAC or WAV file")
        visitor = client_id(request)
        store.allow(visitor, "upload")
        _clear(d / "song")
        stored = f"song{ext}"
        size = await _save_upload(file, d / "song" / stored, MAX_SONG_BYTES)
        store.allow(visitor, "upload_bytes", size)
        m = store.manifest(pid)
        m["song"] = {"kind": "upload", "stored": stored, "title": _display_name(file.filename),
                     "artist": "", "artwork": ""}
        store.save_manifest(pid, m)
        return m["song"]

    @app.delete("/api/projects/{pid}/song")
    def delete_song(pid: str) -> dict:
        d = store.project_dir(pid)
        _clear(d / "song")
        m = store.manifest(pid)
        m["song"] = None
        store.save_manifest(pid, m)
        return {"ok": True}

    @app.post("/api/projects/{pid}/render")
    def start_render(pid: str, body: RenderIn, request: Request) -> dict:
        store.project_dir(pid)
        now = time.time()
        for uid, up in list(store.uploads.items()):
            if up["pid"] != pid:
                continue
            if now - up["active"] < UPLOAD_IDLE_SECONDS:
                raise HTTPException(409, "some files are still uploading")
            _discard_upload(store, uid, pid)  # abandoned; it would never finish
        with store.lock:
            queued = sum(1 for j in store.jobs.values() if j.status == "queued")
        if queued >= MAX_QUEUED_JOBS:
            raise HTTPException(503, "Lots of reels are being made right now; try again in a few minutes.")
        store.allow(client_id(request), "render")
        m = store.manifest(pid)
        if not m["files"]:
            raise HTTPException(400, "add some photos or videos first")
        bad = [p for p in body.presets if p not in PRESETS]
        if bad or not body.presets:
            raise HTTPException(400, "pick at least one style")
        if body.order:
            _apply_order(store, pid, body.order)
        song = m.get("song") or {}
        length = min(body.length, PREVIEW_MAX_SECONDS) if song.get("kind") == "preview" else body.length
        job = Job(uuid.uuid4().hex, pid, length, body.presets, body.seed)
        with store.lock:
            store.jobs[job.id] = job
        store.queue.put(job.id)
        return {"job_id": job.id}

    @app.get("/api/jobs/{jid}")
    def job_status(jid: str) -> dict:
        job = _job(store, jid)
        d = asdict(job)
        d["queue_position"] = store.queue_position(job) if job.status == "queued" else 0
        d.pop("log")
        d["log_tail"] = job.log[-6:]
        return d

    @app.delete("/api/jobs/{jid}")
    def delete_job(jid: str) -> dict:
        job = _job(store, jid)
        if job.status not in ("done", "failed"):
            raise HTTPException(409, "still rendering")
        store.delete_results(jid)
        return {"ok": True}

    @app.get("/api/jobs/{jid}/video/{preset}/{variant}")
    def video(jid: str, preset: str, variant: str, request: Request, dl: int = 0):
        job = _job(store, jid)
        if preset not in job.presets or variant not in ("music", "silent"):
            raise HTTPException(404, "no such video")
        out = store.results / jid
        path = out / (f"{preset}.mp4" if variant == "music" else f"{preset}-silent.mp4")
        if not path.is_file():
            raise HTTPException(404, "video not ready")
        name = f"reel-{preset}{'-silent' if variant == 'silent' else ''}.mp4"
        return FileResponse(path, media_type="video/mp4", filename=name if dl else None,
                            content_disposition_type="attachment" if dl else "inline")

    return app


# --- helpers ----------------------------------------------------------------------

async def _save_upload(file: UploadFile, dest: Path, limit: int) -> int:
    size = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with tmp.open("wb") as f:
            while chunk := await file.read(CHUNK):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"{file.filename}: file too large")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, f"{file.filename}: empty file")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return size


def _make_thumb(src: Path, out: Path) -> None:
    from PIL import Image, ImageOps

    from . import ffmpeg
    from .probe import probe_file  # registers the HEIC opener

    info = probe_file(src)
    tmp = out.with_suffix(".tmp.jpg")
    if info.kind == "photo":
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((360, 360))
            im.save(tmp, "JPEG", quality=80)
    else:
        ffmpeg.run([*ffmpeg.inp(src, "-ss", f"{min(0.5, info.duration / 2):.2f}"),
                    "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "5", tmp])
    tmp.replace(out)


def _display_name(name: str | None) -> str:
    base = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[\x00-\x1f]", "", base)[:120] or "file"


def _clear(d: Path) -> None:
    for p in d.iterdir():
        if p.is_file():
            p.unlink()


def _apply_order(store: Store, pid: str, order: list[str]) -> None:
    """Rename stored files so their sort order matches the user's arrangement."""
    d = store.project_dir(pid) / "media"
    m = store.manifest(pid)
    by_id = {f["id"]: f for f in m["files"]}
    arranged = [by_id[i] for i in order if i in by_id]
    arranged += [f for f in m["files"] if f["id"] not in set(order)]
    for idx, f in enumerate(arranged):
        new = f"{idx:04d}_{f['stored'].split('_', 1)[1]}"
        if new != f["stored"]:
            (d / f["stored"]).rename(d / new)
            f["stored"] = new
    m["files"] = arranged
    m["next"] = len(arranged)
    store.save_manifest(pid, m)


def client_id(request: Request) -> str:
    """Who is this visitor? Behind the Cloudflare Worker every request comes from the
    tunnel, so trust the Worker's X-Reel-Client-IP header, but only when it carries
    the shared secret from REEL_PROXY_SECRET. Otherwise use the socket address."""
    secret = os.environ.get(PROXY_SECRET_ENV, "")
    sent = request.headers.get("x-reel-proxy-secret", "")
    if secret and hmac.compare_digest(sent, secret):
        ip = request.headers.get("x-reel-client-ip", "").strip()
        if ip:
            return ip[:64]
    return request.client.host if request.client else "unknown"


def _discard_upload(store: "Store", uid: str, pid: str) -> None:
    up = store.uploads.get(uid) if ID_RE.match(uid) else None
    if up and up["pid"] == pid:
        store.uploads.pop(uid, None)
        try:
            (store.projects / pid / "media" / f"{uid}.part").unlink(missing_ok=True)
        except OSError:
            pass  # a piece is still being written (Windows); the project cleanup removes it


def _check_new_file(store: "Store", name: str, ext: str, size: int) -> None:
    """Cheap checks before accepting any bytes: type, size, server capacity."""
    if ext not in MEDIA_EXTS:
        raise HTTPException(415, f"{name}: unsupported file type")
    is_video = ext in VIDEO_EXTS
    limit = MAX_VIDEO_BYTES if is_video else MAX_PHOTO_BYTES
    if size > limit:
        raise HTTPException(413, f"{name} is {size / MB:.0f} MB; "
                                 f"{'videos' if is_video else 'photos'} can be up to {limit // MB} MB")
    if store.draft_bytes() + size > MAX_DRAFT_BYTES:
        raise HTTPException(503, "Lots of uploads are in progress right now. Try again in a few minutes.")
    if shutil.disk_usage(store.data).free < MIN_FREE_DISK_BYTES + size:
        raise HTTPException(507, "The server is low on disk space. Try again later.")


def _add_media(store: "Store", pid: str, src: Path, name: str, ext: str, size: int) -> dict:
    """Check a fully received file and add it to the project, or delete it and say why.
    Catches what the size check can't: unreadable files and too-long videos."""
    def reject(status: int, msg: str):
        src.unlink(missing_ok=True)
        raise HTTPException(status, msg)

    try:
        info = probe_file(src)
    except UnsupportedMedia as e:
        reject(415, str(e).replace(src.name, name))
    if info.kind == "audio":
        reject(415, f"{name} is an audio file; add it as the song instead")
    duration = round(info.duration, 2) if info.kind == "video" else 0.0
    if duration > MAX_VIDEO_SECONDS:
        reject(413, f"{name} is {_mmss(duration)} long; videos can be up to "
                    f"{_mmss(MAX_VIDEO_SECONDS)}. Trim it on your phone first.")
    d = store.project_dir(pid)
    with store.project_locks[pid]:
        m = store.manifest(pid)
        video_total = sum(f.get("duration", 0) for f in m["files"]) + duration
        if duration and video_total > MAX_TOTAL_VIDEO_SECONDS:
            reject(413, f"{name} would make {_mmss(video_total)} of video in this reel; "
                        f"the limit is {_mmss(MAX_TOTAL_VIDEO_SECONDS)}.")
        fid = uuid.uuid4().hex[:12]
        # Stored name is generated; the user's filename is only kept for display.
        stored = f"{m['next']:04d}_{fid}{ext}"
        src.replace(d / "media" / stored)
        entry = {"id": fid, "name": name, "stored": stored, "size": size,
                 "kind": info.kind, "duration": duration}
        m["files"].append(entry)
        m["next"] += 1
        store.save_manifest(pid, m)
    return entry


def _mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _wait_text(seconds: float) -> str:
    minutes = max(1, int(seconds // 60) + 1)
    return f"{minutes} min" if minutes < 120 else f"{round(minutes / 60)} hours"


def _pending_upload(store: "Store", pid: str, uid: str) -> dict:
    store.project_dir(pid)
    up = store.uploads.get(uid) if ID_RE.match(uid) else None
    if not up or up["pid"] != pid:
        raise HTTPException(404, "upload not found; start it again")
    return up


def _page(name: str) -> str:
    return resources.files("reel_engine").joinpath(f"static/{name}").read_text("utf-8")


def _job(store: Store, jid: str) -> Job:
    if not ID_RE.match(jid):
        raise HTTPException(404, "job not found")
    with store.lock:
        job = store.jobs.get(jid)
    if job is None:
        job = store.load_job(jid)  # finished before a restart
    if job is None:
        raise HTTPException(404, "these reels were deleted or never existed")
    if job.expires_at is not None and job.expires_at < time.time():
        store.delete_results(jid)
        raise HTTPException(410, "these reels have expired")
    return job


def _worker(store: Store) -> None:
    while True:
        jid = store.queue.get()
        job = store.jobs.get(jid)
        if job:
            try:
                _run(store, job)
            except Exception as e:  # report every failure to the page
                log.error("job %s failed:\n%s", jid, traceback.format_exc())
                job.status = "failed"
                job.error = str(e).splitlines()[0] if str(e) else e.__class__.__name__
                job.message = "Something went wrong."
                # Keep the uploads so the user can go back and try again.
            finally:
                job.finished_at = job.finished_at or time.time()
                job.expires_at = job.expires_at or job.finished_at + store.result_ttl
                shutil.rmtree(store.work / jid, ignore_errors=True)
                store.save_job(job)
        store.queue.task_done()


def _cleaner(store: Store) -> None:
    while True:
        time.sleep(CLEANUP_EVERY_SECONDS)
        try:
            store.cleanup()
        except OSError:
            log.warning("cleanup failed", exc_info=True)


def _run(store: Store, job: Job) -> None:
    d = store.project_dir(job.project_id)
    m = store.manifest(job.project_id)
    files = list_media([d / "media"])
    n_files, n_presets = max(1, len(files)), len(job.presets)
    counts = {"prepared": 0, "rendered": 0}

    def progress(msg: str) -> None:
        msg = msg.strip()
        job.log.append(msg)
        if msg.startswith("prepared"):
            counts["prepared"] += 1
            job.message = f"Preparing media ({counts['prepared']}/{n_files})…"
        elif msg.startswith("planned"):
            job.status = "rendering"
            job.message = f"Rendering {msg.split()[1].rstrip(':')} ({counts['rendered'] + 1}/{n_presets})…"
        elif msg.startswith("rendered"):
            counts["rendered"] += 1
        elif msg.startswith("music"):
            job.message = "Finding the beat…"
        job.progress = round(0.05 + 0.45 * counts["prepared"] / n_files
                             + 0.5 * counts["rendered"] / n_presets, 3)

    job.status = "preparing"
    job.message = f"Preparing media (0/{n_files})…"
    job.progress = 0.02
    work = store.work / job.id
    prepared = prepare(files, work, progress=progress)
    names = {f["stored"]: f["name"] for f in m["files"]}
    skipped = []
    for s in prepared.skipped:
        # Show the user's own file names, e.g. "IMG_2.jpg: same file as IMG_1.jpg".
        for stored, shown in names.items():
            s = s.replace(stored, shown)
        skipped.append(s)
    if not prepared.assets:
        raise ValueError("none of the files could be used: " + "; ".join(skipped))

    song = d / "song" / m["song"]["stored"] if m.get("song") else None
    out = store.results / job.id
    results = make_variants(prepared, song, out, work, job.length, job.presets,
                            job.seed, progress=progress)
    # Keep only the videos; the plans and README were for the CLI.
    for extra in [*out.glob("*.edl.json"), out / "README.txt"]:
        extra.unlink(missing_ok=True)
    job.results = [{
        "preset": r.preset,
        "duration": round(r.edl.output.duration_seconds, 2),
        "shots": len(r.edl.clips),
        "has_music": bool(r.render and r.render.with_music),
    } for r in results]
    music = results[0].edl.music if results else None
    job.hint = music.instagram_hint if music else ""
    job.notes = sorted({n for r in results for n in r.edl.notes}) + [f"Skipped {s}" for s in skipped]
    # Success: the uploads have done their job. Only the finished reels remain.
    # Clean up and set the expiry *before* flipping to "done" so a poller never sees
    # a finished job whose uploads still exist or whose expiry is unset.
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)
    job.finished_at = time.time()
    job.expires_at = job.finished_at + store.result_ttl
    store.save_job(job)
    job.progress = 1.0
    job.message = "Your reels are ready!"
    job.status = "done"
