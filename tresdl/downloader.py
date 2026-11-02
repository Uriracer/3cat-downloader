"""Download manager: persistent queue, worker threads, yt-dlp, progress, cancel, retry.

Job lifecycle::

    QUEUED -> RESOLVING (client.get_media) -> DOWNLOADING -> POSTPROCESSING
           -> COMPLETED | FAILED | CANCELLED | SKIPPED

* Workers pick the oldest QUEUED job; the pool size follows
  ``settings.max_parallel`` (re-read whenever work is added and when
  :meth:`DownloadManager.refresh_workers` is called after a settings change).
* Files are downloaded to a short temporary name inside the target folder
  (``.3catdl-<job>...``) and moved to the final path with ``os.replace``.
  Subtitle sidecars are only given their final names after the video.
* Two different videos never share an output path: when they would (language
  versions, re-uploads, a template without an episode number...), the later
  one in queue order is saved as ``<name> [<video id>].mp4``.
* The queue is persisted to ``<data_dir>/queue.json``; jobs that were active
  when the app stopped are resumed as QUEUED on the next start. Writes are
  debounced (state changes mark the queue dirty; a writer thread saves it)
  and flushed synchronously by ``enqueue`` and ``stop``.
* Subscribers are called from worker threads, outside the internal lock.
  Progress notifications are throttled (~4/s per job); state changes are not.

DRM-protected or geo-blocked content is never bypassed: such jobs fail with a
clear Catalan message.
"""
from __future__ import annotations

import copy
import gc
import glob
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

import httpx

from . import naming
from .config import QUALITIES, Settings, app_data_dir, atomic_write_json
from .models import DownloadJob, Episode, JobStatus, MediaInfo, MediaSource, Subtitle

if TYPE_CHECKING:  # pragma: no cover - typing only (api.py is owned elsewhere)
    from .api import ThreeCatClient

log = logging.getLogger(__name__)

QUEUE_FILE = "queue.json"
QUEUE_VERSION = 1
NOTIFY_INTERVAL = 0.25          # seconds between progress notifications per job
TEMP_PREFIX = ".3catdl-"
SOCKET_TIMEOUT = 10.0           # seconds; also bounds how long a cancel waits during a stall
SAVE_DEBOUNCE = 0.5             # seconds; queue.json writes are coalesced over this window
WRITER_IDLE_EXIT = 5.0          # seconds without changes before the writer thread exits

ACTIVE_STATES = frozenset({JobStatus.RESOLVING, JobStatus.DOWNLOADING, JobStatus.POSTPROCESSING})
PENDING_STATES = frozenset({JobStatus.QUEUED}) | ACTIVE_STATES
FINISHED_STATES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SKIPPED}
)

_GEO_AREAS = {"ESPANYA": "d'Espanya", "CATALUNYA": "de Catalunya"}
_ISO639_2 = {"ca": "cat", "es": "spa", "en": "eng", "oc": "oci", "fr": "fre", "de": "ger"}
_DRM_MARKERS = ("ContentProtection", "SAMPLE-AES", "com.widevine", "com.apple.streamingkeydelivery")


# --------------------------------------------------------------------------- errors

class _Cancelled(Exception):
    """Raised (e.g. from the yt-dlp progress hook) to abort a running job."""


class _JobFailed(Exception):
    """A job failure with a user-facing (Catalan) message."""


class _TryNext(Exception):
    """The current candidate does not satisfy the quality; try the next one."""


# --------------------------------------------------------------------------- helpers

@dataclass
class _Control:
    """Per-active-job control block."""
    cancel: threading.Event = field(default_factory=threading.Event)
    reason: str = "user"            # "user" | "shutdown"
    last_notify: float = 0.0
    temp_paths: list[str] = field(default_factory=list)   # temp globs to clean up


@dataclass
class _Candidate:
    """A source plus the yt-dlp format selector to use for it."""
    source: MediaSource
    fmt: str
    max_height: Optional[int] = None   # skip candidate if the selection is taller


