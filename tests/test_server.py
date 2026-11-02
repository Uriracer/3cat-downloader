"""Tests for tresdl.server (REST API, SSE, guards) using in-memory fakes.

No network access and no dependency on tresdl.api / tresdl.downloader /
tresdl.config: the client, manager and settings store are fakes.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tresdl import server
from tresdl.models import DownloadJob, Episode, JobStatus, MediaInfo, MediaSource, Program, Season


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class ThreeCatError(Exception):
    """Matched by name by the server, like the real tresdl.api.ThreeCatError."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


PROGRAM = Program(id=54076, slug="merli", title="Merlí", description="Sèrie", info="Tres temporades")


def make_episode(vid: int, season: int, number: int) -> Episode:
    return Episode(id=vid, program_id=PROGRAM.id, program_title=PROGRAM.title, title=f"Capítol {number}",
                   raw_title=f"T{season}xC{number} - Capítol {number}", season_id=f"PUTEMP_{season}",
                   season_number=season, episode_number=number, absolute_number=number)


EPISODES = [make_episode(100 + i, 1, i + 1) for i in range(3)] + [make_episode(200 + i, 2, i + 1) for i in range(2)]


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_with: Optional[Exception] = None

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def search(self, query: str, limit: int = 30) -> list[Program]:
        self.calls.append(("search", query))
        self._check()
        return [PROGRAM]

    def get_program(self, program_id: int) -> Program:
        self.calls.append(("get_program", program_id))
        self._check()
        if program_id != PROGRAM.id:
            raise ThreeCatError("No existeix", status_code=404)
        return PROGRAM

    def list_episodes(self, program_id: int, season_id: str | None = None,
                      full_episodes_only: bool = True) -> list[Episode]:
        self.calls.append(("list_episodes", program_id))
        self._check()
        if program_id != PROGRAM.id:
            raise ThreeCatError("No existeix", status_code=404)
        return [e for e in EPISODES if season_id is None or e.season_id == season_id]

    def list_seasons(self, episodes: list[Episode]) -> list[Season]:
        seasons: dict[str, Season] = {}
        for ep in episodes:
            s = seasons.setdefault(ep.season_id, Season(id=ep.season_id, number=ep.season_number,
                                                        title=f"{ep.season_number}a Temporada"))
            s.episode_count += 1
        return list(seasons.values())

    def get_media(self, video_id: int) -> MediaInfo:
        self._check()
        return MediaInfo(video_id=video_id, title="Clip", available=True, geo="TOTS",
                         sources=[MediaSource(url="https://x/stream.mpd", kind="dash", label="DASH", height=1080)])

    def get_episode(self, video_id: int) -> Episode:
        self.calls.append(("get_episode", video_id))
        self._check()
        return Episode(id=video_id, program_id=0, program_title="Solt", title="Vídeo", raw_title="Vídeo")

    def resolve(self, text: str) -> dict:
        self._check()
        if text.isdigit():
            return {"type": "video", "episode": self.get_episode(int(text)), "program": None}
        if "merli" in text:
            return {"type": "program", "program": PROGRAM}
        return {"type": "search", "results": [PROGRAM]}


