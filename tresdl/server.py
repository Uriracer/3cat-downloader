"""Local HTTP server for 3Cat Downloader.

The FastAPI application built by :func:`create_app` is the core of the program:
the desktop window (pywebview) is only a browser pointed at it, and in the
future Sonarr/Radarr will talk to the same server (see ``tresdl/arr``).

Design notes
------------
* Every call into :class:`~tresdl.api.ThreeCatClient` or
  :class:`~tresdl.downloader.DownloadManager` is blocking, so it runs in the
  threadpool (``run_in_threadpool``) to keep the event loop responsive.
* ``ThreeCatError`` becomes HTTP 502 ``{"detail": ...}`` (404 when the error
  carries ``status_code == 404`` or ``not_found = True``).
* ``/api/events`` is a Server-Sent Events stream. The manager has a single
  subscription (:class:`EventBroadcaster`) that fans every job change out to
  one ``asyncio.Queue`` per connected client via ``loop.call_soon_threadsafe``.
* The server must only be bound to 127.0.0.1. As defence in depth against DNS
  rebinding / cross-site requests, unknown ``Host`` headers and foreign
  ``Origin`` headers on state-changing requests are rejected with 403.
* Sibling modules (api, config, downloader) are imported lazily, only when
  :func:`create_app` has to build a default instance, so importing this module
  never fails because of them.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import string
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Literal, Optional, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .models import DownloadJob, Episode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Settings

try:  # tresdl/__init__.py is the single source of truth for the version.
    from . import __version__ as APP_VERSION  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - package without __init__ yet
    APP_VERSION = "0.1.0"

log = logging.getLogger(__name__)

#: Identifier returned by ``/api/health`` so ``run.py`` can recognise an
#: already running instance of this very application on a port.
APP_ID = "3cat-downloader"

#: Seconds between SSE keepalive comments.
KEEPALIVE_SECONDS = 15.0

#: Max buffered events per SSE client before the oldest ones are dropped.
EVENT_QUEUE_SIZE = 1000

QUALITIES: tuple[str, ...] = ("best", "1080", "720", "576", "480", "mp4")
CATEGORIES: tuple[str, ...] = ("tv", "movies")

#: Host names accepted in the ``Host`` header (the server binds 127.0.0.1).
#: Only loopback names: a single-label name such as "testserver" could be
#: answered by anyone on the LAN (LLMNR/NetBIOS) and used for DNS rebinding.
DEFAULT_ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: Highest accepted ``max_parallel`` (same limit as ``tresdl.config``).
MAX_PARALLEL_LIMIT = 8

#: Max length of the text accepted by ``POST /api/resolve``.
RESOLVE_MAX_LENGTH = 2000

QualityName = Literal["best", "1080", "720", "576", "480", "mp4"]
CategoryName = Literal["tv", "movies"]


# --------------------------------------------------------------------------- #
# Collaborator protocols (structural; real classes live in sibling modules)
# --------------------------------------------------------------------------- #
class ClientLike(Protocol):
    """Subset of ``tresdl.api.ThreeCatClient`` used by the server."""

    def search(self, query: str, limit: int = 30) -> list[Any]: ...
    def get_program(self, program_id: int) -> Any: ...
    def list_episodes(self, program_id: int, season_id: str | None = None,
                      full_episodes_only: bool = True) -> list[Episode]: ...
    def list_seasons(self, episodes: list[Episode]) -> list[Any]: ...
    def get_media(self, video_id: int) -> Any: ...
    def get_episode(self, video_id: int) -> Episode: ...
    def resolve(self, text: str) -> dict: ...


class ManagerLike(Protocol):
    """Subset of ``tresdl.downloader.DownloadManager`` used by the server."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def enqueue(self, episodes: list[Episode], quality: str | None = None,
                category: str = "tv") -> list[DownloadJob]: ...
    def jobs(self) -> list[DownloadJob]: ...
    def get(self, job_id: str) -> DownloadJob | None: ...
    def cancel(self, job_id: str) -> bool: ...
    def retry(self, job_id: str) -> bool: ...
    def remove(self, job_id: str) -> bool: ...
    def clear_finished(self) -> int: ...
    def subscribe(self, cb: Callable[[DownloadJob], None]) -> None: ...