class _YdlLogger:
    """Routes yt-dlp output to ``logging`` instead of stdout/stderr."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.info("yt-dlp error: %s", msg)


class _Progress:
    """Aggregates per-format progress (video + audio for DASH) into one value."""

    def __init__(self) -> None:
        self.expected: dict[str, float] = {}
        self.total: dict[str, float] = {}
        self.done: dict[str, float] = {}

    def prepare(self, info: dict) -> None:
        duration = info.get("duration") or 0
        for f in info.get("requested_formats") or [info]:
            key = str(f.get("format_id") or "main")
            size = f.get("filesize") or f.get("filesize_approx")
            if not size and f.get("tbr") and duration:
                size = float(f["tbr"]) * 1000 / 8 * float(duration)
            self.expected[key] = float(size or 0)

    def update(self, d: dict) -> tuple[Optional[float], int, Optional[int]]:
        info = d.get("info_dict") or {}
        key = str(info.get("format_id") or d.get("filename") or "main")
        downloaded = float(d.get("downloaded_bytes") or 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if d.get("status") == "finished":
            total = total or downloaded
            downloaded = float(total)
        if total:
            self.total[key] = float(total)
        self.done[key] = downloaded
        keys = set(self.expected) | set(self.total) | set(self.done)
        total_all = sum(self.total.get(k) or self.expected.get(k) or 0 for k in keys)
        done_all = sum(self.done.get(k, 0.0) for k in keys)
        percent: Optional[float] = None
        if total_all > 0:
            percent = min(99.9, max(0.0, done_all / total_all * 100))
        elif d.get("fragment_count"):
            percent = min(99.9, 100.0 * (d.get("fragment_index") or 0) / d["fragment_count"])
        return percent, int(done_all), (int(total_all) if total_all > 0 else None)


def _is_threecat_error(exc: BaseException) -> bool:
    """True for ``api.ThreeCatError`` and its subclasses (e.g. ``ThreeCatNotFound``).

    Matched by class name so this module does not need to import ``api``.
    """
    return any(cls.__name__ == "ThreeCatError" for cls in type(exc).__mro__)


def _source_height(src: MediaSource) -> Optional[int]:
    if src.height:
        return src.height
    m = re.search(r"(\d{3,4})", src.label or "")
    return int(m.group(1)) if m else None


def plan_candidates(sources: list[MediaSource], quality: str) -> list[_Candidate]:
    """Ordered download candidates for ``quality`` (raises ``_JobFailed``).

    * ``best``    -> DASH/HLS (best video+audio) if present, else highest MP4.
    * ``<height>``-> closest height <= requested (DASH via a height-limited
      format selector, MP4s by label); the closest taller one if none fits.
    * ``mp4``     -> direct MP4 files only.
    """
    adaptive = sorted(
        (s for s in sources if s.kind in ("dash", "hls")), key=lambda s: s.kind != "dash"
    )
    mp4s = sorted(
        (s for s in sources if s.kind == "mp4"),
        key=lambda s: _source_height(s) or 0, reverse=True,
    )
    if quality == "mp4":
        if not mp4s:
            raise _JobFailed(
                "Aquest vídeo no té cap versió MP4 directa. Tria una altra qualitat."
            )
        return [_Candidate(s, "b") for s in mp4s]
    if quality.isdigit():
        h = int(quality)
        fmt = f"bv*[height<={h}]+ba/b[height<={h}]/wv*+ba/w"
        fitting = [s for s in mp4s if (_source_height(s) or 0) <= h]
        taller = sorted((s for s in mp4s if (_source_height(s) or 0) > h),
                        key=lambda s: _source_height(s) or 0)
        if fitting:
            return [_Candidate(s, fmt, max_height=h) for s in adaptive] + [
                _Candidate(s, "b") for s in fitting
            ]
        return [_Candidate(s, fmt) for s in adaptive] + [_Candidate(s, "b") for s in taller]
    # "best"
    return [_Candidate(s, "bv*+ba/b") for s in adaptive] + [_Candidate(s, "b") for s in mp4s]


def _geo_message(geo: str) -> Optional[str]:
    area = _GEO_AREAS.get((geo or "").upper())
    if area:
        return (f"Aquest vídeo només es pot descarregar des {area} "
                "(restricció geogràfica de 3cat).")
    return None


def _forbidden_message(media: MediaInfo) -> str:
    return _geo_message(media.geo) or (
        "El servidor de 3cat ha denegat l'accés al vídeo (HTTP 403)."
    )


def _clean_ydl_error(exc: BaseException) -> str:
    msg = str(exc).strip()
    msg = re.sub(r"^(ERROR:\s*)+", "", msg)
    return re.sub(r"\x1b\[[0-9;]*m", "", msg)[:300]


def _map_ydl_error(exc: Exception, media: MediaInfo) -> _JobFailed:
    """Translate a yt-dlp error into a user-facing failure."""
    msg = _clean_ydl_error(exc)
    if "403" in msg or "Forbidden" in msg:
        return _JobFailed(_forbidden_message(media))
    if "404" in msg:
        return _JobFailed("El vídeo ja no existeix al servidor de 3cat (HTTP 404).")
    if "DRM" in msg:
        return _JobFailed("Aquest vídeo està protegit amb DRM i no es pot descarregar.")
    return _JobFailed(f"Error de descàrrega: {msg}")


def _job_from_dict(data: dict) -> Optional[DownloadJob]:
    """Rebuild a job from ``to_dict()`` output; ``None`` if unusable."""
    try:
        ep_data = data["episode"]
        ep_fields = {f.name for f in fields(Episode)}
        episode = Episode(**{k: v for k, v in ep_data.items() if k in ep_fields})
        job_fields = {f.name for f in fields(DownloadJob)} - {"episode", "status"}
        values = {k: v for k, v in data.items() if k in job_fields}
        return DownloadJob(episode=episode, status=JobStatus(data["status"]), **values)
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("Dropping unreadable persisted job: %s", exc)
        return None


def _ffmpeg_exe() -> str:
    """ffmpeg path (``TRESDL_FFMPEG`` → bundled imageio-ffmpeg → PATH)."""
    from .runtime import ffmpeg_exe

    try:
        return ffmpeg_exe()
    except RuntimeError as exc:
        raise _JobFailed(str(exc)) from exc


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- manager

class DownloadManager:
    """Thread-safe download queue backed by yt-dlp."""

    def __init__(
        self,
        client: "ThreeCatClient",
        settings_getter: Callable[[], Settings],
        *,
        data_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._client = client
        self._settings_getter = settings_getter
        self._data_dir = Path(data_dir) if data_dir is not None else app_data_dir()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._save_lock = threading.Lock()
        self._emit_lock = threading.RLock()  # keeps subscriber notifications ordered
        self._jobs: dict[str, DownloadJob] = {}
        self._controls: dict[str, _Control] = {}
        self._subscribers: list[Callable[[DownloadJob], None]] = []
        self._workers: dict[int, threading.Thread] = {}
        self._running = False
        # Output folders in use by running jobs (refcount), so a cancelled job
        # never removes a folder another job is about to write into.
        self._dir_lock = threading.Lock()
        self._dirs_in_use: dict[str, int] = {}
        self._created_dirs: set[str] = set()
        # Private per-job data persisted next to each job in queue.json
        # ("unique": use the "[<id>]" file name; "temps": partial-file bases
        # kept by a shutdown, so they can be deleted if the job is cancelled).
        self._extras: dict[str, dict[str, Any]] = {}
        # Output paths (normcase) claimed by running jobs: job id -> path.
        self._claims: dict[str, str] = {}
        # Debounced persistence.
        self._save_requested = False
        self._dirty = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self._load()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the worker pool (idempotent)."""
        with self._cond:
            self._running = True
        self._ensure_workers()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop workers promptly; in-flight jobs are interrupted and re-queued.

        A job the user already cancelled stays cancelled: its control keeps
        the "user" reason, and if its worker does not finish within
        ``timeout`` the job is marked CANCELLED here, so it is not resumed on
        the next start.
        """
        user_cancelled: list[str] = []
        with self._cond:
            self._running = False
            for job_id, control in self._controls.items():
                if control.cancel.is_set():
                    user_cancelled.append(job_id)   # cancel() already ran: keep "user"
                else:
                    control.reason = "shutdown"
                control.cancel.set()
            self._cond.notify_all()
            workers = list(self._workers.values())
        deadline = time.monotonic() + timeout
        for t in workers:
            if t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            for job_id in user_cancelled:
                job = self._jobs.get(job_id)
                if job is not None and job.status not in FINISHED_STATES:
                    self._finish_locked(job, JobStatus.CANCELLED)
        self._save()

    # ------------------------------------------------------------------ public API

    def subscribe(self, cb: Callable[[DownloadJob], None]) -> None:
        """Register ``cb(job_snapshot)``; called from worker threads on every change."""
        with self._lock:
            self._subscribers.append(cb)

    def unsubscribe(self, cb: Callable[[DownloadJob], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(cb)
            except ValueError:
                pass

    def enqueue(
        self, episodes: list[Episode], quality: str | None = None, category: str = "tv",
        targets: Optional[dict[int, str]] = None,
    ) -> list[DownloadJob]:
        """Queue ``episodes``; returns the newly created jobs.

        Episodes whose video id already has a queued/active job (for the same
        target) are skipped. ``targets`` maps video ids to explicit output files
        (Sonarr/Radarr releases); those never get a ``[<id>]`` name.
        """
        targets = dict(targets or {})
        quality = self._normalise_quality(quality)
        settings = self._settings()
        # Output paths are rendered outside the lock (about 0.1 ms each, so a
        # whole daily programme would otherwise block the queue for seconds).
        with self._lock:
            pending = [(j.id, j.episode, bool(self._extras.get(j.id, {}).get("unique")))
                       for j in self._jobs.values() if j.status in PENDING_STATES and not j.target]
        owners: dict[str, int] = {}
        for _job_id, ep, unique in pending:
            key = self._path_key(settings, ep, unique)
            if key is not None:
                owners.setdefault(key, ep.id)
        new_keys = {ep.id: self._path_key(settings, ep, False) for ep in episodes if ep.id not in targets}

        created: list[DownloadJob] = []
        with self._cond:
            busy = {(j.episode.id, j.target) for j in self._jobs.values() if j.status in PENDING_STATES}
            for ep in episodes:
                target = targets.get(ep.id)
                if (ep.id, target) in busy:
                    continue
                busy.add((ep.id, target))
                job = DownloadJob(
                    id=uuid.uuid4().hex, episode=ep, quality=quality,
                    created_at=time.time(), category=category or "tv", target=target,
                )
                # Same output path as an earlier job of another video (queue
                # order: pending jobs, then this list in order) -> "[<id>]" name.
                key = new_keys.get(ep.id)
                if key is not None and owners.setdefault(key, ep.id) != ep.id:
                    self._extras.setdefault(job.id, {})["unique"] = True
                self._jobs[job.id] = job
                created.append(job)
            snapshots = [copy.copy(j) for j in created]
            self._cond.notify_all()
        if created:
            self._save()
            self._ensure_workers()
            for snap in snapshots:
                self._emit(snap)
        return snapshots

    @staticmethod
    def _path_key(settings: Settings, ep: Episode, unique: bool) -> Optional[str]:
        """Normalised output path of ``ep`` under ``settings`` (None if unrenderable).

        The order of a listing is stable, so the first video keeps the plain
        name across runs and ``skip_existing`` keeps working.
        """
        try:
            path = naming.episode_path(settings.download_dir, ep, "mp4", settings.filename_template)
        except ValueError:
            return None
        if unique:
            path = naming.disambiguate(path, ep.id)
        return os.path.normcase(path)

    def jobs(self) -> list[DownloadJob]:
        with self._lock:
            return [copy.copy(j) for j in self._jobs.values()]

    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.copy(job) if job else None

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns False if already finished/unknown.

        A QUEUED job is never running (a worker moves a job to RESOLVING in
        the same critical section in which it registers its control, and
        unregisters it together with the final status), so it is cancelled
        directly; partial files a previous shutdown kept for it are deleted.
        """
        snap = None
        kept: list[str] = []
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.status in FINISHED_STATES:
                return False
            control = self._controls.get(job_id)
            if job.status == JobStatus.QUEUED:
                self._finish_locked(job, JobStatus.CANCELLED)
                snap = copy.copy(job)
                kept = list(self._extras.get(job_id, {}).pop("temps", []) or [])
            elif control is not None:
                control.reason = "user"
                control.cancel.set()
        if snap is not None:
            self._delete_partials(snap, kept)
            self._request_save()
            self._emit(snap)
        return True

    def _delete_partials(self, job: DownloadJob, kept: list[str]) -> None:
        """Best effort: delete the temp files of a job that is not running.

        ``kept`` are the temp bases recorded when a shutdown kept partial
        files; the base for the current settings is tried as well.
        """
        bases = [str(b) for b in kept]
        settings = self._settings()
        try:
            out_dir = Path(job.target or naming.episode_path(
                settings.download_dir, job.episode, "mp4", settings.filename_template)).parent
            bases.append(str(out_dir / f"{TEMP_PREFIX}{job.id[:12]}"))
        except ValueError:
            pass
        bases = [b for b in dict.fromkeys(bases) if Path(b).name.startswith(TEMP_PREFIX)]
        if bases:
            self._cleanup(bases)

    def refresh_workers(self) -> None:
        """Apply a changed ``max_parallel`` to the running pool (idempotent).

        Extra workers above a lowered limit exit after their current job;
        a raised limit starts the missing workers at once.
        """
        self._ensure_workers()

    def retry(self, job_id: str) -> bool:
        """Re-queue a finished job (unless another job for the same video is pending)."""
        with self._cond:
            job = self._jobs.get(job_id)
            if job is None or job.status not in FINISHED_STATES:
                return False
            if any(j.episode.id == job.episode.id and j.target == job.target
                   and j.status in PENDING_STATES for j in self._jobs.values()):
                return False
            self._reset_locked(job)
            snap = copy.copy(job)
            self._cond.notify_all()
        self._request_save()
        self._ensure_workers()
        self._emit(snap)
        return True

    def remove(self, job_id: str) -> bool:
        """Forget a finished/failed/cancelled/skipped job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status not in FINISHED_STATES:
                return False
            del self._jobs[job_id]
            self._extras.pop(job_id, None)
        self._request_save()
        return True

    def clear_finished(self) -> int:
        """Remove every job in a terminal state; returns how many were removed."""
        with self._lock:
            ids = [i for i, j in self._jobs.items() if j.status in FINISHED_STATES]
            for i in ids:
                del self._jobs[i]
                self._extras.pop(i, None)
        if ids:
            self._request_save()
        return len(ids)

    # ------------------------------------------------------------------ internals: state

    def _settings(self) -> Settings:
        try:
            return self._settings_getter()
        except Exception:  # pragma: no cover - defensive
            log.exception("settings_getter failed; using defaults")
            return Settings()

    def _normalise_quality(self, quality: str | None) -> str:
        q = str(quality or "").strip().lower().removesuffix("p")
        if q in QUALITIES:
            return q
        q = str(self._settings().quality).lower()
        return q if q in QUALITIES else "best"

    @staticmethod
    def _reset_locked(job: DownloadJob) -> None:
        job.status = JobStatus.QUEUED
        job.progress = 0.0
        job.downloaded_bytes = 0
        job.total_bytes = None
        job.speed = None
        job.eta = None
        job.error = None
        job.finished_at = None

    @staticmethod
    def _finish_locked(job: DownloadJob, status: JobStatus, error: str | None = None) -> None:
        job.status = status
        job.error = error
        job.speed = None
        job.eta = None
        job.finished_at = time.time()
        if status in (JobStatus.COMPLETED, JobStatus.SKIPPED):
            job.progress = 100.0

    def _update(self, job_id: str, *, force: bool = True, **changes: Any) -> None:
        """Apply ``changes`` to a job and notify (throttled unless ``force``)."""
        now = time.monotonic()
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in changes.items():
                setattr(job, k, v)
            control = self._controls.get(job_id)
            if not force and control is not None and now - control.last_notify < NOTIFY_INTERVAL:
                return
            if control is not None:
                control.last_notify = now
            snap = copy.copy(job)
        self._emit(snap)

    def _set_status(self, job_id: str, status: JobStatus, **changes: Any) -> None:
        """In-run transition (RESOLVING/DOWNLOADING/POSTPROCESSING) of the running job."""
        self._update(job_id, status=status, **changes)
        self._request_save()

    def _finalize(self, job_id: str, control: Optional[_Control], status: JobStatus,
                  error: str | None = None, **changes: Any) -> None:
        """Publish the end of a run atomically.

        The terminal status (or QUEUED after a shutdown), the extra fields,
        the removal of the job's control and the release of its output-path
        claim happen in one critical section. Hence a ``retry()``/``cancel()``
        racing with the end of a run never sees a finished job that still
        has a (stale) control, and the worker never overwrites a re-queued job.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if status == JobStatus.QUEUED:
                self._reset_locked(job)
            else:
                self._finish_locked(job, status, error)
            for k, v in changes.items():
                setattr(job, k, v)
            if control is not None and self._controls.get(job_id) is control:
                del self._controls[job_id]
            self._claims.pop(job_id, None)
            snap = copy.copy(job)
            self._cond.notify_all()
        self._request_save()
        self._emit(snap)

    def _emit(self, snap: DownloadJob) -> None:
        """Notify subscribers about a job change.

        Emissions are serialised and always carry the job's *current* state
        (``snap`` is only used if the job was removed meanwhile). Without this,
        a snapshot taken by one thread (e.g. "queued" from ``enqueue``) could be
        delivered after a newer one from a worker ("skipped"), leaving clients
        with a stale final state.
        """
        with self._emit_lock:
            with self._lock:
                current = self._jobs.get(snap.id)
                if current is not None:
                    snap = copy.copy(current)
                subscribers = list(self._subscribers)
            for cb in subscribers:
                try:
                    cb(snap)
                except Exception:
                    log.exception("Download subscriber failed")

    # ------------------------------------------------------------------ internals: persistence

    @property
    def queue_path(self) -> Path:
        return self._data_dir / QUEUE_FILE

    def _load(self) -> None:
        try:
            with open(self.queue_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Cannot read %s (%s); starting with an empty queue", self.queue_path, exc)
            return
        items = data.get("jobs", []) if isinstance(data, dict) else []
        changed = False
        for item in items if isinstance(items, list) else []:
            job = _job_from_dict(item) if isinstance(item, dict) else None
            if job is None:
                continue
            if job.status in ACTIVE_STATES:
                self._reset_locked(job)
                changed = True
            self._jobs[job.id] = job
            extra = item.get("_extra")
            if isinstance(extra, dict) and extra:
                self._extras[job.id] = dict(extra)
        if changed:
            self._save()

    def _request_save(self) -> None:
        """Mark the queue dirty; the writer thread saves it within ~SAVE_DEBOUNCE."""
        with self._lock:
            self._dirty.set()
            if self._writer is None:
                self._writer = threading.Thread(target=self._writer_loop,
                                                name="3cat-queue-writer", daemon=True)
                self._writer.start()

    def _writer_loop(self) -> None:
        while True:
            if not self._dirty.wait(timeout=WRITER_IDLE_EXIT):
                with self._lock:
                    if not self._dirty.is_set():
                        self._writer = None
                        return
                continue
            time.sleep(SAVE_DEBOUNCE)       # coalesce bursts of state changes
            self._dirty.clear()
            self._save()

    def _save(self) -> None:
        """Write queue.json now (synchronous).

        Only shallow copies are taken under ``_lock``; serialising (the slow
        part for queues of thousands of jobs) happens outside it.
        """
        with self._save_lock:
            with self._lock:
                jobs = [copy.copy(j) for j in self._jobs.values()]
                extras = {k: dict(v) for k, v in self._extras.items() if v}
            items = []
            for job in jobs:
                item = job.to_dict()
                if job.id in extras:
                    item["_extra"] = extras[job.id]
                items.append(item)
            try:
                atomic_write_json(self.queue_path, {"version": QUEUE_VERSION, "jobs": items},
                                  compact=True)
            except OSError as exc:
                log.warning("Cannot save download queue: %s", exc)

    # ------------------------------------------------------------------ internals: workers

    def _max_parallel(self) -> int:
        try:
            return max(1, int(self._settings().max_parallel))
        except (TypeError, ValueError):
            return 1

    def _ensure_workers(self) -> None:
        limit = self._max_parallel()
        with self._cond:
            if not self._running:
                return
            for index in range(limit):
                t = self._workers.get(index)
                if t is None or not t.is_alive():
                    t = threading.Thread(
                        target=self._worker_loop, args=(index,),
                        name=f"3cat-download-{index}", daemon=True,
                    )
                    self._workers[index] = t
                    t.start()
            self._cond.notify_all()

    def _worker_loop(self, index: int) -> None:
        while True:
            limit = self._max_parallel()
            with self._cond:
                if not self._running or index >= limit:
                    if self._workers.get(index) is threading.current_thread():
                        del self._workers[index]
                    return
                job = next((j for j in self._jobs.values()
                            if j.status == JobStatus.QUEUED and j.id not in self._controls), None)
                if job is None:
                    self._cond.wait(timeout=1.0)
                    continue
                control = _Control()
                self._controls[job.id] = control
                job.status = JobStatus.RESOLVING
                job.error = None
                job_id = job.id
            try:
                self._run_job(job_id, control)
            except Exception:  # pragma: no cover - last-resort guard
                log.exception("Unexpected error in download worker")
            finally:
                with self._lock:
                    # Normally already done by _finalize; never drop the
                    # control (or claim) of a newer run of the same job.
                    if self._controls.get(job_id) is control:
                        del self._controls[job_id]
                        self._claims.pop(job_id, None)

    # ------------------------------------------------------------------ internals: one job

    def _run_job(self, job_id: str, control: _Control) -> None:
        with self._lock:
            job = copy.copy(self._jobs[job_id])
        ep = job.episode
        settings = self._settings()
        self._set_status(job_id, JobStatus.RESOLVING, progress=0.0, error=None)
        temps: list[str] = []
        held_dir: Optional[Path] = None  # output folder marked in use (see _acquire_dir)
        failure: Optional[str] = None   # None after an exception = interrupted
        try:
            try:
                media = self._client.get_media(ep.id)
            except Exception as exc:
                if control.cancel.is_set():
                    raise _Cancelled() from exc
                if _is_threecat_error(exc) and str(exc):
                    raise _JobFailed(str(exc)) from exc  # already user-facing (Catalan)
                raise _JobFailed(f"No s'ha pogut obtenir la informació del vídeo: {exc}") from exc
            self._check_cancel(control)
            if not media.available:
                raise _JobFailed("Aquest vídeo ja no està disponible a 3cat (pot haver caducat).")
            if not media.sources:
                raise _JobFailed("No s'ha trobat cap font de vídeo per a aquest capítol.")

            if job.target:  # Sonarr/Radarr release: exact file, never renamed
                output = job.target
                with self._lock:
                    self._claims[job_id] = os.path.normcase(output)
            else:
                output = self._claim_output(job_id, ep, naming.episode_path(
                    settings.download_dir, ep, "mp4", settings.filename_template
                ))
            if settings.skip_existing and os.path.exists(output):
                self._finalize(job_id, control, JobStatus.SKIPPED, output_path=output)
                return
            candidates = plan_candidates(media.sources, job.quality)
            out_dir = Path(output).parent
            self._acquire_dir(out_dir)
            held_dir = out_dir

            self._set_status(job_id, JobStatus.DOWNLOADING, output_path=output)
            base = out_dir / f"{TEMP_PREFIX}{job_id[:12]}"
            temps.append(str(base))
            video_tmp = self._download_candidates(job_id, control, media, candidates, base, True)

            ad_tmp: Optional[Path] = None
            ad_path = naming.audio_description_path(output)
            if settings.audio_description and media.audio_description and not (
                settings.skip_existing and os.path.exists(ad_path)
            ):
                ad_base = out_dir / f"{TEMP_PREFIX}{job_id[:12]}-ad"
                temps.append(str(ad_base))
                try:
                    ad_q = job.quality if job.quality != "mp4" or any(
                        s.kind == "mp4" for s in media.audio_description) else "best"
                    ad_tmp = self._download_candidates(
                        job_id, control, media,
                        plan_candidates(media.audio_description, ad_q), ad_base, False,
                    )
                except _JobFailed as exc:
                    log.warning("Audio description download failed for %s: %s", ep.id, exc)

            self._set_status(job_id, JobStatus.POSTPROCESSING, progress=99.9, speed=None, eta=None)
            sidecars: list[tuple[str, str]] = []
            if settings.subtitles and media.subtitles:
                video_tmp, sidecars = self._handle_subtitles(
                    control, media.subtitles, video_tmp, output, base, settings.embed_subtitles
                )
            self._check_cancel(control)
            self._move_into_place(video_tmp, output)
            if ad_tmp is not None:
                self._move_into_place(ad_tmp, ad_path)
            # Sidecars get their final names only once the video is in place,
            # so a cancel/failure never leaves a subtitle without its video.
            for tmp, final_path in sidecars:
                try:
                    os.replace(tmp, final_path)
                except OSError as exc:
                    log.warning("Cannot save subtitle %s: %s", final_path, exc)
                    _remove_quietly(tmp)
            self._cleanup(temps)
            self._release_dir(held_dir, remove_empty=False)
            held_dir = None
            with self._lock:
                self._extras.get(job_id, {}).pop("temps", None)
            self._finalize(job_id, control, JobStatus.COMPLETED, output_path=output)
            return
        except _Cancelled:
            failure = None
        except _JobFailed as exc:
            failure = None if control.cancel.is_set() else str(exc)
        except Exception as exc:
            failure = None if control.cancel.is_set() else f"Error inesperat: {exc}"
            if failure:
                log.exception("Download of video %s failed", ep.id)
        # Outside the except blocks: the traceback (and the file handles its
        # frames may still hold) is released before temp files are deleted.
        gc.collect()
        shutdown = failure is None and control.reason == "shutdown"
        if held_dir is not None:
            # Cancelled/failed: delete the temp files and drop the (now empty)
            # folders this job created *before* the final status is published.
            # On shutdown keep both, so yt-dlp can resume the partial files.
            if not shutdown:
                self._cleanup(temps)
            self._release_dir(held_dir, remove_empty=not shutdown)
        if failure is not None:
            self._fail(job_id, control, failure, temps)
        else:
            self._on_interrupted(job_id, control, temps)

    def _claim_output(self, job_id: str, ep: Episode, output: str) -> str:
        """Reserve ``output`` for this job, or a ``[<id>]`` variant if it is taken.

        The plain name is taken when the job was flagged at enqueue time, when
        another running job claimed it, or when another video's finished job
        (completed/skipped) already produced it. Without this, two videos with
        the same name would overwrite each other, or the second would be
        reported as "skipped" although it was never downloaded.
        """
        with self._lock:
            unique = bool(self._extras.get(job_id, {}).get("unique"))
            key = os.path.normcase(output)
            if not unique:
                owner_ids = {self._jobs[j].episode.id for j, k in self._claims.items()
                             if k == key and j != job_id and j in self._jobs}
                for other in self._jobs.values():
                    if (other.id != job_id and other.output_path
                            and other.status in (JobStatus.COMPLETED, JobStatus.SKIPPED)
                            and os.path.normcase(other.output_path) == key):
                        owner_ids.add(other.episode.id)
                owner_ids.discard(ep.id)
                unique = bool(owner_ids)
                if unique:
                    log.info("Output name %s already used by video(s) %s; saving %s as [%s]",
                             output, sorted(owner_ids), ep.id, ep.id)
            if unique:
                output = naming.disambiguate(output, ep.id)
                key = os.path.normcase(output)
            self._claims[job_id] = key
        return output

    @staticmethod
    def _move_into_place(tmp: Path | str, final_path: str) -> None:
        try:
            os.replace(tmp, final_path)
        except OSError as exc:
            raise _JobFailed(
                f"No s'ha pogut desar el fitxer a «{final_path}». Comprova que el disc està "
                "connectat, que hi ha espai i que tens permisos d'escriptura."
            ) from exc

    def _acquire_dir(self, out_dir: Path) -> None:
        """Create ``out_dir`` (and parents) and mark it in use by one job.

        Folders that did not exist before are remembered so that
        :meth:`_release_dir` may remove them again if they end up empty
        (only once they were really created). An unusable folder fails the
        job with a Catalan message.
        """
        with self._dir_lock:
            missing: list[str] = []
            probe = out_dir
            while not probe.exists() and probe.parent != probe:
                missing.append(os.path.normcase(str(probe)))
                probe = probe.parent
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise _JobFailed(
                    f"No es pot crear o escriure a la carpeta de descàrregues «{out_dir}». "
                    "Comprova que el disc està connectat i que tens permisos d'escriptura."
                ) from exc
            self._created_dirs.update(missing)
            key = os.path.normcase(str(out_dir))
            self._dirs_in_use[key] = self._dirs_in_use.get(key, 0) + 1

    def _release_dir(self, out_dir: Path, *, remove_empty: bool) -> None:
        """Undo :meth:`_acquire_dir`.

        With ``remove_empty`` and no other job using ``out_dir``, the empty
        folders created by this manager are removed, deepest first. Folders
        that existed before (or that are not empty) are never touched.
        """
        with self._dir_lock:
            key = os.path.normcase(str(out_dir))
            count = self._dirs_in_use.get(key, 0) - 1
            if count > 0:
                self._dirs_in_use[key] = count
                return
            self._dirs_in_use.pop(key, None)
            if not remove_empty:
                return
            folder = out_dir
            while os.path.normcase(str(folder)) in self._created_dirs:
                try:
                    folder.rmdir()  # only succeeds if empty
                except OSError:
                    break
                self._created_dirs.discard(os.path.normcase(str(folder)))
                folder = folder.parent

    def _fail(self, job_id: str, control: _Control, message: str, temps: list[str]) -> None:
        self._cleanup(temps)
        with self._lock:
            self._extras.get(job_id, {}).pop("temps", None)
        self._finalize(job_id, control, JobStatus.FAILED, message)

    def _on_interrupted(self, job_id: str, control: _Control, temps: list[str]) -> None:
        if control.reason == "shutdown":
            # Keep partial files so yt-dlp can resume after restart, and
            # remember where they are in case the job is cancelled instead.
            with self._lock:
                if temps:
                    self._extras.setdefault(job_id, {})["temps"] = list(temps)
            self._finalize(job_id, control, JobStatus.QUEUED)
            return
        self._cleanup(temps)
        with self._lock:
            self._extras.get(job_id, {}).pop("temps", None)
        self._finalize(job_id, control, JobStatus.CANCELLED)

    @staticmethod
    def _check_cancel(control: _Control) -> None:
        if control.cancel.is_set():
            raise _Cancelled()

    @staticmethod
    def _cleanup(bases: list[str]) -> None:
        """Delete temp files; retries briefly (Windows keeps closing handles)."""
        for attempt in range(10):
            left = [p for base in bases for p in glob.glob(glob.escape(base) + "*")]
            for path in left:
                _remove_quietly(path)
            if not any(os.path.exists(p) for p in left):
                return
            gc.collect()
            time.sleep(0.1 * (attempt + 1))
        log.warning("Could not delete temporary files: %s", left)

    # ------------------------------------------------------------------ internals: download

    def _download_candidates(
        self, job_id: str, control: _Control, media: MediaInfo,
        candidates: list[_Candidate], base: Path, report: bool,
    ) -> Path:
        """Try each candidate in order; return the downloaded temp file."""
        first_error: Optional[str] = None
        for cand in candidates:
            self._check_cancel(control)
            error: Optional[str] = None
            try:
                if cand.source.kind in ("dash", "hls"):
                    self._check_manifest(cand.source, media)
                return self._run_ytdlp(job_id, control, cand, base, report, media)
            except _TryNext:
                continue
            except _JobFailed as exc:
                if control.cancel.is_set():
                    raise _Cancelled() from None
                error = str(exc)
            log.info("Candidate %s failed: %s", cand.source.label, error)
            first_error = first_error or error
            self._cleanup([str(base)])
        raise _JobFailed(first_error or "No s'ha trobat cap font de vídeo adequada.")

    @staticmethod
    def _check_manifest(source: MediaSource, media: MediaInfo) -> None:
        """Refuse DRM-protected manifests; map 403 to a clear (geo) error."""
        try:
            resp = httpx.get(source.url, timeout=20.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            log.debug("Manifest pre-check failed (%s); letting yt-dlp try", exc)
            return
        if resp.status_code in (401, 403):
            raise _JobFailed(_forbidden_message(media))
        if resp.status_code == 404:
            raise _JobFailed("El vídeo ja no existeix al servidor de 3cat (HTTP 404).")
        if any(marker in resp.text for marker in _DRM_MARKERS):
            raise _JobFailed(
                "Aquest vídeo està protegit amb DRM i no es pot descarregar."
            )

    def _run_ytdlp(
        self, job_id: str, control: _Control, cand: _Candidate, base: Path,
        report: bool, media: MediaInfo,
    ) -> Path:
        import yt_dlp
        from yt_dlp.utils import DownloadError

        progress = _Progress()

        def hook(d: dict) -> None:
            if control.cancel.is_set():
                raise _Cancelled()
            if not report or d.get("status") not in ("downloading", "finished"):
                return
            percent, done, total = progress.update(d)
            changes: dict[str, Any] = {"downloaded_bytes": done, "total_bytes": total}
            if percent is not None:
                changes["progress"] = round(percent, 1)
            if d.get("status") == "downloading":
                changes["speed"] = d.get("speed")
                eta = d.get("eta")
                changes["eta"] = int(eta) if eta is not None else None
            self._update(job_id, force=False, **changes)

        def retry_sleep(n: int = 0) -> float:
            # yt-dlp calls this before every retry (also while the network is
            # stalled, when no progress hook runs): honour a cancel there.
            delay = min(0.5 * (2 ** n), 4.0)
            if control.cancel.wait(delay):
                raise _Cancelled()
            return 0.0

        opts: dict[str, Any] = {
            "format": cand.fmt,
            "outtmpl": {"default": str(base).replace("%", "%%") + ".%(ext)s"},
            "merge_output_format": "mp4",
            "ffmpeg_location": _ffmpeg_exe(),
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "logger": _YdlLogger(),
            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": SOCKET_TIMEOUT,
            "retry_sleep_functions": {"http": retry_sleep, "fragment": retry_sleep,
                                      "file_access": retry_sleep, "extractor": retry_sleep},
            "noplaylist": True,
            "continuedl": True,
            "cachedir": False,
            "updatetime": False,
            "progress_hooks": [hook],
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(cand.source.url, download=False)
                self._check_cancel(control)
                if cand.max_height is not None:
                    heights = [f.get("height") for f in (info.get("requested_formats") or [info])]
                    if any(h and h > cand.max_height for h in heights):
                        raise _TryNext()
                progress.prepare(info)
                info = ydl.process_ie_result(info, download=True)
        except (_Cancelled, _TryNext):
            raise
        except DownloadError as exc:
            if control.cancel.is_set():
                raise _Cancelled() from exc
            raise _map_ydl_error(exc, media) from exc
        except Exception as exc:
            if control.cancel.is_set():
                raise _Cancelled() from exc
            raise _JobFailed(f"Error de descàrrega: {_clean_ydl_error(exc)}") from exc
        self._check_cancel(control)

        path: Optional[str] = None
        for item in info.get("requested_downloads") or []:
            path = item.get("filepath") or path
        if not path or not os.path.exists(path):
            matches = [p for p in glob.glob(glob.escape(str(base)) + ".*")
                       if not p.endswith((".part", ".ytdl")) and ".f" not in Path(p).name[len(base.name):]]
            path = max(matches, key=os.path.getsize) if matches else None
        if not path or not os.path.exists(path):
            raise _JobFailed("La descàrrega ha acabat però no s'ha trobat el fitxer.")
        return Path(path)

    # ------------------------------------------------------------------ internals: subtitles

    def _handle_subtitles(
        self, control: _Control, subs: list[Subtitle], video_tmp: Path,
        output: str, base: Path, embed: bool,
    ) -> tuple[Path, list[tuple[str, str]]]:
        """Download VTTs as sidecars, or embed them (mov_text).

        Returns ``(video_path, sidecars)``; ``sidecars`` are ``(temp, final)``
        pairs the caller renames only after the video is in place. Temp names
        start with ``base`` so a cancel/failure cleanup removes them.
        """
        fetched: list[tuple[Subtitle, str, str]] = []
        used: set[str] = set()
        for index, sub in enumerate(subs):
            self._check_cancel(control)
            lang = (sub.lang or "und").lower()
            final = naming.subtitle_path(output, lang, "vtt")
            n = 2
            while final in used:
                final = naming.subtitle_path(output, f"{lang}.{n}", "vtt")
                n += 1
            used.add(final)
            tmp = f"{base}.sub{index}.vtt"
            try:
                resp = httpx.get(sub.url, timeout=30.0, follow_redirects=True)
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    fh.write(resp.content)
            except (httpx.HTTPError, OSError) as exc:
                log.warning("Cannot download subtitle %s: %s", sub.url, exc)
                _remove_quietly(tmp)
                continue
            fetched.append((sub, tmp, final))
        self._check_cancel(control)
        sidecars = [(tmp, final) for _sub, tmp, final in fetched]
        if not embed or not fetched:
            return video_tmp, sidecars

        muxed = Path(f"{base}.muxed.mp4")
        if self._mux_subtitles(control, video_tmp, [(sub, tmp) for sub, tmp, _f in fetched], muxed):
            _remove_quietly(str(video_tmp))
            for _sub, tmp, _final in fetched:
                _remove_quietly(tmp)
            return muxed, []
        # Embedding failed: fall back to sidecar files.
        _remove_quietly(str(muxed))
        return video_tmp, sidecars

    @staticmethod
    def _mux_subtitles(
        control: _Control, video: Path, subs: list[tuple[Subtitle, str]], out: Path
    ) -> bool:
        cmd = [_ffmpeg_exe(), "-y", "-nostdin", "-nostats", "-loglevel", "error", "-i", str(video)]
        for _sub, path in subs:
            cmd += ["-i", path]
        cmd += ["-map", "0:v?", "-map", "0:a?"]
        for i in range(len(subs)):
            cmd += ["-map", f"{i + 1}:0"]
        cmd += ["-c", "copy", "-c:s", "mov_text"]
        for i, (sub, _path) in enumerate(subs):
            lang = _ISO639_2.get((sub.lang or "").lower(), (sub.lang or "und").lower())
            cmd += [f"-metadata:s:s:{i}", f"language={lang}"]
            if sub.label:
                cmd += [f"-metadata:s:s:{i}", f"title={sub.label}"]
        cmd += ["-movflags", "+faststart", str(out)]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags
            )
        except OSError as exc:
            log.warning("Cannot run ffmpeg: %s", exc)
            return False
        while proc.poll() is None:
            if control.cancel.wait(0.2):
                proc.kill()
                proc.wait()
                raise _Cancelled()
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        if proc.returncode != 0:
            log.warning("ffmpeg subtitle mux failed (%s): %s", proc.returncode, stderr[-500:])
            return False
        return out.exists() and out.stat().st_size > 0
