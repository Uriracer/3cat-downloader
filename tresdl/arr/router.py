"""HTTP endpoints for Sonarr/Radarr (see README.md next to this file).

* ``/arr/newznab/api``        Newznab indexer (``t=caps|search|tvsearch|movie``)
* ``/arr/nzb/<token>.nzb``    pseudo-NZB of one release
* ``/sabnzbd/api`` and ``/api``  SABnzbd download-client API (``mode=...``)

Every request needs the API key (``apikey`` parameter or ``X-Api-Key``
header), in desktop and server mode alike. Errors use each protocol's format.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from . import releases as rl
from .indexer import Indexer
from .sabnzbd import SAB_VERSION, SabError, SabnzbdService

log = logging.getLogger(__name__)

MAX_NZB_BYTES = 1_000_000


def _key_ok(request: Request, expected: str) -> bool:
    given = request.query_params.get("apikey") or request.headers.get("x-api-key") or ""
    return bool(expected) and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _xml(text: str, status: int = 200) -> Response:
    return Response(text, status_code=status, media_type="application/xml; charset=utf-8")


def _int(value: Optional[str], default: int = 0) -> int:
    try:
        return max(0, int(value or default))
    except ValueError:
        return default


def create_arr_router(client: Any, manager: Any, settings_getter: Callable[[], Any],
                      *, data_dir: Any = None) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    indexer = Indexer(client)
    sab = SabnzbdService(client, manager, settings_getter, data_dir=data_dir)
    router.sab = sab  # type: ignore[attr-defined]  # for tests

    def api_key() -> str:
        return str(getattr(settings_getter(), "api_key", "") or "")

    # ------------------------------------------------------------ newznab
    @router.get("/arr/newznab/api")
    @router.get("/arr/newznab/api/")
    async def newznab(request: Request) -> Response:
        params = request.query_params
        if not _key_ok(request, api_key()):
            return _xml(rl.error_xml(100, "Incorrect user credentials"))
        t = (params.get("t") or "").lower()
        if t == "caps":
            return _xml(rl.caps_xml())
        cats = {int(c) for c in (params.get("cat") or "").replace(" ", "").split(",") if c.isdigit()}
        wants_movies = (t == "movie") or (bool(cats) and all(2000 <= c < 3000 for c in cats))
        q = (params.get("q") or "").strip()
        offset, limit = _int(params.get("offset")), _int(params.get("limit"), 100) or 100
        try:
            if t not in ("search", "tvsearch", "movie"):
                return _xml(rl.error_xml(202, f"No such function ({t})"))
            if not q:
                found = await run_in_threadpool(indexer.rss, wants_movies)
            elif wants_movies:
                found = await run_in_threadpool(indexer.movie_search, q)
            else:
                found = await run_in_threadpool(indexer.tv_search, q, params.get("season"), params.get("ep"))
        except Exception as exc:
            log.warning("Newznab search failed (%s): %s", q, exc)
            return _xml(rl.error_xml(900, f"Error de 3cat: {exc}"))
        page = found[offset:offset + limit]
        return _xml(rl.rss_feed(page, base_url=str(request.base_url), api_key=api_key(),
                                offset=offset, total=len(found)))

    @router.get("/arr/nzb/{token}.nzb")
    async def nzb(token: str, request: Request) -> Response:
        if not _key_ok(request, api_key()):
            return _xml(rl.error_xml(100, "Incorrect user credentials"), status=401)
        try:
            tok = rl.Token.decode(token)
        except ValueError as exc:
            return _xml(rl.error_xml(300, str(exc)), status=404)
        filename = f"{tok.title}.nzb".replace('"', "")
        return Response(rl.build_nzb(tok), media_type="application/x-nzb",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # ------------------------------------------------------------ sabnzbd
    async def sabnzbd(request: Request) -> Response:
        params = request.query_params
        mode = (params.get("mode") or "").lower()
        if mode == "version":   # SABnzbd answers this without a key too
            return JSONResponse({"version": SAB_VERSION})
        if not (params.get("apikey") or request.headers.get("x-api-key")):
            return JSONResponse({"status": False, "error": "API Key Required"})
        if not _key_ok(request, api_key()):
            return JSONResponse({"status": False, "error": "API Key Incorrect"})
        try:
            return JSONResponse(await _sab_mode(mode, request))
        except SabError as exc:
            return JSONResponse({"status": False, "error": str(exc)})

    async def _sab_mode(mode: str, request: Request) -> dict:
        params = request.query_params
        category = params.get("category") or None
        start, limit = _int(params.get("start")), _int(params.get("limit"))
        name = (params.get("name") or "").lower()
        values = [v for v in (params.get("value") or "").split(",") if v]
        delete_files = params.get("del_files") in ("1", "true", "True")
        if mode == "get_config":
            return sab.config()
        if mode == "fullstatus":
            return {"status": {"completedir": sab.complete_dir(), "paused": False}}
        if mode == "queue":
            if name == "delete":
                return {"status": await run_in_threadpool(sab.delete, values, delete_files), "nzo_ids": values}
            if name in ("pause", "resume", "priority", "change_cat"):
                return {"status": True}
            return await run_in_threadpool(sab.queue, category, start, limit)
        if mode == "history":
            if name == "delete":
                return {"status": await run_in_threadpool(sab.delete, values, delete_files)}
            return await run_in_threadpool(sab.history, category, start, limit)
        if mode in ("addfile", "addlocalfile", "addurl"):
            data = await _nzb_bytes(mode, request)
            try:
                token = rl.token_from_nzb(data)
            except ValueError as exc:
                raise SabError(str(exc)) from exc
            nzo_id = await run_in_threadpool(sab.add, token, params.get("cat"))
            return {"status": True, "nzo_ids": [nzo_id]}
        if mode == "retry":
            nzo_id = await run_in_threadpool(sab.retry, params.get("value") or "")
            return {"status": nzo_id is not None, "nzo_id": nzo_id}
        if mode in ("pause", "resume", "warnings", "server_stats", "status", "shutdown", "restart"):
            return {"status": True, "warnings": []} if mode == "warnings" else {"status": True}
        raise SabError(f"Mode no suportat: {mode or '(cap)'}")

    async def _nzb_bytes(mode: str, request: Request) -> bytes:
        if mode == "addurl":
            url = request.query_params.get("name") or ""
            marker = "/arr/nzb/"
            if marker not in url:
                raise SabError("Només s'accepten enllaços de 3Cat Downloader.")
            token = url.split(marker, 1)[1].split(".nzb", 1)[0]
            try:
                return rl.build_nzb(rl.Token.decode(token))
            except ValueError as exc:
                raise SabError(str(exc)) from exc
        form = await request.form()
        upload = form.get("name") or form.get("nzbfile")
        if upload is None or isinstance(upload, str):
            raise SabError("No s'ha rebut cap fitxer NZB.")
        data = await upload.read(MAX_NZB_BYTES + 1)
        if len(data) > MAX_NZB_BYTES:
            raise SabError("El fitxer NZB és massa gran.")
        return data

    for path in ("/sabnzbd/api", "/api"):
        router.add_api_route(path, sabnzbd, methods=["GET", "POST"])
    return router
