"""Tests for tresdl.downloader.

Offline tests replace the yt-dlp step with a fake; tests marked ``live``
download the short public clip 6293447 (1m46s, geo TOTS) for real.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

import httpx
import pytest

from tresdl import downloader as dl
from tresdl.config import Settings
from tresdl.downloader import DownloadManager, plan_candidates
from tresdl.models import (
    DownloadJob, Episode, JobStatus, MediaInfo, MediaSource, Subtitle,
)
from tresdl.naming import episode_path

LIVE_VIDEO_ID = 6293447
MEDIA_URL = ("https://api-media.3cat.cat/pvideo/media.jsp?media=video&versio=vast"
             "&idint={id}&profile=pc_3cat&format=dm")


# --------------------------------------------------------------------------- helpers

def make_ep(vid: int, n: int = 1, **kw) -> Episode:
    base = dict(
        id=vid, program_id=54076, program_title="Merlí", title=f"Capítol {n}",
        raw_title=f"T1xC{n} - Capítol {n}", season_id="PUTEMP_1", season_number=1,
        episode_number=n, absolute_number=n, air_date="2015-09-14",
    )
    base.update(kw)
    return Episode(**base)


def make_media(vid: int, *, dash: bool = True, mp4s: tuple[str, ...] = ("720p", "480p"),
               available: bool = True, subs: tuple[Subtitle, ...] = (),
               ad: bool = False, geo: str = "TOTS") -> MediaInfo:
    sources = []
    if dash:
        sources.append(MediaSource(f"https://example.invalid/{vid}/stream.mpd", "dash", "DASH"))
    for label in mp4s:
        sources.append(MediaSource(f"https://example.invalid/{vid}/{label}.mp4", "mp4", label,
                                   int(label.rstrip("p"))))
    ad_sources = [MediaSource(f"https://example.invalid/{vid}/aud.mp4", "mp4", "720p", 720)] if ad else []
    return MediaInfo(video_id=vid, title=f"Vídeo {vid}", available=available, geo=geo,
                     sources=sources, subtitles=list(subs), audio_description=ad_sources)


class FakeClient:
    """Stand-in for ThreeCatClient: only ``get_media`` is used by the manager."""

    def __init__(self, media: dict[int, MediaInfo | Exception] | None = None,
                 delay: float = 0.0) -> None:
        self.media = media or {}
        self.delay = delay
        self.calls: list[int] = []

    def get_media(self, video_id: int) -> MediaInfo:
        self.calls.append(video_id)
        if self.delay:
            time.sleep(self.delay)
        result = self.media.get(video_id) or make_media(video_id)
        if isinstance(result, Exception):
            raise result
        return result


class ThreeCatError(Exception):
    """Same class name as tresdl.api.ThreeCatError (matched by name)."""


class ThreeCatNotFound(ThreeCatError):
    """Subclass, like tresdl.api.ThreeCatNotFound: its message is shown as is."""


def wait_for(pred: Callable[[], bool], timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def install_fake_ytdlp(mgr: DownloadManager, *, steps: int = 3, delay: float = 0.0,
                       writer: Callable[[Path], None] | None = None) -> list:
    """Replace the yt-dlp step; returns the list of candidates used."""
    used: list = []

    def run(job_id, control, cand, base, report, media):
        for i in range(steps):
            if control.cancel.is_set():
                raise dl._Cancelled()
            Path(str(base) + ".f1.mp4.part").write_bytes(b"partial")
            time.sleep(delay)
            if report:
                mgr._update(job_id, force=False, progress=100.0 * (i + 1) / (steps + 1),
                            downloaded_bytes=i * 100, total_bytes=steps * 100)
        _remove(str(base) + ".f1.mp4.part")
        path = Path(str(base) + ".mp4")
        if writer:
            writer(path)
        else:
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 2048)
        used.append(cand)
        return path

    mgr._run_ytdlp = run  # type: ignore[method-assign]
    mgr._check_manifest = lambda source, media: None  # type: ignore[method-assign]
    return used


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def temp_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.name.startswith(dl.TEMP_PREFIX)]


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(download_dir=str(tmp_path / "videos"), max_parallel=2,
                    subtitles=False, api_key="test")


@pytest.fixture()
def make_manager(tmp_path: Path, settings: Settings):
    managers: list[DownloadManager] = []

    def factory(client=None, data_dir: Path | None = None) -> DownloadManager:
        mgr = DownloadManager(client or FakeClient(), lambda: settings,
                              data_dir=data_dir or tmp_path / "data")
        managers.append(mgr)
        return mgr

    yield factory
    for m in managers:
        m.stop(timeout=5)


def final(mgr: DownloadManager, job_id: str, timeout: float = 10.0) -> DownloadJob:
    assert wait_for(lambda: mgr.get(job_id).status in dl.FINISHED_STATES, timeout), \
        mgr.get(job_id)
    return mgr.get(job_id)


# --------------------------------------------------------------------------- source planning

def test_plan_best_prefers_dash() -> None:
    c = plan_candidates(make_media(1).sources, "best")
    assert [x.source.kind for x in c] == ["dash", "mp4", "mp4"]
    assert c[0].fmt == "bv*+ba/b"
    assert [x.source.label for x in c[1:]] == ["720p", "480p"]


def test_plan_best_without_dash_uses_highest_mp4() -> None:
    c = plan_candidates(make_media(1, dash=False, mp4s=("480p", "720p", "576p")).sources, "best")
    assert [x.source.label for x in c] == ["720p", "576p", "480p"]


def test_plan_numeric_height() -> None:
    c = plan_candidates(make_media(1).sources, "576")
    assert c[0].source.kind == "dash"
    assert c[0].fmt == "bv*[height<=576]+ba/b[height<=576]/wv*+ba/w"
    assert c[0].max_height == 576          # an MP4 <= 576 exists: DASH must fit too
    assert [x.source.label for x in c[1:]] == ["480p"]
    c = plan_candidates(make_media(1, dash=False, mp4s=("720p",)).sources, "480")
    assert [x.source.label for x in c] == ["720p"]   # nothing fits: closest taller
    c = plan_candidates(make_media(1, mp4s=("720p",)).sources, "480")
    assert c[0].max_height is None


def test_plan_mp4_only() -> None:
    c = plan_candidates(make_media(1).sources, "mp4")
    assert [x.source.label for x in c] == ["720p", "480p"]
    with pytest.raises(dl._JobFailed, match="MP4"):
        plan_candidates(make_media(1, mp4s=()).sources, "mp4")


def test_progress_aggregates_video_and_audio() -> None:
    p = dl._Progress()
    p.prepare({"duration": 10, "requested_formats": [
        {"format_id": "v", "tbr": 800}, {"format_id": "a", "tbr": 80}]})
    pct, done, total = p.update({"status": "downloading", "downloaded_bytes": 500_000,
                                 "total_bytes": 1_000_000, "info_dict": {"format_id": "v"}})
    assert total == 1_100_000 and done == 500_000 and 45 < pct < 46
    p.update({"status": "finished", "total_bytes": 1_000_000, "info_dict": {"format_id": "v"}})
    pct, _, _ = p.update({"status": "downloading", "downloaded_bytes": 50_000,
                          "total_bytes": 100_000, "info_dict": {"format_id": "a"}})
    assert 95 < pct < 96


# --------------------------------------------------------------------------- lifecycle

def test_lifecycle_completes(make_manager, settings: Settings) -> None:
    mgr = make_manager()
    install_fake_ytdlp(mgr, steps=4, delay=0.02)
    events: list[tuple[str, JobStatus]] = []
    lock = threading.Lock()

    def record(job: DownloadJob) -> None:
        with lock:
            events.append((job.id, job.status))

    def broken(job: DownloadJob) -> None:
        raise RuntimeError("subscriber bug")

    mgr.subscribe(broken)
    mgr.subscribe(record)
    jobs = mgr.enqueue([make_ep(1, 1), make_ep(2, 2)], quality="720p")
    assert [j.quality for j in jobs] == ["720", "720"]
    assert all(j.status == JobStatus.QUEUED for j in jobs)
    mgr.start()
    for job in jobs:
        done = final(mgr, job.id)
        assert done.status == JobStatus.COMPLETED, done.error
        assert done.progress == 100.0 and done.finished_at and done.error is None
        expected = episode_path(settings.download_dir, job.episode)
        assert done.output_path == expected and os.path.getsize(expected) > 2000
        with lock:
            seq = [s for i, s in events if i == job.id]
        # Emissions carry the job's *current* state, so under load an intermediate
        # state can be skipped; what arrives must still follow the lifecycle order.
        order = [JobStatus.QUEUED, JobStatus.RESOLVING, JobStatus.DOWNLOADING,
                 JobStatus.POSTPROCESSING, JobStatus.COMPLETED]
        ranks = [order.index(s) for s in seq]
        assert ranks == sorted(ranks), seq
        assert seq[-1] == JobStatus.COMPLETED, seq
    assert temp_files(Path(settings.download_dir)) == []


def test_skip_existing(make_manager, settings: Settings) -> None:
    mgr = make_manager()
    used = install_fake_ytdlp(mgr)
    ep = make_ep(5)
    target = Path(episode_path(settings.download_dir, ep))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    mgr.start()
    job = final(mgr, mgr.enqueue([ep])[0].id)
    assert job.status == JobStatus.SKIPPED and job.output_path == str(target)
    assert target.read_bytes() == b"old" and used == []

    settings.skip_existing = False
    job = final(mgr, mgr.enqueue([ep])[0].id)
    assert job.status == JobStatus.COMPLETED and target.read_bytes() != b"old"


@pytest.mark.parametrize("media, quality, message", [
    (make_media(10, available=False), "best", "no està disponible"),
    (make_media(10, dash=False, mp4s=()), "best", "cap font"),
    (make_media(10, mp4s=()), "mp4", "MP4"),
    (RuntimeError("boom"), "best", "No s'ha pogut obtenir la informació del vídeo: boom"),
    (ThreeCatError("Aquest vídeo està protegit amb DRM i no es pot descarregar."), "best",
     "^Aquest vídeo està protegit amb DRM"),
    (ThreeCatNotFound("No s'ha trobat el contingut a 3Cat."), "best",
     r"^No s'ha trobat el contingut a 3Cat\.$"),
])
def test_failures_have_catalan_messages(make_manager, media, quality, message) -> None:
    mgr = make_manager(FakeClient({10: media}))
    install_fake_ytdlp(mgr)
    mgr.start()
    job = final(mgr, mgr.enqueue([make_ep(10)], quality=quality)[0].id)
    assert job.status == JobStatus.FAILED
    assert re.search(message, job.error), job.error


def test_download_error_mapping_and_fallback(make_manager, settings: Settings) -> None:
    mgr = make_manager(FakeClient({i: make_media(i, geo="ESPANYA") for i in (11, 12)}))
    install_fake_ytdlp(mgr)
    tried: list[str] = []
    real_run = mgr._run_ytdlp

    def run(job_id, control, cand, base, report, media):
        tried.append(cand.source.label)
        if cand.source.kind == "dash":
            raise dl._map_ydl_error(Exception("ERROR: HTTP Error 403: Forbidden"), media)
        return real_run(job_id, control, cand, base, report, media)

    mgr._run_ytdlp = run
    mgr.start()
    job = final(mgr, mgr.enqueue([make_ep(11)])[0].id)
    assert job.status == JobStatus.COMPLETED and tried == ["DASH", "720p"]

    def always_403(job_id, control, cand, base, report, media):
        raise dl._map_ydl_error(Exception("HTTP Error 403: Forbidden"), media)

    mgr._run_ytdlp = always_403
    job = final(mgr, mgr.enqueue([make_ep(12, 2)])[0].id)
    assert job.status == JobStatus.FAILED
    assert job.error == dl._geo_message("ESPANYA")


def test_drm_manifest_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class Resp:
        status_code = 200
        text = '<MPD><ContentProtection schemeIdUri="urn:uuid:edef8ba9"/></MPD>'

    monkeypatch.setattr(dl.httpx, "get", lambda *a, **k: Resp())
    src = MediaSource("https://example.invalid/x.mpd", "dash", "DASH")
    with pytest.raises(dl._JobFailed, match="DRM"):
        DownloadManager._check_manifest(src, make_media(1))
    Resp.status_code, Resp.text = 403, ""
    with pytest.raises(dl._JobFailed, match="Catalunya"):
        DownloadManager._check_manifest(src, make_media(1, geo="CATALUNYA"))


def test_dedupe(make_manager) -> None:
    mgr = make_manager()
    first = mgr.enqueue([make_ep(1), make_ep(2), make_ep(1)])
    assert [j.episode.id for j in first] == [1, 2]
    assert mgr.enqueue([make_ep(1), make_ep(3)])[0].episode.id == 3
    assert len(mgr.jobs()) == 3
    mgr.cancel(first[0].id)
    assert len(mgr.enqueue([make_ep(1)])) == 1   # finished jobs do not block


def test_quality_defaults_to_settings(make_manager, settings: Settings) -> None:
    settings.quality = "576"
    mgr = make_manager()
    assert mgr.enqueue([make_ep(1)])[0].quality == "576"
    assert mgr.enqueue([make_ep(2)], quality="bogus")[0].quality == "576"
    assert mgr.enqueue([make_ep(3)], quality="MP4")[0].quality == "mp4"


# --------------------------------------------------------------------------- persistence

def test_persistence_resets_active_and_keeps_history(make_manager, tmp_path: Path) -> None:
    data = tmp_path / "persist"
    mgr = make_manager(data_dir=data)
    jobs = mgr.enqueue([make_ep(1), make_ep(2), make_ep(3)], category="tv")
    queue = json.loads((data / "queue.json").read_text(encoding="utf-8"))
    assert [j["id"] for j in queue["jobs"]] == [j.id for j in jobs]
    queue["jobs"][0]["status"] = "downloading"
    queue["jobs"][0]["progress"] = 42.0
    queue["jobs"][1]["status"] = "completed"
    queue["jobs"][1]["future_field"] = 1
    queue["jobs"].append({"garbage": True})
    (data / "queue.json").write_text(json.dumps(queue), encoding="utf-8")

    mgr2 = make_manager(data_dir=data)
    restored = {j.id: j for j in mgr2.jobs()}
    assert len(restored) == 3
    assert restored[jobs[0].id].status == JobStatus.QUEUED
    assert restored[jobs[0].id].progress == 0.0
    assert restored[jobs[1].id].status == JobStatus.COMPLETED
    assert restored[jobs[2].id].episode == jobs[2].episode
    on_disk = json.loads((data / "queue.json").read_text(encoding="utf-8"))
    assert on_disk["jobs"][0]["status"] == "queued"


# --------------------------------------------------------------------------- cancel / stop / retry

def test_cancel_queued(make_manager) -> None:
    mgr = make_manager()
    job = mgr.enqueue([make_ep(1)])[0]
    assert mgr.cancel(job.id) is True
    assert mgr.get(job.id).status == JobStatus.CANCELLED
    assert mgr.cancel(job.id) is False
    assert mgr.cancel("nope") is False


def test_cancel_running(make_manager, settings: Settings) -> None:
    mgr = make_manager()
    install_fake_ytdlp(mgr, steps=500, delay=0.01)
    mgr.start()
    job = mgr.enqueue([make_ep(1)])[0]
    assert wait_for(lambda: mgr.get(job.id).progress > 0)
    t0 = time.monotonic()
    assert mgr.cancel(job.id)
    done = final(mgr, job.id, timeout=5)
    assert done.status == JobStatus.CANCELLED and time.monotonic() - t0 < 2
    assert temp_files(Path(settings.download_dir)) == []
    assert not os.path.exists(episode_path(settings.download_dir, job.episode))
    # The series/season folders created only for this job are removed again.
    assert not (Path(settings.download_dir) / "Merlí").exists()


def test_cancel_keeps_folders_in_use_or_preexisting(make_manager, settings: Settings) -> None:
    root = Path(settings.download_dir)
    (root / "Merlí").mkdir(parents=True)          # existed before: never removed
    mgr = make_manager()
    install_fake_ytdlp(mgr, steps=300, delay=0.01)
    mgr.start()
    a, b = mgr.enqueue([make_ep(1, 1), make_ep(2, 2)])
    assert wait_for(lambda: mgr.get(a.id).progress > 0 and mgr.get(b.id).progress > 0)
    assert mgr.cancel(a.id)
    assert final(mgr, a.id, timeout=5).status == JobStatus.CANCELLED
    season = root / "Merlí" / "Season 01"
    assert season.is_dir()                         # still used by job b
    assert mgr.cancel(b.id)
    assert final(mgr, b.id, timeout=5).status == JobStatus.CANCELLED
    assert not season.exists()                     # created by the manager, now empty
    assert (root / "Merlí").is_dir()


def test_last_event_per_job_is_its_final_state(make_manager, settings: Settings) -> None:
    """Events from enqueue() and from workers must never arrive out of order."""
    for n in range(1, 21):  # already on disk -> the worker finishes almost instantly
        path = Path(episode_path(settings.download_dir, make_ep(n, n)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    settings.max_parallel = 4
    mgr = make_manager()
    install_fake_ytdlp(mgr)
    last: dict[str, JobStatus] = {}
    mgr.subscribe(lambda job: last.__setitem__(job.id, job.status))
    mgr.start()
    jobs = mgr.enqueue([make_ep(n, n) for n in range(1, 21)])
    for job in jobs:
        assert final(mgr, job.id).status == JobStatus.SKIPPED
    time.sleep(0.2)
    assert {last[j.id] for j in jobs} == {JobStatus.SKIPPED}


def test_stop_is_prompt_and_requeues(make_manager, tmp_path: Path) -> None:
    data = tmp_path / "stopdata"
    mgr = make_manager(data_dir=data)
    install_fake_ytdlp(mgr, steps=1000, delay=0.01)
    mgr.start()
    job = mgr.enqueue([make_ep(1)])[0]
    assert wait_for(lambda: mgr.get(job.id).status == JobStatus.DOWNLOADING)
    t0 = time.monotonic()
    mgr.stop()
    assert time.monotonic() - t0 < 3
    assert mgr.get(job.id).status == JobStatus.QUEUED
    assert make_manager(data_dir=data).get(job.id).status == JobStatus.QUEUED


def test_retry_remove_clear(make_manager) -> None:
    client = FakeClient({1: make_media(1, available=False)})
    mgr = make_manager(client)
    install_fake_ytdlp(mgr)
    mgr.start()
    job = mgr.enqueue([make_ep(1)])[0]
    assert final(mgr, job.id).status == JobStatus.FAILED
    assert mgr.remove("nope") is False

    client.media[1] = make_media(1)
    assert mgr.retry(job.id) is True
    assert final(mgr, job.id).status == JobStatus.COMPLETED
    assert mgr.get(job.id).error is None

    mgr.stop()  # stopped first, so the new job deterministically stays queued
    pending = mgr.enqueue([make_ep(2)])[0]
    assert mgr.remove(pending.id) is False          # still queued
    assert mgr.retry(pending.id) is False
    assert mgr.clear_finished() == 1
    assert [j.id for j in mgr.jobs()] == [pending.id]
    mgr.cancel(pending.id)
    assert mgr.remove(pending.id) is True and mgr.jobs() == []


def test_progress_notifications_are_throttled(make_manager) -> None:
    mgr = make_manager()
    job = mgr.enqueue([make_ep(1)])[0]
    seen: list[DownloadJob] = []
    mgr.subscribe(seen.append)
    mgr._controls[job.id] = dl._Control(last_notify=time.monotonic())
    for i in range(200):
        mgr._update(job.id, force=False, progress=float(i) / 2)
    assert len(seen) <= 1
    assert mgr.get(job.id).progress == 99.5     # state still updated
    mgr._update(job.id, status=JobStatus.DOWNLOADING)
    assert seen[-1].status == JobStatus.DOWNLOADING


# --------------------------------------------------------------------------- subtitles / AD

class _SubResp:
    status_code = 200
    content = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHola\n"
    text = content.decode()

    def raise_for_status(self) -> None:
        return None


def test_subtitle_sidecar_and_audio_description(make_manager, settings: Settings,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl.httpx, "get", lambda *a, **k: _SubResp())
    settings.subtitles = True
    settings.audio_description = True
    subs = (Subtitle("https://example.invalid/a.vtt", "ca", "Català"),
            Subtitle("https://example.invalid/b.vtt", "ca", "Català (SPS)"))
    mgr = make_manager(FakeClient({7: make_media(7, subs=subs, ad=True)}))
    used = install_fake_ytdlp(mgr)
    mgr.start()
    job = final(mgr, mgr.enqueue([make_ep(7)])[0].id)
    assert job.status == JobStatus.COMPLETED, job.error
    out = Path(job.output_path)
    assert out.with_name(out.stem + ".ca.vtt").read_bytes().startswith(b"WEBVTT")
    assert out.with_name(out.stem + ".ca.2.vtt").exists()
    assert out.with_name(out.stem + " (Audiodescripció).mp4").exists()
    assert len(used) == 2
    assert temp_files(Path(settings.download_dir)) == []


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _make_tiny_mp4(path: Path) -> None:
    subprocess.run(
        [_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=5",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-c:v", "libx264", "-c:a", "aac",
         "-shortest", str(path)],
        check=True, capture_output=True, timeout=60,
    )


def _probe(path: str) -> str:
    proc = subprocess.run([_ffmpeg(), "-hide_banner", "-i", path], capture_output=True, timeout=60)
    return proc.stderr.decode("utf-8", "replace")


def test_embed_subtitles_with_ffmpeg(make_manager, settings: Settings,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dl.httpx, "get", lambda *a, **k: _SubResp())
    settings.subtitles = True
    settings.embed_subtitles = True
    subs = (Subtitle("https://example.invalid/a.vtt", "ca", "Català"),)
    mgr = make_manager(FakeClient({8: make_media(8, subs=subs)}))
    install_fake_ytdlp(mgr, writer=_make_tiny_mp4)
    mgr.start()
    job = final(mgr, mgr.enqueue([make_ep(8)])[0].id, timeout=60)
    assert job.status == JobStatus.COMPLETED, job.error
    out = Path(job.output_path)
    assert not list(out.parent.glob("*.vtt"))
    info = _probe(str(out))
    assert "Subtitle: mov_text" in info and "(cat)" in info
    assert temp_files(Path(settings.download_dir)) == []


# --------------------------------------------------------------------------- live

def _live_media(video_id: int) -> MediaInfo:
    """Build a MediaInfo straight from media.jsp (independent of tresdl.api)."""
    data = httpx.get(MEDIA_URL.format(id=video_id), timeout=20).json()
    sources = []
    for entry in data["media"]["url"]:
        label = entry["label"]
        if label.upper() == "DASH":
            sources.append(MediaSource(entry["file"], "dash", label))
        else:
            m = re.search(r"\d+", label)
            sources.append(MediaSource(entry["file"], "mp4", label, int(m.group()) if m else None))
    subs = [Subtitle(s["url"], s.get("iso", "ca"), s.get("text", ""))
            for s in (data.get("subtitols") or [])]
    info = data["informacio"]
    return MediaInfo(video_id=video_id, title=info["titol"], available=info["estat"]["actiu"],
                     geo=data["media"].get("geo", ""), sources=sources, subtitles=subs)


@pytest.fixture(scope="module")
def live_media() -> MediaInfo:
    return _live_media(LIVE_VIDEO_ID)


def _live_episode(media: MediaInfo) -> Episode:
    return Episode(id=LIVE_VIDEO_ID, program_id=0, program_title="Telenotícies",
                   title=media.title, raw_title=media.title, air_date="2024-07-21")


@pytest.mark.live
def test_live_real_download(make_manager, settings: Settings, live_media: MediaInfo) -> None:
    assert live_media.available and live_media.sources
    settings.subtitles = True
    mgr = make_manager(FakeClient({LIVE_VIDEO_ID: live_media}))
    states: list[JobStatus] = []
    mgr.subscribe(lambda j: states.append(j.status))
    mgr.start()
    job = final(mgr, mgr.enqueue([_live_episode(live_media)], quality="480")[0].id, timeout=300)
    assert job.status == JobStatus.COMPLETED, job.error
    out = Path(job.output_path)
    assert out.exists() and out.stat().st_size > 1_000_000
    with open(out, "rb") as fh:
        assert fh.read(12)[4:8] == b"ftyp"
    probe = _probe(str(out))
    assert "Video:" in probe and "Audio:" in probe
    assert out.with_name(out.stem + ".ca.vtt").exists()
    assert JobStatus.DOWNLOADING in states
    assert temp_files(Path(settings.download_dir)) == []


@pytest.mark.live
def test_live_cancel(make_manager, settings: Settings, live_media: MediaInfo) -> None:
    mgr = make_manager(FakeClient({LIVE_VIDEO_ID: live_media}))
    mgr.start()
    job = mgr.enqueue([_live_episode(live_media)], quality="best")[0]
    assert wait_for(lambda: mgr.get(job.id).downloaded_bytes > 0
                    or mgr.get(job.id).status in dl.FINISHED_STATES, timeout=60)
    assert mgr.cancel(job.id)
    done = final(mgr, job.id, timeout=20)
    assert done.status == JobStatus.CANCELLED, done.error
    assert temp_files(Path(settings.download_dir)) == []
    assert not os.path.exists(done.output_path)


# --------------------------------------------------------------------------- regression

def test_same_name_videos_get_distinct_paths(make_manager, settings: Settings) -> None:
    """Language copies / re-uploads with identical S, E and title never overwrite each other."""
    settings.max_parallel = 2
    mgr = make_manager()
    install_fake_ytdlp(mgr, steps=3, delay=0.02)
    mgr.start()
    a = make_ep(5420251, 38, title="Bufalo Bill")
    b = make_ep(6309873, 38, title="Bufalo Bill")
    ja, jb = mgr.enqueue([a, b])
    da, db = final(mgr, ja.id), final(mgr, jb.id)
    assert da.status == db.status == JobStatus.COMPLETED
    assert da.output_path == episode_path(settings.download_dir, a)
    assert db.output_path.endswith("Bufalo Bill [6309873].mp4")
    assert os.path.exists(da.output_path) and os.path.exists(db.output_path)

    # Re-running the same listing skips both (names are stable), never downloading anything.
    used = install_fake_ytdlp(mgr)
    ra, rb = mgr.enqueue([a, b])
    assert final(mgr, ra.id).status == final(mgr, rb.id).status == JobStatus.SKIPPED
    assert (mgr.get(ra.id).output_path, mgr.get(rb.id).output_path) == (da.output_path, db.output_path)
    assert used == []


def test_name_taken_by_finished_job_of_other_video(make_manager, settings: Settings) -> None:
    mgr = make_manager()
    install_fake_ytdlp(mgr)
    mgr.start()
    first = final(mgr, mgr.enqueue([make_ep(1, 1, title="Igual")])[0].id)
    second = final(mgr, mgr.enqueue([make_ep(2, 1, title="Igual")])[0].id)
    assert first.status == JobStatus.COMPLETED
    assert second.status == JobStatus.COMPLETED, "must not be reported as skipped"
    assert second.output_path.endswith("Igual [2].mp4")


def test_template_collisions_are_disambiguated(make_manager, settings: Settings) -> None:
    settings.filename_template = "{series}/{title}"
    mgr = make_manager()
    install_fake_ytdlp(mgr)
    mgr.start()
    jobs = mgr.enqueue([make_ep(n, n, title="Mateix títol") for n in (1, 2, 3)])
    done = [final(mgr, j.id) for j in jobs]
    assert [d.status for d in done] == [JobStatus.COMPLETED] * 3
    assert len({d.output_path for d in done}) == 3


def test_stop_does_not_undo_a_user_cancel(make_manager, settings: Settings, tmp_path: Path) -> None:
    data = tmp_path / "cancelstop"
    mgr = make_manager(FakeClient(delay=0.5), data_dir=data)
    install_fake_ytdlp(mgr)
    mgr.start()
    job = mgr.enqueue([make_ep(1)])[0]
    assert wait_for(lambda: mgr.get(job.id).status == JobStatus.RESOLVING)
    assert mgr.cancel(job.id)
    mgr.stop(timeout=5)
    assert mgr.get(job.id).status == JobStatus.CANCELLED
    assert make_manager(data_dir=data).get(job.id).status == JobStatus.CANCELLED
    assert temp_files(Path(settings.download_dir)) == []


def test_stop_timeout_keeps_user_cancel(make_manager, tmp_path: Path) -> None:
    data = tmp_path / "cancelstop2"
    mgr = make_manager(FakeClient(delay=2.0), data_dir=data)
    install_fake_ytdlp(mgr)
    mgr.start()
    job = mgr.enqueue([make_ep(1)])[0]
    assert wait_for(lambda: mgr.get(job.id).status == JobStatus.RESOLVING)
    assert mgr.cancel(job.id)
    mgr.stop(timeout=0.1)                       # the worker is still blocked in get_media
    assert make_manager(data_dir=data).get(job.id).status == JobStatus.CANCELLED


def test_raising_max_parallel_applies_to_queued_work(make_manager, settings: Settings) -> None:
    settings.max_parallel = 1
    mgr = make_manager()
    install_fake_ytdlp(mgr, steps=300, delay=0.01)
    mgr.start()
    jobs = mgr.enqueue([make_ep(n, n) for n in range(1, 5)])

    def active() -> int:
        return sum(1 for j in jobs if mgr.get(j.id).status in dl.ACTIVE_STATES)

    assert wait_for(lambda: active() == 1)
    settings.max_parallel = 3
    mgr.refresh_workers()
    assert wait_for(lambda: active() == 3, timeout=5)
    for j in jobs:
        mgr.cancel(j.id)


def test_retry_then_cancel_at_end_of_run_really_cancels(make_manager) -> None:
    client = FakeClient({1: RuntimeError("boom")})
    mgr = make_manager(client)
    install_fake_ytdlp(mgr)
    results: dict[str, object] = {}

    def on_change(job: DownloadJob) -> None:
        if job.status == JobStatus.FAILED and "retry" not in results:
            results["retry"] = mgr.retry(job.id)
            results["cancel"] = mgr.cancel(job.id)
            results["after"] = mgr.get(job.id).status

    mgr.subscribe(on_change)
    mgr.start()
    job = mgr.enqueue([make_ep(1)])[0]
    assert wait_for(lambda: "after" in results)
    time.sleep(0.5)
    assert results == {"retry": True, "cancel": True, "after": JobStatus.CANCELLED}
    assert mgr.get(job.id).status == JobStatus.CANCELLED
    assert client.calls == [1]


def _blocking_ytdlp(mgr: DownloadManager) -> None:
    def run(job_id, control, cand, base, report, media):
        Path(str(base) + ".f137.mp4.part").write_bytes(b"partial" * 100)
        mgr._update(job_id, progress=10.0)
        while not control.cancel.wait(0.02):
            pass
        raise dl._Cancelled()

    mgr._run_ytdlp = run  # type: ignore[method-assign]
    mgr._check_manifest = lambda source, media: None  # type: ignore[method-assign]


def test_cancel_after_restart_deletes_kept_partials(make_manager, settings: Settings,
                                                    tmp_path: Path) -> None:
    data = tmp_path / "partials"
    mgr = make_manager(data_dir=data)
    _blocking_ytdlp(mgr)
    mgr.start()
    job = mgr.enqueue([make_ep(1)])[0]
    assert wait_for(lambda: mgr.get(job.id).progress >= 10.0)
    mgr.stop()
    assert mgr.get(job.id).status == JobStatus.QUEUED
    assert len(temp_files(Path(settings.download_dir))) == 1          # kept for resuming

    settings.filename_template = "{series}/Altres/{series} - {title}"  # changed meanwhile
    again = make_manager(data_dir=data)                                # not started
    assert again.cancel(job.id) is True
    assert again.get(job.id).status == JobStatus.CANCELLED
    assert temp_files(Path(settings.download_dir)) == []


def test_queue_saves_are_debounced(make_manager, monkeypatch: pytest.MonkeyPatch,
                                   tmp_path: Path) -> None:
    data = tmp_path / "debounce"
    mgr = make_manager(data_dir=data)
    jobs = mgr.enqueue([make_ep(n, n) for n in range(1, 2001)])
    writes: list[int] = []
    real_write = dl.atomic_write_json
    target = os.path.normcase(str(data / "queue.json"))

    def counting_write(path, payload, **kw):
        if os.path.normcase(str(path)) == target:       # ignore other tests' managers
            writes.append(1)
        real_write(path, payload, **kw)

    monkeypatch.setattr(dl, "atomic_write_json", counting_write)
    job_id = jobs[0].id
    for status in (JobStatus.RESOLVING, JobStatus.DOWNLOADING, JobStatus.POSTPROCESSING):
        mgr._set_status(job_id, status)
    mgr._finalize(job_id, None, JobStatus.COMPLETED)
    assert wait_for(lambda: len(writes) >= 1, timeout=3)
    time.sleep(dl.SAVE_DEBOUNCE + 0.3)
    assert len(writes) <= 2
    mgr.stop()
    on_disk = json.loads((data / "queue.json").read_text(encoding="utf-8"))
    assert on_disk["jobs"][0]["status"] == "completed" and len(on_disk["jobs"]) == 2000


def test_unusable_download_dir_fails_in_catalan(make_manager, settings: Settings,
                                                tmp_path: Path) -> None:
    afile = tmp_path / "afile.txt"
    afile.write_text("x", encoding="utf-8")
    settings.download_dir = str(afile / "sub")
    mgr = make_manager()
    install_fake_ytdlp(mgr)
    mgr.start()
    job = final(mgr, mgr.enqueue([make_ep(1)])[0].id)
    assert job.status == JobStatus.FAILED
    assert job.error.startswith("No es pot crear o escriure a la carpeta de descàrregues")
    assert mgr._created_dirs == set()


def test_cancel_during_subtitles_leaves_no_sidecar(make_manager, settings: Settings,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    settings.subtitles = True
    settings.embed_subtitles = False
    subs = (Subtitle("https://example.invalid/a.vtt", "ca", "Català"),)
    mgr = make_manager(FakeClient({9: make_media(9, subs=subs)}))
    install_fake_ytdlp(mgr)
    holder: dict[str, str] = {}

    def get(*_a, **_k):
        mgr.cancel(holder["id"])
        return _SubResp()

    monkeypatch.setattr(dl.httpx, "get", get)
    # Enqueue before starting: the worker must not reach get() before the id is known.
    holder["id"] = mgr.enqueue([make_ep(9)])[0].id
    mgr.start()
    job = final(mgr, holder["id"])
    assert job.status == JobStatus.CANCELLED
    assert list(Path(settings.download_dir).rglob("*")) == []


class _StallHandler(BaseHTTPRequestHandler):
    """Sends 300 KB of a 5 MB "video" and then stops answering."""

    stop = threading.Event()

    def log_message(self, *_args) -> None:  # keep test output clean
        pass

    def _headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(5_000_000))
        self.send_header("Accept-Ranges", "none")
        self.end_headers()

    def do_HEAD(self) -> None:
        self._headers()

    def do_GET(self) -> None:
        self._headers()
        try:
            self.wfile.write(b"\x00\x00\x00\x18ftypmp42" + b"x" * 300_000)
            self.wfile.flush()
        except OSError:
            return
        self.stop.wait(60)


def test_cancel_is_prompt_while_the_network_stalls(make_manager, settings: Settings,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Real yt-dlp against a stalling local server: a cancel must not wait for all retries."""
    monkeypatch.setattr(dl, "SOCKET_TIMEOUT", 2.0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StallHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/video.mp4"
        media = MediaInfo(video_id=77, title="Stall", available=True, geo="TOTS",
                          sources=[MediaSource(url, "mp4", "720p", 720)])
        mgr = make_manager(FakeClient({77: media}))
        mgr.start()
        job = mgr.enqueue([make_ep(77)], quality="mp4")[0]
        assert wait_for(lambda: mgr.get(job.id).downloaded_bytes > 100_000, timeout=30), mgr.get(job.id)
        time.sleep(0.5)                              # now stalled inside a socket read
        t0 = time.monotonic()
        assert mgr.cancel(job.id)
        done = final(mgr, job.id, timeout=dl.SOCKET_TIMEOUT + 8)
        assert done.status == JobStatus.CANCELLED
        assert time.monotonic() - t0 < dl.SOCKET_TIMEOUT + 6
        assert temp_files(Path(settings.download_dir)) == []
    finally:
        _StallHandler.stop.set()
        server.shutdown()
        server.server_close()


def test_explicit_target_for_arr_releases(make_manager, settings: Settings, tmp_path: Path) -> None:
    """Sonarr/Radarr jobs write exactly to their release folder and are not deduped
    against a normal download of the same video."""
    target = tmp_path / "videos" / "tv" / "Merli.S01E01.REL" / "Merli.S01E01.REL.mp4"
    mgr = make_manager()
    install_fake_ytdlp(mgr)
    normal = mgr.enqueue([make_ep(1)])
    arr = mgr.enqueue([make_ep(1)], category="tv", targets={1: str(target)})
    again = mgr.enqueue([make_ep(1)], targets={1: str(target)})
    assert len(normal) == 1 and len(arr) == 1 and again == []
    mgr.start()
    done = final(mgr, arr[0].id)
    assert done.status == JobStatus.COMPLETED and done.output_path == str(target) and target.is_file()
    assert done.target == str(target)
    plain = final(mgr, normal[0].id)
    assert plain.status == JobStatus.COMPLETED and plain.output_path != str(target)