class FakeManager:
    def __init__(self) -> None:
        self._jobs: dict[str, DownloadJob] = {}
        self._subs: list[Callable[[DownloadJob], None]] = []
        self.started = False
        self.enqueue_args: list[dict[str, Any]] = []
        self.refreshed = 0

    def refresh_workers(self) -> None:
        self.refreshed += 1

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def emit(self, job: DownloadJob) -> None:
        for cb in self._subs:
            cb(job)

    def enqueue(self, episodes: list[Episode], quality: str | None = None, category: str = "tv") -> list[DownloadJob]:
        self.enqueue_args.append({"ids": [e.id for e in episodes], "quality": quality, "category": category})
        queued = {j.episode.id for j in self._jobs.values()}
        created = []
        for ep in episodes:
            if ep.id in queued:
                continue
            job = DownloadJob(id=f"job{ep.id}", episode=ep, quality=quality or "best", category=category)
            self._jobs[job.id] = job
            created.append(job)
            self.emit(job)
        return created

    def jobs(self) -> list[DownloadJob]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> DownloadJob | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status != JobStatus.QUEUED:
            return False
        job.status = JobStatus.CANCELLED
        return True

    def retry(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
            return False
        job.status = JobStatus.QUEUED
        return True

    def remove(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.status in (JobStatus.QUEUED, JobStatus.DOWNLOADING):
            return False
        del self._jobs[job_id]
        return True

    def clear_finished(self) -> int:
        done = [k for k, j in self._jobs.items() if j.status in (JobStatus.COMPLETED, JobStatus.CANCELLED)]
        for k in done:
            del self._jobs[k]
        return len(done)

    def subscribe(self, cb: Callable[[DownloadJob], None]) -> None:
        self._subs.append(cb)


@dataclass
class FakeSettings:
    download_dir: str = ""
    max_parallel: int = 2
    quality: str = "best"
    subtitles: bool = True
    embed_subtitles: bool = False
    audio_description: bool = False
    skip_existing: bool = True
    filename_template: str = "{series}/Season {season:02d}/{series} - S{season:02d}E{episode:02d} - {title}"
    server_port: int = 8765
    api_key: str = "abc123"


@dataclass
class FakeStore:
    settings: FakeSettings
    saved: list[FakeSettings] = field(default_factory=list)

    def get(self) -> FakeSettings:
        return self.settings

    def set(self, settings: FakeSettings) -> FakeSettings:
        self.saved.append(settings)
        self.settings = settings
        return settings


#: The production allow-list only accepts loopback names (not "testserver").
BASE_URL = "http://127.0.0.1"


@pytest.fixture()
def web_dir(tmp_path: Path) -> Path:
    d = tmp_path / "web"
    d.mkdir()
    (d / "index.html").write_text("<!doctype html><title>3Cat</title>", encoding="utf-8")
    (d / "app.js").write_text("console.log(1)", encoding="utf-8")
    return d


@pytest.fixture()
def env(tmp_path: Path, web_dir: Path):
    downloads = tmp_path / "Descàrregues"
    client, manager = FakeClient(), FakeManager()
    store = FakeStore(FakeSettings(download_dir=str(downloads)))
    app = server.create_app(client=client, manager=manager, settings_store=store, web_dir=web_dir)
    with TestClient(app, base_url=BASE_URL) as http:
        yield {"http": http, "client": client, "manager": manager, "store": store,
               "app": app, "downloads": downloads}


# --------------------------------------------------------------------------- #
# Basic endpoints
# --------------------------------------------------------------------------- #
def test_health(env) -> None:
    r = env["http"].get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["version"] and data["app"] == server.APP_ID
    assert isinstance(data["ffmpeg"], bool)
    assert r.headers["cache-control"] == "no-store"


def test_manager_not_started_when_injected(env) -> None:
    assert env["manager"].started is False  # caller (run.py) owns it


def test_search(env) -> None:
    http = env["http"]
    assert http.get("/api/search", params={"q": "  "}).json() == {"results": []}
    r = http.get("/api/search", params={"q": "merli"})
    assert r.status_code == 200
    assert r.json()["results"][0]["slug"] == "merli"


def test_resolve_serialises_models(env) -> None:
    http = env["http"]
    assert http.post("/api/resolve", json={"text": "https://www.3cat.cat/3cat/merli/"}).json() == {
        "type": "program", "program": PROGRAM.to_dict()}
    video = http.post("/api/resolve", json={"text": "6293447"}).json()
    assert video["type"] == "video" and video["episode"]["id"] == 6293447 and video["program"] is None
    assert http.post("/api/resolve", json={"text": "x"}).json()["type"] == "search"
    assert http.post("/api/resolve", json={"text": ""}).status_code == 422
    assert http.post("/api/resolve", json={"text": "   "}).status_code == 422


def test_program_detail(env) -> None:
    r = env["http"].get(f"/api/programs/{PROGRAM.id}")
    assert r.status_code == 200
    data = r.json()
    assert data["program"]["title"] == "Merlí"
    assert len(data["episodes"]) == 5
    assert [s["id"] for s in data["seasons"]] == ["PUTEMP_1", "PUTEMP_2"]


def test_threecat_error_maps_to_502_and_404(env) -> None:
    http = env["http"]
    assert http.get("/api/programs/1").status_code == 404
    env["client"].fail_with = ThreeCatError("3cat no respon")
    r = http.get(f"/api/programs/{PROGRAM.id}")
    assert r.status_code == 502 and r.json() == {"detail": "3cat no respon"}
    assert http.get("/api/search", params={"q": "a"}).status_code == 502
    assert http.get("/api/videos/1/media").status_code == 502


def test_real_api_errors_map_to_404(env) -> None:
    """The real tresdl.api errors carry ``.status`` / are ``ThreeCatNotFound``."""
    from tresdl import api

    http = env["http"]
    for exc in (api.ThreeCatNotFound("No s'ha trobat el contingut a 3Cat.", status=404),
                api.ThreeCatError("No existeix", status=404)):
        env["client"].fail_with = exc
        r = http.get(f"/api/programs/{PROGRAM.id}")
        assert r.status_code == 404 and r.json() == {"detail": str(exc)}
    env["client"].fail_with = api.ThreeCatError("Error de xarxa", status=500)
    assert http.get(f"/api/programs/{PROGRAM.id}").status_code == 502


def test_media(env) -> None:
    r = env["http"].get("/api/videos/6293447/media")
    assert r.status_code == 200
    assert r.json()["sources"][0]["kind"] == "dash"


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #
def test_download_whole_series(env) -> None:
    r = env["http"].post("/api/downloads", json={"program_id": PROGRAM.id, "quality": "720"})
    assert r.status_code == 200
    data = r.json()
    assert [j["episode"]["id"] for j in data["jobs"]] == [e.id for e in EPISODES]
    assert data["jobs"][0]["status"] == "queued" and data["jobs"][0]["quality"] == "720"
    assert env["manager"].enqueue_args[-1] == {"ids": [e.id for e in EPISODES], "quality": "720", "category": "tv"}
    # Second request: everything deduplicated by the manager.
    again = env["http"].post("/api/downloads", json={"program_id": PROGRAM.id}).json()
    assert again["jobs"] == [] and again["requested"] == 5


def test_download_seasons(env) -> None:
    r = env["http"].post("/api/downloads", json={"program_id": PROGRAM.id, "season_ids": ["PUTEMP_2"],
                                                 "category": "movies"})
    assert r.status_code == 200
    assert [j["episode"]["id"] for j in r.json()["jobs"]] == [200, 201]
    assert env["manager"].enqueue_args[-1]["category"] == "movies"


def test_download_episode_ids(env) -> None:
    http, client = env["http"], env["client"]
    r = http.post("/api/downloads", json={"program_id": PROGRAM.id, "episode_ids": [101, 101, 999]})
    assert [j["episode"]["id"] for j in r.json()["jobs"]] == [101, 999]
    assert ("get_episode", 999) in client.calls and ("get_episode", 101) not in client.calls
    r = http.post("/api/downloads", json={"episode_ids": [6293447]})
    assert r.json()["jobs"][0]["episode"]["id"] == 6293447


def test_download_validation(env) -> None:
    http = env["http"]
    assert http.post("/api/downloads", json={}).status_code == 400
    assert http.post("/api/downloads", json={"program_id": PROGRAM.id, "season_ids": []}).status_code == 400
    assert http.post("/api/downloads", json={"program_id": PROGRAM.id, "season_ids": ["PUTEMP_9"]}).status_code == 404
    assert http.post("/api/downloads", json={"program_id": PROGRAM.id, "quality": "4k"}).status_code == 422
    assert http.post("/api/downloads", json={"program_id": PROGRAM.id, "category": "music"}).status_code == 422
    assert http.post("/api/downloads", json={"program_id": "abc"}).status_code == 422


def test_job_actions(env) -> None:
    http, manager = env["http"], env["manager"]
    http.post("/api/downloads", json={"episode_ids": [1, 2]})
    assert len(http.get("/api/downloads").json()["jobs"]) == 2
    assert http.post("/api/downloads/job1/cancel").json() == {"ok": True}
    assert http.post("/api/downloads/job1/cancel").json() == {"ok": False}
    assert http.post("/api/downloads/job1/retry").json() == {"ok": True}
    assert http.post("/api/downloads/nope/cancel").status_code == 404
    assert http.post("/api/downloads/nope/retry").status_code == 404
    assert http.delete("/api/downloads/nope").status_code == 404
    assert http.delete("/api/downloads/job1").json() == {"ok": False}  # still queued
    manager.get("job2").status = JobStatus.COMPLETED
    assert http.post("/api/downloads/clear").json() == {"removed": 1}
    manager.get("job1").status = JobStatus.FAILED
    assert http.delete("/api/downloads/job1").json() == {"ok": True}
    assert http.get("/api/downloads").json() == {"jobs": []}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_settings_roundtrip(env, tmp_path: Path) -> None:
    http, store = env["http"], env["store"]
    assert http.get("/api/settings").json()["api_key"] == "abc123"
    new_dir = str(tmp_path / "Videos")
    r = http.put("/api/settings", json={"max_parallel": "4", "quality": "720p", "download_dir": new_dir,
                                        "api_key": "", "unknown": 1})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["max_parallel"] == 4 and data["quality"] == "720" and data["download_dir"] == new_dir
    assert data["api_key"] == "abc123" and "unknown" not in data
    assert store.saved and store.settings.max_parallel == 4


@pytest.mark.parametrize("changes", [
    {"max_parallel": 0}, {"max_parallel": 99}, {"quality": "4k"}, {"server_port": 80},
    {"download_dir": ""}, {"download_dir": "relatiu/carpeta"}, {"filename_template": ""},
    {"filename_template": "{series"}, {"subtitles": "potser"},
])
def test_settings_rejects_invalid(env, changes: dict) -> None:
    r = env["http"].put("/api/settings", json=changes)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert env["store"].saved == []


# --------------------------------------------------------------------------- #
# Open folder
# --------------------------------------------------------------------------- #
def test_open_folder(env, monkeypatch, tmp_path: Path) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(server, "open_in_file_manager", lambda p: opened.append(p))
    http, downloads = env["http"], env["downloads"]

    assert http.post("/api/open-folder").json() == {"ok": True}  # no body -> download dir (created)
    assert downloads.is_dir()
    sub = downloads / "Merlí" / "Season 01"
    sub.mkdir(parents=True)
    assert http.post("/api/open-folder", json={"path": str(sub)}).json() == {"ok": True}
    assert http.post("/api/open-folder", json={"path": "Merlí"}).json() == {"ok": True}  # relative to root
    assert [Path(p).name for p in opened] == [downloads.name, "Season 01", "Merlí"]

    outside = tmp_path / "secret"
    outside.mkdir()
    assert http.post("/api/open-folder", json={"path": str(outside)}).status_code == 400
    assert http.post("/api/open-folder", json={"path": str(downloads / ".." / "secret")}).status_code == 400
    assert http.post("/api/open-folder", json={"path": "C:\\Windows"}).status_code == 400
    assert http.post("/api/open-folder", json={"path": str(downloads / "no-hi-és")}).status_code == 404
    assert len(opened) == 3


def test_is_within() -> None:
    root = Path("C:/Users/x/Videos/3Cat")
    assert server.is_within(root / "a" / "b.mp4", root)
    assert server.is_within(root, root)
    assert not server.is_within(Path("C:/Users/x/Videos/3Cat2"), root)
    assert not server.is_within(Path("D:/3Cat"), root)


# --------------------------------------------------------------------------- #
# Guards, static UI, caching
# --------------------------------------------------------------------------- #
def test_host_and_origin_guard(env) -> None:
    http = env["http"]
    assert http.get("/api/health", headers={"Host": "evil.example:8765"}).status_code == 403
    assert http.get("/api/health", headers={"Host": "localhost:8765"}).status_code == 200
    assert http.post("/api/downloads/clear", headers={"Origin": "https://evil.example"}).status_code == 403
    assert http.post("/api/downloads/clear", headers={"Origin": "null"}).status_code == 403
    assert http.post("/api/downloads/clear", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200


def test_static_ui_and_cache_headers(env) -> None:
    http = env["http"]
    index = http.get("/")
    assert index.status_code == 200 and "3Cat" in index.text
    assert index.headers["cache-control"] == "no-store"
    assert http.get("/index.html").headers["cache-control"] == "no-store"
    js = http.get("/app.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert http.get("/api/downloads").headers["cache-control"] == "no-store"


def test_missing_web_dir_fallback(tmp_path: Path) -> None:
    store = FakeStore(FakeSettings(download_dir=str(tmp_path)))
    app = server.create_app(FakeClient(), FakeManager(), store, web_dir=tmp_path / "nope")
    with TestClient(app, base_url=BASE_URL) as http:
        r = http.get("/")
        assert r.status_code == 200 and "interfície" in r.text
        assert http.get("/api/health").status_code == 200


def test_resolve_web_dir_frozen(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TRESDL_WEB_DIR", raising=False)
    monkeypatch.setattr(server.sys, "frozen", True, raising=False)
    monkeypatch.setattr(server.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert server.resolve_web_dir() == tmp_path / "web"
    monkeypatch.setattr(server.sys, "frozen", False, raising=False)
    assert server.resolve_web_dir() == Path(server.__file__).resolve().parent.parent / "web"


def test_arr_router_stub_returns_501() -> None:
    from tresdl.arr.router import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as http:
        assert http.get("/arr/newznab/api", params={"t": "caps"}).status_code == 501
        assert http.get("/arr/nzb/v6293447.nzb").status_code == 501
        assert http.get("/sabnzbd/api", params={"mode": "version"}).status_code == 501
        assert http.post("/sabnzbd/api", params={"mode": "addfile"}).status_code == 501


# --------------------------------------------------------------------------- #
# Server-Sent Events (real uvicorn server on an ephemeral port)
# --------------------------------------------------------------------------- #
def _read_events(lines, count: int) -> list[tuple[str, str]]:
    """Collect ``count`` (event, data) pairs from an SSE line iterator."""
    events: list[tuple[str, str]] = []
    event, data = "", ""
    for line in lines:
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
        elif line == "" and event:
            events.append((event, data))
            event, data = "", ""
            if len(events) >= count:
                break
    return events


def test_sse_stream(tmp_path: Path, web_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(server, "KEEPALIVE_SECONDS", 0.2)
    manager = FakeManager()
    store = FakeStore(FakeSettings(download_dir=str(tmp_path)))
    app = server.create_app(FakeClient(), manager, store, web_dir=web_dir)
    manager.enqueue([EPISODES[0]])  # existing job -> sent as snapshot on connect

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                       timeout_graceful_shutdown=2))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not uv.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert uv.started

        base = f"http://127.0.0.1:{port}"
        with httpx.Client(trust_env=False, timeout=5) as http:
            with http.stream("GET", f"{base}/api/events") as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                lines = resp.iter_lines()
                snapshot = _read_events(lines, 1)
                assert snapshot[0][0] == "job" and json.loads(snapshot[0][1])["id"] == "job100"
                assert app.state.events.subscriber_count() == 1

                # Change from another thread, like a download worker does.
                job = manager.get("job100")
                job.status, job.progress = JobStatus.DOWNLOADING, 42.5
                threading.Thread(target=manager.emit, args=(job,)).start()
                update = _read_events(lines, 1)
                payload = json.loads(update[0][1])
                assert payload["status"] == "downloading" and payload["progress"] == 42.5

                # Keepalive comments arrive while idle.
                seen_keepalive = False
                for line in lines:
                    if line.startswith(": keepalive"):
                        seen_keepalive = True
                        break
                assert seen_keepalive

            # Client disconnected -> subscriber removed.
            deadline = time.monotonic() + 5
            while app.state.events.subscriber_count() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert app.state.events.subscriber_count() == 0

            # close() ends open streams so the server can stop promptly.
            with http.stream("GET", f"{base}/api/events") as resp:
                lines = resp.iter_lines()
                _read_events(lines, 1)  # snapshot
                app.state.events.close()
                rest = list(lines)  # returns because the server ended the stream
                assert not any(line.startswith("event:") for line in rest)
    finally:
        app.state.events.close()
        uv.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_broadcaster_drops_oldest_when_full() -> None:
    async def scenario() -> list[object]:
        b = server.EventBroadcaster(maxsize=2)
        sub = b.open(asyncio.get_running_loop())
        for i in range(4):
            job = DownloadJob(id=f"j{i}", episode=EPISODES[0])
            b.publish(job)
        await asyncio.sleep(0.05)
        items = []
        while not sub.queue.empty():
            items.append(json.loads(str(sub.queue.get_nowait()))["id"])
        b.discard(sub)
        b.publish(DownloadJob(id="late", episode=EPISODES[0]))
        await asyncio.sleep(0.01)
        assert sub.queue.empty()
        return items

    assert asyncio.run(scenario()) == ["j2", "j3"]


# --------------------------------------------------------------------------- #
# Regression tests (adversarial review fixes)
# --------------------------------------------------------------------------- #
def test_empty_episode_ids_is_not_the_whole_series(env) -> None:
    r = env["http"].post("/api/downloads", json={"program_id": PROGRAM.id, "episode_ids": []})
    assert r.status_code == 400 and r.json()["detail"] == "No s'ha seleccionat cap capítol."
    assert env["manager"].enqueue_args == []


def test_testserver_host_rejected_by_default(tmp_path: Path, web_dir: Path) -> None:
    store = FakeStore(FakeSettings(download_dir=str(tmp_path)))
    app = server.create_app(FakeClient(), FakeManager(), store, web_dir=web_dir)
    with TestClient(app, base_url=BASE_URL) as http:
        assert http.get("/api/settings", headers={"Host": "testserver"}).status_code == 403
        assert http.get("/api/settings", headers={"Host": "testserver:8798"}).status_code == 403
        assert http.get("/api/settings", headers={"Host": "127.0.0.1:8798"}).status_code == 200
    assert "testserver" not in server.DEFAULT_ALLOWED_HOSTS


def test_validation_errors_are_catalan(env) -> None:
    http = env["http"]
    r = http.post("/api/resolve", json={"text": "a" * 2001})
    assert r.status_code == 422
    assert r.json() == {"detail": "El text és massa llarg (màxim 2000 caràcters)."}
    r = http.get("/api/search", params={"q": "a" * 201})
    assert r.json()["detail"] == "El text és massa llarg (màxim 200 caràcters)."
    r = http.put("/api/settings", json={"max_parallel": "abc"})
    assert r.status_code == 422 and "Input should" not in r.json()["detail"]
    assert r.json()["detail"] == "Valor no vàlid per a 'max_parallel'."
    r = http.post("/api/downloads", json={"program_id": "abc"})
    assert r.status_code == 422 and r.json()["detail"] == "Valor no vàlid per a 'program_id'."


def test_put_settings_refreshes_workers(env) -> None:
    assert env["http"].put("/api/settings", json={"max_parallel": 3}).status_code == 200
    assert env["manager"].refreshed == 1
    assert env["http"].put("/api/settings", json={"max_parallel": 8}).status_code == 200


def test_settings_reject_download_dir_through_a_file(env, tmp_path: Path) -> None:
    afile = tmp_path / "afile.txt"
    afile.write_text("x", encoding="utf-8")
    r = env["http"].put("/api/settings", json={"download_dir": str(afile / "sub")})
    assert r.status_code == 422 and "és un fitxer" in r.json()["detail"]
    # A missing (e.g. unplugged) drive is still accepted.
    missing = env["http"].put("/api/settings", json={"download_dir": str(tmp_path / "no" / "existeix")})
    assert missing.status_code == 200


def test_open_folder_by_job_after_download_dir_change(env, monkeypatch, tmp_path: Path) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(server, "open_in_file_manager", lambda p: opened.append(p))
    http, manager, downloads = env["http"], env["manager"], env["downloads"]
    http.post("/api/downloads", json={"episode_ids": [1]})
    job = manager.get("job1")
    out = downloads / "Merlí" / "Season 01" / "Merlí - S01E01 - X.mp4"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"x")
    job.output_path, job.status = str(out), JobStatus.COMPLETED
    assert http.put("/api/settings", json={"download_dir": str(tmp_path / "dl2")}).status_code == 200
    # By path: outside the new download folder -> refused (unchanged behaviour).
    assert http.post("/api/open-folder", json={"path": str(out.parent)}).status_code == 400
    # By job id: allowed, the server produced the path itself.
    assert http.post("/api/open-folder", json={"job_id": "job1"}).json() == {"ok": True}
    assert opened[-1] == out
    out.unlink()                                   # file gone -> its folder is opened
    assert http.post("/api/open-folder", json={"job_id": "job1"}).json() == {"ok": True}
    assert opened[-1] == out.parent
    assert http.post("/api/open-folder", json={"job_id": "nope"}).status_code == 404


def test_naming_preview_matches_backend(env) -> None:
    from tresdl import naming

    http, downloads = env["http"], env["downloads"]
    for template in ("{series}/{date} - {title}", "{series}/{id}", "{series}/E{absolute:03d} - {title}",
                     "{series}/{title:.10}", "{series}: {title}", naming.DEFAULT_TEMPLATE):
        r = http.post("/api/naming/preview", json={"filename_template": template})
        assert r.status_code == 200 and r.json()["ok"] is True, (template, r.json())
        assert r.json()["path"] == naming.episode_path(str(downloads), server.PREVIEW_EPISODE,
                                                       "mp4", template)
    r = http.post("/api/naming/preview", json={"filename_template": "{series}/{nope}"})
    assert r.status_code == 200 and r.json()["ok"] is False and "nope" in r.json()["error"]
    r = http.post("/api/naming/preview", json={"filename_template": "{series}/{series} {season}"})
    assert r.json()["ok"] is False