class SettingsStoreLike(Protocol):
    """Holder of the current settings (see :class:`SettingsStore`)."""

    def get(self) -> Any: ...
    def set(self, settings: Any) -> Any: ...


# --------------------------------------------------------------------------- #
# Settings store
# --------------------------------------------------------------------------- #
class SettingsStore:
    """Thread-safe holder of the current :class:`~tresdl.config.Settings`.

    ``get()`` returns the current instance, which callers must treat as
    read-only; ``set()`` persists a new instance and swaps it in atomically.
    ``get`` is meant to be passed as the ``settings_getter`` of the
    ``DownloadManager`` so it always sees the latest settings.
    """

    def __init__(self, loader: Callable[[], Any] | None = None,
                 saver: Callable[[Any], None] | None = None) -> None:
        if loader is None or saver is None:
            from . import config  # lazy: sibling module

            loader = loader or config.load_settings
            saver = saver or config.save_settings
        self._saver = saver
        self._lock = threading.Lock()
        self._settings = loader()

    def get(self) -> "Settings":
        with self._lock:
            return self._settings

    def set(self, settings: "Settings") -> "Settings":
        with self._lock:
            self._saver(settings)
            self._settings = settings
            return settings


# --------------------------------------------------------------------------- #
# Server-Sent Events fan-out
# --------------------------------------------------------------------------- #
_CLOSE = object()  # sentinel pushed to subscriber queues to end their stream


class _Subscriber:
    """One SSE connection: an asyncio queue bound to the loop serving it."""

    def __init__(self, loop: asyncio.AbstractEventLoop, maxsize: int) -> None:
        self.loop = loop
        self.queue: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
        self.alive = True

    def push(self, item: object) -> None:
        """Thread-safe: schedule ``item`` onto the subscriber's loop."""
        if not self.alive:
            return
        try:
            self.loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:  # loop already closed
            self.alive = False

    def _put(self, item: object) -> None:  # runs on self.loop
        if self.queue.full():  # slow consumer: drop the oldest event
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race-free on one loop
                pass
        self.queue.put_nowait(item)


class EventBroadcaster:
    """Fans job updates (from any thread) out to every SSE connection.

    The manager contract has no ``unsubscribe``, so the broadcaster subscribes
    exactly once and connections come and go via :meth:`open`/:meth:`discard`.
    """

    def __init__(self, maxsize: int = EVENT_QUEUE_SIZE) -> None:
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._subs: set[_Subscriber] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, job: DownloadJob) -> None:
        """Manager callback: serialise ``job`` now and deliver it everywhere."""
        try:
            payload = json.dumps(job.to_dict(), ensure_ascii=False, separators=(",", ":"))
        except Exception:  # never let a bad job break the worker thread
            log.exception("Could not serialise job update")
            return
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.push(payload)

    def open(self, loop: asyncio.AbstractEventLoop) -> _Subscriber:
        sub = _Subscriber(loop, self._maxsize)
        with self._lock:
            if self._closed:
                sub.push(_CLOSE)
            self._subs.add(sub)
        return sub

    def discard(self, sub: _Subscriber) -> None:
        sub.alive = False
        with self._lock:
            self._subs.discard(sub)

    def close(self) -> None:
        """End every open stream (call before stopping the HTTP server)."""
        with self._lock:
            self._closed = True
            subs = list(self._subs)
        for sub in subs:
            sub.push(_CLOSE)


