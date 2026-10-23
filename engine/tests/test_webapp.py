import time

import pytest
from fastapi.testclient import TestClient

from reel_engine.webapp import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "data"))


def test_page_and_config(client):
    assert "Reel Maker" in client.get("/").text
    assert "--accent" in client.get("/static/app.css").text
    assert client.get("/r/not-an-id").status_code == 404
    cfg = client.get("/api/config").json()
    assert cfg["presets"] == ["hype", "smooth", "cinematic"]


def test_rejects_bad_ids_and_types(client):
    assert client.get("/api/projects/../../etc").status_code == 404
    assert client.get("/api/projects/" + "a" * 32).status_code == 404
    pid = client.post("/api/projects").json()["id"]
    r = client.post(f"/api/projects/{pid}/files", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 415
    r = client.post(f"/api/projects/{pid}/render", json={"length": 15})
    assert r.status_code == 400  # no media yet
    assert client.get("/api/jobs/" + "b" * 32).status_code == 404


def test_upload_delete_and_order(client):
    pid = client.post("/api/projects").json()["id"]
    a = client.post(f"/api/projects/{pid}/files", files={"file": ("a.jpg", b"\xff\xd8x", "image/jpeg")}).json()
    b = client.post(f"/api/projects/{pid}/files", files={"file": ("b.jpg", b"\xff\xd8y", "image/jpeg")}).json()
    assert a["name"] == "a.jpg" and a["stored"] != "a.jpg"
    client.delete(f"/api/projects/{pid}/files/{a['id']}")
    files = client.get(f"/api/projects/{pid}").json()["files"]
    assert [f["id"] for f in files] == [b["id"]]


@pytest.mark.ffmpeg
def test_full_flow(client, media_dir, song_file):
    pid = client.post("/api/projects").json()["id"]
    for name in ("01_beach.jpg", "03_group_landscape.jpg", "04_iphone.heic", "06_portrait.mp4", "07_zoom.mov"):
        r = client.post(f"/api/projects/{pid}/files",
                        files={"file": (name, (media_dir / name).read_bytes(), "application/octet-stream")})
        assert r.status_code == 200, r.text
        # Thumbnails work for formats browsers can't show (HEIC, HEVC).
        t = client.get(f"/api/projects/{pid}/files/{r.json()['id']}/thumb")
        assert t.status_code == 200 and t.headers["content-type"] == "image/jpeg"
    r = client.post(f"/api/projects/{pid}/song/upload",
                    files={"file": ("my song.m4a", song_file.read_bytes(), "audio/mp4")})
    assert r.status_code == 200 and r.json()["kind"] == "upload"

    job_id = client.post(f"/api/projects/{pid}/render",
                         json={"length": 7, "presets": ["hype", "smooth"]}).json()["job_id"]
    deadline = time.time() + 300
    while True:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed") or time.time() > deadline:
            break
        time.sleep(1)
    assert job["status"] == "done", job
    assert [r["preset"] for r in job["results"]] == ["hype", "smooth"]
    assert "Instagram" in job["hint"]

    r = client.get(f"/api/jobs/{job_id}/video/hype/music?dl=1")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert "attachment" in r.headers["content-disposition"] and "reel-hype.mp4" in r.headers["content-disposition"]
    assert len(r.content) > 10_000
    ranged = client.get(f"/api/jobs/{job_id}/video/hype/silent", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100
    assert client.get(f"/api/jobs/{job_id}/video/nope/music").status_code == 404

    # Only the finished reels remain: uploads, song and working files are gone.
    data = client.app.state.store.data
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert not (data / "projects" / pid).exists()
    assert not (data / "work" / job_id).exists()
    assert sorted(p.name for p in (data / "results" / job_id).iterdir()) == [
        "hype-silent.mp4", "hype.mp4", "job.json", "smooth-silent.mp4", "smooth.mp4"]
    assert time.time() + 50 * 60 < job["expires_at"] <= time.time() + 3600

    # The result screen and its videos survive a server restart.
    restarted = TestClient(create_app(data))
    assert "Your reels" in restarted.get(f"/r/{job_id}").text
    assert restarted.get(f"/api/jobs/{job_id}").json()["status"] == "done"
    assert restarted.get(f"/api/jobs/{job_id}/video/smooth/music").status_code == 200

    # Delete now: everything goes.
    assert restarted.delete(f"/api/jobs/{job_id}").status_code == 200
    assert not (data / "results" / job_id).exists()
    assert restarted.get(f"/api/jobs/{job_id}").status_code == 404


def test_failed_render_keeps_uploads_for_retry(client):
    pid = client.post("/api/projects").json()["id"]
    client.post(f"/api/projects/{pid}/files", files={"file": ("bad.jpg", b"\xff\xd8 junk", "image/jpeg")})
    job_id = client.post(f"/api/projects/{pid}/render", json={"length": 7}).json()["job_id"]
    deadline = time.time() + 60
    while (job := client.get(f"/api/jobs/{job_id}").json())["status"] not in ("done", "failed"):
        assert time.time() < deadline
        time.sleep(0.5)
    assert job["status"] == "failed" and job["project_id"] == pid
    assert client.get(f"/api/projects/{pid}").status_code == 200  # user can go back and fix it


def test_expired_results_are_deleted(tmp_path):
    data = tmp_path / "data"
    app = create_app(data)
    store = app.state.store
    jid = "c" * 32
    from reel_engine.webapp import Job
    job = Job(jid, "d" * 32, 7, ["hype"], 0, status="done", expires_at=time.time() - 1)
    store.save_job(job)
    (data / "results" / jid / "hype.mp4").write_bytes(b"x")
    c = TestClient(app)
    assert c.get(f"/api/jobs/{jid}").status_code == 410
    assert not (data / "results" / jid).exists()

    # Abandoned drafts and orphaned work dirs are swept too.
    import os
    old = time.time() - 48 * 3600
    draft = data / "projects" / ("e" * 32)
    draft.mkdir(parents=True)
    (draft / "project.json").write_text("{}")
    os.utime(draft / "project.json", (old, old))
    orphan = data / "work" / ("f" * 32)
    orphan.mkdir(parents=True)
    os.utime(orphan, (old, old))
    store.cleanup()
    assert not draft.exists() and not orphan.exists()


def test_chunked_upload_any_order_and_retry(client, monkeypatch):
    from reel_engine import webapp
    monkeypatch.setattr(webapp, "UPLOAD_CHUNK_BYTES", 10)
    pid = client.post("/api/projects").json()["id"]
    data = bytes(range(256)) * 1  # 256 bytes -> 26 chunks of 10
    init = client.post(f"/api/projects/{pid}/uploads", json={"name": "clip.MOV", "size": len(data)}).json()
    assert init["chunks"] == 26
    url = f"/api/projects/{pid}/uploads/{init['upload_id']}"
    order = list(range(26))[::-1]
    for i in order:
        assert client.put(f"{url}/{i}", content=data[i * 10:(i + 1) * 10]).status_code == 200
    assert client.put(f"{url}/3", content=data[30:40]).status_code == 200  # a retry is harmless
    assert client.put(f"{url}/0", content=b"short").status_code == 400
    assert client.put(f"{url}/99", content=b"x").status_code == 400
    entry = client.post(f"{url}/complete").json()
    assert entry["name"] == "clip.MOV" and entry["size"] == 256
    stored = client.app.state.store.project_dir(pid) / "media" / entry["stored"]
    assert stored.read_bytes() == data


def test_incomplete_upload_is_refused(client):
    pid = client.post("/api/projects").json()["id"]
    init = client.post(f"/api/projects/{pid}/uploads", json={"name": "a.jpg", "size": 5}).json()
    r = client.post(f"/api/projects/{pid}/uploads/{init['upload_id']}/complete")
    assert r.status_code == 400 and "missing" in r.json()["detail"]
    assert client.post(f"/api/projects/{pid}/render", json={"length": 7}).status_code == 409
    assert client.post(f"/api/projects/{pid}/uploads", json={"name": "a.exe", "size": 5}).status_code == 415
    assert client.post(f"/api/projects/{pid}/uploads",
                       json={"name": "a.mp4", "size": 600 * 2**20}).status_code == 413


def test_visitor_limits_use_worker_header_only_with_secret(client, monkeypatch):
    from reel_engine import webapp
    monkeypatch.setattr(webapp, "LIMITS", {**webapp.LIMITS, "project": (2, 3600)})
    monkeypatch.setenv("REEL_PROXY_SECRET", "s3cret")
    good = {"x-reel-proxy-secret": "s3cret"}
    for _ in range(2):
        assert client.post("/api/projects", headers={**good, "x-reel-client-ip": "1.1.1.1"}).status_code == 200
    r = client.post("/api/projects", headers={**good, "x-reel-client-ip": "1.1.1.1"})
    assert r.status_code == 429
    # A different visitor is unaffected...
    assert client.post("/api/projects", headers={**good, "x-reel-client-ip": "2.2.2.2"}).status_code == 200
    # ...and a spoofed header without the secret is ignored (falls back to the socket address).
    for _ in range(2):
        client.post("/api/projects", headers={"x-reel-client-ip": "3.3.3.3"})
    assert client.post("/api/projects", headers={"x-reel-client-ip": "4.4.4.4"}).status_code == 429


@pytest.mark.parametrize("kind,asked,expected", [
    ("preview", 60, 30),  # searched songs can't make 60 s reels
    ("preview", 7, 7),    # ...but shorter is fine
    ("upload", 60, 60),   # uploaded audio keeps the chosen length
])
def test_reel_length_depends_on_song_source(client, kind, asked, expected):
    pid = client.post("/api/projects").json()["id"]
    client.post(f"/api/projects/{pid}/files", files={"file": ("a.jpg", b"\xff\xd8 junk", "image/jpeg")})
    store = client.app.state.store
    m = store.manifest(pid)
    m["song"] = {"kind": kind, "stored": "song.m4a", "title": "t", "artist": "a", "artwork": ""}
    store.save_manifest(pid, m)
    jid = client.post(f"/api/projects/{pid}/render", json={"length": asked}).json()["job_id"]
    assert client.get(f"/api/jobs/{jid}").json()["length"] == expected


def test_cancelled_upload_does_not_block_rendering(client):
    pid = client.post("/api/projects").json()["id"]
    client.post(f"/api/projects/{pid}/files", files={"file": ("a.jpg", b"\xff\xd8 junk", "image/jpeg")})
    init = client.post(f"/api/projects/{pid}/uploads", json={"name": "big.mov", "size": 20}).json()
    url = f"/api/projects/{pid}/uploads/{init['upload_id']}"
    client.put(f"{url}/0", content=b"x" * 20)
    assert client.post(f"/api/projects/{pid}/render", json={"length": 7}).status_code == 409

    # The user removes the tile: the page cancels the upload.
    assert client.delete(url).status_code == 200
    media = client.app.state.store.project_dir(pid) / "media"
    assert not list(media.glob("*.part"))
    assert client.put(f"{url}/0", content=b"x" * 20).status_code == 404  # late piece
    assert client.post(f"{url}/complete").status_code == 404              # never added
    assert client.post(f"/api/projects/{pid}/render", json={"length": 7}).status_code == 200
    assert [f["name"] for f in client.get(f"/api/projects/{pid}").json()["files"]] == ["a.jpg"]


def test_abandoned_upload_stops_blocking_after_idle_time(client):
    pid = client.post("/api/projects").json()["id"]
    client.post(f"/api/projects/{pid}/files", files={"file": ("a.jpg", b"\xff\xd8 junk", "image/jpeg")})
    init = client.post(f"/api/projects/{pid}/uploads", json={"name": "big.mov", "size": 20}).json()
    store = client.app.state.store
    store.uploads[init["upload_id"]]["active"] -= 11 * 60  # tab closed 11 minutes ago
    assert client.post(f"/api/projects/{pid}/render", json={"length": 7}).status_code == 200
    assert init["upload_id"] not in store.uploads
