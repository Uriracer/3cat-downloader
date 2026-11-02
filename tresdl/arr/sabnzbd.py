"""SABnzbd-compatible download client on top of :class:`DownloadManager`.

One SABnzbd job (``nzo_id``) = one Sonarr/Radarr release = one or more
:class:`DownloadJob` (a season pack has one per episode). Files go to
``<download dir>/<category>/<release name>/`` so Sonarr/Radarr import the
folder SABnzbd reports as ``storage``.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from ..models import DownloadJob, Episode, JobStatus
from ..naming import sanitize
from .releases import Token, estimate_size

log = logging.getLogger(__name__)

SAB_VERSION = "4.5.0"
GROUPS_FILE = "arr_jobs.json"
DEFAULT_CATEGORIES = ("tv", "movies", "sonarr", "radarr")
ACTIVE = {JobStatus.QUEUED, JobStatus.RESOLVING, JobStatus.DOWNLOADING, JobStatus.POSTPROCESSING}
OK = {JobStatus.COMPLETED, JobStatus.SKIPPED}


class SabManager(Protocol):
    def enqueue(self, episodes: list[Episode], quality: str | None = None, category: str = "tv",
                targets: Optional[dict[int, str]] = None) -> list[DownloadJob]: ...
    def get(self, job_id: str) -> DownloadJob | None: ...
    def cancel(self, job_id: str) -> bool: ...
    def retry(self, job_id: str) -> bool: ...
    def remove(self, job_id: str) -> bool: ...
    def jobs(self) -> list[DownloadJob]: ...


class SabClient(Protocol):
    def get_episode(self, video_id: int) -> Episode: ...


class SabError(Exception):
    """Reported to Sonarr/Radarr as ``{"status": false, "error": ...}``."""


@dataclass
class ArrGroup:
    nzo_id: str
    title: str
    category: str
    folder: str
    job_ids: list[str] = field(default_factory=list)
    video_ids: list[int] = field(default_factory=list)
    created_at: float = 0.0
    finished_at: Optional[float] = None
    error: Optional[str] = None


def category_dir(category: str) -> str:
    return sanitize(category or "tv") or "tv"


def _fmt_timeleft(seconds: Optional[float]) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _folder_size(folder: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return path.resolve() != root.resolve()
    except (ValueError, OSError):
        return False


class SabnzbdService:
    def __init__(self, client: SabClient, manager: SabManager, settings_getter: Callable[[], Any],
                 data_dir: Path | str | None = None) -> None:
        self._client = client
        self._manager = manager
        self._settings = settings_getter
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._lock = threading.RLock()
        self._groups: dict[str, ArrGroup] = {}
        self._load()

    # -------------------------------------------------------------- storage
    def _path(self) -> Path:
        if self._data_dir is None:
            from ..config import app_data_dir

            self._data_dir = app_data_dir()
        return self._data_dir / GROUPS_FILE

    def _load(self) -> None:
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
            for raw in data.get("groups", []):
                group = ArrGroup(**{k: v for k, v in raw.items() if k in ArrGroup.__dataclass_fields__})
                self._groups[group.nzo_id] = group
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Cannot read %s: %s", GROUPS_FILE, exc)

    def _save(self) -> None:
        from ..config import atomic_write_json

        with self._lock:
            data = {"groups": [asdict(g) for g in self._groups.values()]}
        try:
            atomic_write_json(self._path(), data, compact=True)
        except OSError as exc:
            log.warning("Cannot save %s: %s", GROUPS_FILE, exc)

    # -------------------------------------------------------------- config
    def complete_dir(self) -> str:
        return str(Path(os.path.expandvars(os.path.expanduser(self._settings().download_dir))))

    def config(self) -> dict:
        categories = [{"name": "*", "order": 0, "pp": "3", "script": "None", "dir": "", "priority": 0}]
        for i, name in enumerate(DEFAULT_CATEGORIES, start=1):
            categories.append({"name": name, "order": i, "pp": "", "script": "Default",
                               "dir": category_dir(name), "priority": -100})
        return {"config": {
            "misc": {
                "complete_dir": self.complete_dir(), "download_dir": self.complete_dir(),
                "tv_categories": [], "enable_tv_sorting": False,
                "movie_categories": [], "enable_movie_sorting": False,
                "date_categories": [], "enable_date_sorting": False,
                "pre_check": False, "history_retention": "",
                "history_retention_option": "all", "history_retention_number": 0,
            },
            "categories": categories, "servers": [], "sorters": [],
        }}

    # -------------------------------------------------------------- add
    def add(self, token: Token, category: Optional[str], quality: Optional[str] = None) -> str:
        """Queue the videos of ``token``; returns the new ``nzo_id``."""
        category = (category or token.category or "tv").strip() or "tv"
        if category == "*":
            category = token.category or "tv"
        folder = Path(self.complete_dir()) / category_dir(category) / (sanitize(token.title) or "3cat")
        episodes: list[Episode] = []
        targets: dict[int, str] = {}
        for vid in token.videos:
            try:
                ep = self._client.get_episode(vid)
            except Exception as exc:
                raise SabError(f"No s'ha pogut obtenir el vídeo {vid} de 3cat: {exc}") from exc
            name = token.names.get(str(vid)) or token.title
            targets[vid] = str(folder / f"{sanitize(name) or vid}.mp4")
            episodes.append(ep)
        nzo_id = f"SABnzbd_nzo_3cat{uuid.uuid4().hex[:12]}"
        jobs = self._manager.enqueue(episodes, quality=quality, category=category, targets=targets)
        job_ids = [j.id for j in jobs]
        if len(job_ids) < len(episodes):  # the same release is already queued: reuse those jobs
            wanted = {(vid, targets[vid]) for vid in targets}
            job_ids += [j.id for j in self._manager.jobs()
                        if (j.episode.id, j.target) in wanted and j.id not in job_ids
                        and j.status in ACTIVE]
        group = ArrGroup(nzo_id=nzo_id, title=token.title, category=category, folder=str(folder),
                         job_ids=job_ids, video_ids=list(token.videos), created_at=time.time())
        with self._lock:
            self._groups[nzo_id] = group
        self._save()
        log.info("Sonarr/Radarr: %s (%s vídeos) → %s", token.title, len(episodes), folder)
        return nzo_id

    # -------------------------------------------------------------- state
    def _jobs_of(self, group: ArrGroup) -> list[Optional[DownloadJob]]:
        return [self._manager.get(job_id) for job_id in group.job_ids]

    @staticmethod
    def _summary(group: ArrGroup, jobs: list[Optional[DownloadJob]]) -> dict:
        present = [j for j in jobs if j is not None]
        active = [j for j in present if j.status in ACTIVE]
        ok = [j for j in present if j.status in OK]
        bad = [j for j in present if j.status in (JobStatus.FAILED, JobStatus.CANCELLED)]
        missing = len(jobs) - len(present)
        total = 0
        done = 0.0
        for job in present:
            size = job.total_bytes or estimate_size(job.episode.duration_seconds, 1080)
            total += size
            if job.status in OK:
                done += size
            elif job.status in ACTIVE:
                done += size * (job.progress or 0) / 100.0
        eta = max((j.eta or 0 for j in active), default=0)
        if active:
            busy = [j for j in active if j.status != JobStatus.QUEUED]
            if busy and all(j.status == JobStatus.POSTPROCESSING for j in busy):
                status = "Extracting"
            elif busy:
                status = "Downloading"
            else:
                status = "Queued"
        elif ok:
            status = "Completed"
        else:
            status = "Failed"
        error = group.error
        if not error and (bad or missing) and not active:
            first = next((j.error for j in bad if j.error), None)
            error = first or ("Descàrrega cancel·lada." if bad or missing else None)
        return {"status": status, "total": max(total, 1), "done": done, "eta": eta,
                "active": bool(active), "error": error}

    def queue(self, category: Optional[str] = None, start: int = 0, limit: int = 0) -> dict:
        slots = []
        with self._lock:
            groups = list(self._groups.values())
        for index, group in enumerate(sorted(groups, key=lambda g: g.created_at)):
            if category and category != "*" and group.category != category:
                continue
            info = self._summary(group, self._jobs_of(group))
            if not info["active"]:
                continue
            mb = info["total"] / 1048576
            mbleft = max(0.0, (info["total"] - info["done"]) / 1048576)
            slots.append({
                "status": info["status"], "index": index, "timeleft": _fmt_timeleft(info["eta"]),
                "mb": f"{mb:.2f}", "mbleft": f"{mbleft:.2f}", "filename": group.title,
                "priority": "Normal", "cat": group.category,
                "percentage": str(int(100 * info["done"] / info["total"])), "nzo_id": group.nzo_id,
            })
        if limit:
            slots = slots[start:start + limit]
        return {"queue": {"status": "Downloading" if slots else "Idle", "paused": False,
                          "noofslots": len(slots), "slots": slots}}

    def history(self, category: Optional[str] = None, start: int = 0, limit: int = 0) -> dict:
        slots = []
        changed = False
        with self._lock:
            groups = list(self._groups.values())
        for group in sorted(groups, key=lambda g: g.created_at, reverse=True):
            if category and category != "*" and group.category != category:
                continue
            info = self._summary(group, self._jobs_of(group))
            if info["active"]:
                continue
            if group.finished_at is None:
                group.finished_at = time.time()
                changed = True
            completed = info["status"] == "Completed"
            slots.append({
                "nzo_id": group.nzo_id, "name": group.title, "nzb_name": f"{group.title}.nzb",
                "category": group.category, "status": info["status"],
                "fail_message": "" if completed else (info["error"] or "La descàrrega ha fallat."),
                "storage": group.folder if completed else "",
                "bytes": _folder_size(group.folder) if completed else 0,
                "download_time": int((group.finished_at or time.time()) - group.created_at),
                "completed": int(group.finished_at or time.time()),
            })
        if changed:
            self._save()
        if limit:
            slots = slots[start:start + limit]
        return {"history": {"paused": False, "noofslots": len(slots), "slots": slots}}

    # -------------------------------------------------------------- actions
    def delete(self, nzo_ids: list[str], delete_files: bool) -> bool:
        removed = False
        root = Path(self.complete_dir())
        for nzo_id in nzo_ids:
            with self._lock:
                group = self._groups.pop(nzo_id, None)
            if group is None:
                continue
            removed = True
            for job_id in group.job_ids:
                job = self._manager.get(job_id)
                if job is not None and job.status in ACTIVE:
                    self._manager.cancel(job_id)
                self._manager.remove(job_id)
            if delete_files and _inside(Path(group.folder), root):
                shutil.rmtree(group.folder, ignore_errors=True)
        if removed:
            self._save()
        return removed

    def retry(self, nzo_id: str) -> Optional[str]:
        with self._lock:
            group = self._groups.get(nzo_id)
        if group is None:
            return None
        for job_id in group.job_ids:
            job = self._manager.get(job_id)
            if job is not None and job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
                self._manager.retry(job_id)
        group.finished_at = None
        group.error = None
        self._save()
        return nzo_id