def _sse(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class ResolveRequest(BaseModel):
    text: str = Field(min_length=1, max_length=RESOLVE_MAX_LENGTH)


class DownloadRequest(BaseModel):
    """Body of ``POST /api/downloads``.

    * ``episode_ids`` -> exactly those videos (``program_id`` helps building
      the episodes from one listing instead of one request per video); an
      empty list is an empty selection (HTTP 400), never "the whole series";
    * ``program_id`` alone -> the whole series;
    * ``program_id`` + ``season_ids`` -> only those seasons.
    """

    episode_ids: Optional[list[int]] = None
    program_id: Optional[int] = None
    season_ids: Optional[list[str]] = None
    quality: Optional[QualityName] = None
    category: CategoryName = "tv"


class OpenFolderRequest(BaseModel):
    """``job_id`` opens that job's own output (no root check: the server made the
    path); ``path`` must be inside the download folder; neither = the folder."""
    model_config = ConfigDict(extra="ignore")
    path: Optional[str] = None
    job_id: Optional[str] = None


class NamingPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    filename_template: str = Field(default="", max_length=500)
    download_dir: Optional[str] = Field(default=None, max_length=1000)


#: Sample episode for ``POST /api/naming/preview`` (Merlí S02E05).
PREVIEW_EPISODE = Episode(
    id=5619450, program_id=54076, program_title="Merlí", title="Els peripatètics",
    raw_title="T2xC5 - Els peripatètics", season_id="PUTEMP_2", season_number=2,
    episode_number=5, absolute_number=18, air_date="2016-10-17",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def resolve_web_dir() -> Path:
    """Location of the static UI (``web/``), also when frozen by PyInstaller."""
    override = os.environ.get("TRESDL_WEB_DIR")
    if override:
        return Path(override)
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        return Path(meipass) / "web"
    return Path(__file__).resolve().parent.parent / "web"


def jsonable(value: Any) -> Any:
    """Recursively convert models (``to_dict``), dataclasses and enums to JSON data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return value


def _is_threecat_error(exc: BaseException) -> bool:
    # Matched by name so the real class need not be importable (and fakes work).
    return any(cls.__name__ == "ThreeCatError" for cls in type(exc).__mro__)


async def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call in the threadpool, mapping ThreeCatError to HTTP errors."""
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:
        if not _is_threecat_error(exc):
            raise
        # api.ThreeCatError carries ``status``; ``status_code``/``not_found`` are
        # accepted too so test fakes and future error types keep working.
        not_found = (getattr(exc, "status", None) == 404
                     or getattr(exc, "status_code", None) == 404
                     or bool(getattr(exc, "not_found", False))
                     or any(cls.__name__ == "ThreeCatNotFound" for cls in type(exc).__mro__))
        detail = str(exc) or "Error en comunicar amb 3cat."
        raise HTTPException(status_code=404 if not_found else 502, detail=detail) from exc


_ffmpeg_cache: dict[str, Optional[str]] = {}


def _ffmpeg_path() -> Optional[str]:
    """Path to the ffmpeg binary that downloads will use, or None if missing."""
    if "path" not in _ffmpeg_cache:
        path: Optional[str]
        try:
            import imageio_ffmpeg

            path = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            path = shutil.which("ffmpeg")
        _ffmpeg_cache["path"] = path
    return _ffmpeg_cache["path"]


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def is_within(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or inside it (symlinks resolved, case-insensitive on Windows)."""
    try:
        root_n = _norm(root)
        return os.path.commonpath([root_n, _norm(path)]) == root_n
    except ValueError:  # different drives
        return False


def open_in_file_manager(path: Path) -> None:
    """Open a folder (or reveal a file) in the OS file manager."""
    if sys.platform == "win32":
        if path.is_file():
            subprocess.Popen(f'explorer /select,"{path}"')  # noqa: S603 - fixed program
        else:
            os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent if path.is_file() else path)])


def _expand_user(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _field_name(loc: Any) -> str:
    parts = [str(p) for p in (loc or ()) if p not in ("body", "query", "path")]
    return ".".join(parts) or "?"


def catalan_validation_message(err: dict[str, Any]) -> str:
    """Catalan text for one pydantic/FastAPI validation error (never echoes input)."""
    kind = str(err.get("type", ""))
    ctx = err.get("ctx") or {}
    field = _field_name(err.get("loc"))
    if kind == "string_too_long":
        return f"El text és massa llarg (màxim {ctx.get('max_length', '?')} caràcters)."
    if kind == "string_too_short":
        return f"Falta un valor obligatori ('{field}')."
    if kind == "missing":
        return f"Falta un valor obligatori ('{field}')."
    if kind in ("json_invalid", "model_attributes_type", "dict_type", "model_type"):
        return "La petició no és vàlida."
    if kind == "literal_error":
        return f"Valor no vàlid per a '{field}'. Opcions: {ctx.get('expected', '')}."
    return f"Valor no vàlid per a '{field}'."


def validate_settings(current: Any, changes: dict[str, Any]) -> Any:
    """Merge ``changes`` into ``current`` and return a new, validated Settings.

    Unknown keys are ignored. Raises :class:`ValueError` with a Catalan message.
    """
    if not dataclasses.is_dataclass(current):
        raise TypeError("settings must be a dataclass instance")
    known = {f.name for f in dataclasses.fields(current)}
    merged = dataclasses.asdict(current)
    merged.update({k: v for k, v in changes.items() if k in known})
    try:
        new = TypeAdapter(type(current)).validate_python(merged)
    except ValidationError as exc:
        raise ValueError(catalan_validation_message(exc.errors()[0])) from exc

    def check(name: str, ok: bool, message: str) -> None:
        if name in known and not ok:
            raise ValueError(message)

    if "quality" in known:  # accept "720p" like config.py does
        new = dataclasses.replace(new, quality=str(new.quality).strip().lower().removesuffix("p"))
    values = dataclasses.asdict(new)
    if "download_dir" in known:
        raw = str(values["download_dir"]).strip()
        check("download_dir", bool(raw), "Cal indicar una carpeta de descàrregues.")
        new_dir = _expand_user(raw)
        check("download_dir", new_dir.is_absolute(), "La carpeta de descàrregues ha de ser una ruta absoluta.")
        # A missing drive is accepted (a removable disk may be offline), but a
        # path that goes through an existing *file* can never work.
        probe = new_dir
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        check("download_dir", not probe.exists() or probe.is_dir(),
              f"La carpeta de descàrregues no és vàlida: «{probe}» és un fitxer.")
        new = dataclasses.replace(new, download_dir=str(new_dir))
    check("max_parallel", 1 <= int(values.get("max_parallel", 1)) <= MAX_PARALLEL_LIMIT,
          f"Les descàrregues simultànies han d'estar entre 1 i {MAX_PARALLEL_LIMIT}.")
    check("quality", values.get("quality") in QUALITIES,
          f"Qualitat no vàlida. Opcions: {', '.join(QUALITIES)}.")
    check("server_port", 1024 <= int(values.get("server_port", 1024)) <= 65535,
          "El port ha d'estar entre 1024 i 65535.")
    if "filename_template" in known:
        template = str(values["filename_template"])
        check("filename_template", bool(template.strip()), "La plantilla de noms no pot estar buida.")
        try:
            from .naming import validate_template  # lazy: sibling module
        except ImportError:
            validate_template = None
        try:
            if validate_template is not None:
                validate_template(template)
            else:
                list(string.Formatter().parse(template))
        except ValueError as exc:
            message = str(exc)
            raise ValueError(message if validate_template is not None
                             else f"La plantilla de noms no és vàlida: {message}") from exc
    if "api_key" in known and not str(values.get("api_key") or "").strip():
        new = dataclasses.replace(new, api_key=getattr(current, "api_key"))
    return new


def _hostname(netloc: str) -> str:
    try:
        return (urlsplit("//" + netloc).hostname or "").lower()
    except ValueError:
        return ""


class LocalGuardMiddleware:
    """Pure ASGI middleware: host/origin checks and Cache-Control headers.

    Implemented without ``BaseHTTPMiddleware`` so streaming (SSE) responses
    are not buffered or wrapped.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: frozenset[str]) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if _hostname(headers.get("host", "")) not in self.allowed_hosts:
            await JSONResponse({"detail": "Host no permès."}, status_code=403)(scope, receive, send)
            return
        if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            origin = headers.get("origin")
            if origin is not None and _hostname(urlsplit(origin).netloc) not in self.allowed_hosts:
                await JSONResponse({"detail": "Origen no permès."}, status_code=403)(scope, receive, send)
                return

        path: str = scope.get("path", "")
        if path.startswith("/api/") or path == "/" or path.endswith((".html", "/")):
            cache_control = "no-store"
        else:
            cache_control = "no-cache"

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                if "cache-control" not in response_headers:
                    response_headers["Cache-Control"] = cache_control
            await send(message)

        await self.app(scope, receive, send_wrapper)


_MISSING_UI_HTML = """<!doctype html><html lang="ca"><meta charset="utf-8">
<title>3Cat Downloader</title><body style="font-family:sans-serif;padding:2rem">
<h1>3Cat Downloader</h1><p>No s'ha trobat la interfície (carpeta <code>web/</code>).
L'API continua disponible a <code>/api</code>.</p></body></html>"""


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #
def create_app(client: Optional[ClientLike] = None,
               manager: Optional[ManagerLike] = None,
               settings_store: Optional[SettingsStoreLike] = None,
               *,
               web_dir: Optional[Path] = None,
               allowed_hosts: Optional[frozenset[str]] = None) -> FastAPI:
    """Build the FastAPI app.

    Missing collaborators are created with their defaults (lazy imports). A
    manager created here is started/stopped by the app lifespan; a manager
    passed in is owned (started/stopped) by the caller.

    ``app.state.events`` is the :class:`EventBroadcaster`; call its ``close()``
    before shutting the HTTP server down so open SSE streams end promptly.
    """
    if settings_store is None:
        settings_store = SettingsStore()
    if client is None:
        from .api import ThreeCatClient  # lazy: sibling module

        client = ThreeCatClient()
    owns_manager = manager is None
    if manager is None:
        from .downloader import DownloadManager  # lazy: sibling module

        manager = DownloadManager(client, settings_store.get)  # type: ignore[arg-type]

    store: SettingsStoreLike = settings_store
    cl: ClientLike = client
    mgr: ManagerLike = manager

    events = EventBroadcaster()
    mgr.subscribe(events.publish)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if owns_manager:
            await run_in_threadpool(mgr.start)
        try:
            yield
        finally:
            events.close()
            if owns_manager:
                await run_in_threadpool(mgr.stop)

    app = FastAPI(title="3Cat Downloader", version=APP_VERSION, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(LocalGuardMiddleware,
                       allowed_hosts=DEFAULT_ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Any, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        message = catalan_validation_message(errors[0]) if errors else "La petició no és vàlida."
        return JSONResponse({"detail": message}, status_code=422)
    app.state.client = cl
    app.state.manager = mgr
    app.state.settings_store = store
    app.state.events = events

    def require_job(job_id: str) -> None:
        if mgr.get(job_id) is None:
            raise HTTPException(status_code=404, detail="No existeix aquesta descàrrega.")

    # ----------------------------------------------------------------- health
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        ffmpeg = await run_in_threadpool(_ffmpeg_path)
        return {"ok": True, "version": APP_VERSION, "ffmpeg": bool(ffmpeg),
                "ffmpeg_path": ffmpeg, "app": APP_ID}

    # ---------------------------------------------------------- catalogue
    @app.get("/api/search")
    async def search(q: str = Query(default="", max_length=200)) -> dict[str, Any]:
        query = q.strip()
        if not query:
            return {"results": []}
        results = await _call(cl.search, query)
        return {"results": jsonable(results)}

    @app.post("/api/resolve")
    async def resolve(body: ResolveRequest) -> Any:
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=422, detail="Cal escriure un enllaç o un text a cercar.")
        return jsonable(await _call(cl.resolve, text))

    @app.get("/api/programs/{program_id}")
    async def program_detail(program_id: int) -> dict[str, Any]:
        program, episodes = await asyncio.gather(
            _call(cl.get_program, program_id),
            _call(cl.list_episodes, program_id),
        )
        if program is None:
            raise HTTPException(status_code=404, detail="No s'ha trobat el programa.")
        seasons = await _call(cl.list_seasons, episodes)
        return {"program": jsonable(program), "seasons": jsonable(seasons), "episodes": jsonable(episodes)}

    @app.get("/api/videos/{video_id}/media")
    async def video_media(video_id: int) -> Any:
        media = await _call(cl.get_media, video_id)
        if media is None:
            raise HTTPException(status_code=404, detail="No s'ha trobat el vídeo.")
        return jsonable(media)

    # ---------------------------------------------------------- downloads
    def expand_episodes(req: DownloadRequest) -> list[Episode]:
        """Turn a download request into concrete episodes (blocking)."""
        if req.episode_ids is not None:
            if not req.episode_ids:
                raise HTTPException(status_code=400, detail="No s'ha seleccionat cap capítol.")
            wanted = list(dict.fromkeys(req.episode_ids))
            listed: dict[int, Episode] = {}
            if req.program_id is not None:
                listed = {ep.id: ep for ep in cl.list_episodes(req.program_id)}
            return [listed.get(vid) or cl.get_episode(vid) for vid in wanted]
        if req.program_id is not None:
            episodes = cl.list_episodes(req.program_id)
            if req.season_ids is not None:
                if not req.season_ids:
                    raise HTTPException(status_code=400, detail="No s'ha seleccionat cap temporada.")
                seasons = set(req.season_ids)
                episodes = [ep for ep in episodes if ep.season_id in seasons]
            return episodes
        raise HTTPException(status_code=400,
                            detail="Cal indicar els episodis o el programa que vols descarregar.")

    @app.post("/api/downloads")
    async def create_downloads(body: DownloadRequest) -> dict[str, Any]:
        episodes = await _call(expand_episodes, body)
        if not episodes:
            raise HTTPException(status_code=404, detail="No s'ha trobat cap episodi per descarregar.")
        jobs = await run_in_threadpool(mgr.enqueue, episodes, quality=body.quality, category=body.category)
        return {"jobs": jsonable(jobs), "requested": len(episodes)}

    @app.get("/api/downloads")
    async def list_downloads() -> dict[str, Any]:
        return {"jobs": jsonable(await run_in_threadpool(mgr.jobs))}

    @app.post("/api/downloads/clear")
    async def clear_downloads() -> dict[str, Any]:
        return {"removed": await run_in_threadpool(mgr.clear_finished)}

    @app.post("/api/downloads/{job_id}/cancel")
    async def cancel_download(job_id: str) -> dict[str, Any]:
        require_job(job_id)
        return {"ok": bool(await run_in_threadpool(mgr.cancel, job_id))}

    @app.post("/api/downloads/{job_id}/retry")
    async def retry_download(job_id: str) -> dict[str, Any]:
        require_job(job_id)
        return {"ok": bool(await run_in_threadpool(mgr.retry, job_id))}

    @app.delete("/api/downloads/{job_id}")
    async def delete_download(job_id: str) -> dict[str, Any]:
        require_job(job_id)
        return {"ok": bool(await run_in_threadpool(mgr.remove, job_id))}

    # ----------------------------------------------------------- settings
    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return dataclasses.asdict(store.get())

    @app.put("/api/settings")
    async def put_settings(body: dict[str, Any]) -> dict[str, Any]:
        try:
            new = validate_settings(store.get(), body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        saved = await run_in_threadpool(store.set, new)
        refresh = getattr(mgr, "refresh_workers", None)
        if callable(refresh):  # apply a new max_parallel to queued work at once
            await run_in_threadpool(refresh)
        return dataclasses.asdict(saved)

    @app.post("/api/naming/preview")
    async def naming_preview(body: NamingPreviewRequest) -> dict[str, Any]:
        """Render a sample path exactly as the downloader would (never 4xx for a bad template)."""
        from . import naming  # lazy: sibling module

        template = body.filename_template
        root = (body.download_dir or "").strip() or str(store.get().download_dir)
        try:
            naming.validate_template(template)
            path = naming.episode_path(str(_expand_user(root)), PREVIEW_EPISODE, "mp4", template)
        except ValueError as exc:
            return {"ok": False, "error": str(exc) or "La plantilla no és vàlida."}
        return {"ok": True, "path": path}

    @app.post("/api/open-folder")
    async def open_folder(body: Optional[OpenFolderRequest] = None) -> dict[str, Any]:
        root = _expand_user(store.get().download_dir)
        if body is not None and body.job_id:
            job = await run_in_threadpool(mgr.get, body.job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="No existeix aquesta descàrrega.")
            if not job.output_path:
                raise HTTPException(status_code=404, detail="La carpeta o el fitxer no existeix.")
            # The server produced this path itself, so it is safe to open even
            # if the download folder setting changed since.
            target = Path(job.output_path)
            if not target.exists():
                target = target.parent
            if not target.exists():
                raise HTTPException(status_code=404, detail="La carpeta o el fitxer no existeix.")
            try:
                await run_in_threadpool(open_in_file_manager, target)
            except OSError as exc:
                raise HTTPException(status_code=500,
                                    detail=f"No s'ha pogut obrir la carpeta: {exc}") from exc
            return {"ok": True}
        if body is not None and body.path:
            target = _expand_user(body.path)
            if not target.is_absolute():
                target = root / target
        else:
            target = root
        if not is_within(target, root):
            raise HTTPException(status_code=400,
                                detail="Només es poden obrir carpetes dins de la carpeta de descàrregues.")
        if _norm(target) == _norm(root):
            await run_in_threadpool(lambda: root.mkdir(parents=True, exist_ok=True))
        elif not target.exists():
            raise HTTPException(status_code=404, detail="La carpeta o el fitxer no existeix.")
        try:
            await run_in_threadpool(open_in_file_manager, target)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"No s'ha pogut obrir la carpeta: {exc}") from exc
        return {"ok": True}

    # ------------------------------------------------------------- events
    @app.get("/api/events")
    async def job_events() -> StreamingResponse:
        sub = events.open(asyncio.get_running_loop())
        keepalive = KEEPALIVE_SECONDS

        async def stream() -> AsyncIterator[str]:
            try:
                yield "retry: 3000\n\n"
                # Snapshot first so (re)connecting clients are in sync; events
                # that race with it are harmless upserts on the client side.
                for job in await run_in_threadpool(mgr.jobs):
                    yield _sse("job", json.dumps(job.to_dict(), ensure_ascii=False, separators=(",", ":")))
                while True:
                    try:
                        item = await asyncio.wait_for(sub.queue.get(), timeout=keepalive)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is _CLOSE:
                        break
                    yield _sse("job", str(item))
            finally:  # client disconnected, server closing or stream cancelled
                events.discard(sub)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    # ------------------------------------------------------- static UI (LAST)
    ui_dir = Path(web_dir) if web_dir is not None else resolve_web_dir()
    if not ui_dir.is_dir():
        log.warning("Web UI directory not found: %s", ui_dir)

        @app.get("/", include_in_schema=False)
        async def missing_ui() -> HTMLResponse:
            return HTMLResponse(_MISSING_UI_HTML)
    else:
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="web")

    return app
