"""Endpoint skeletons for the future Sonarr/Radarr integration.

NOT mounted in the application yet: every endpoint answers HTTP 501. The
signatures document the query parameters Sonarr/Radarr send, so the real
implementation only has to fill in the bodies. See ``README.md``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter(tags=["arr (no implementat)"])

NOT_IMPLEMENTED_DETAIL = "La integració amb Sonarr/Radarr encara no està implementada."


def _not_implemented() -> JSONResponse:
    return JSONResponse({"detail": NOT_IMPLEMENTED_DETAIL}, status_code=501)


# --------------------------------------------------------------------------- #
# Newznab-compatible indexer
# --------------------------------------------------------------------------- #
@router.get("/arr/newznab/api")
async def newznab_api(
    t: str = Query(..., description="caps | search | tvsearch | movie"),
    apikey: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="free-text query"),
    season: Optional[str] = Query(None, description="season number (tvsearch)"),
    ep: Optional[str] = Query(None, description="episode number (tvsearch)"),
    tvdbid: Optional[str] = Query(None),
    imdbid: Optional[str] = Query(None),
    cat: Optional[str] = Query(None, description="comma-separated Newznab category ids"),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    o: Optional[str] = Query(None, description="output format: xml (default) | json"),
) -> JSONResponse:
    """Newznab ``api`` endpoint: ``t=caps`` returns the capabilities XML,
    ``t=tvsearch``/``t=movie``/``t=search`` return an RSS feed of releases whose
    enclosure points to ``/arr/nzb/<token>.nzb``."""
    return _not_implemented()


@router.get("/arr/nzb/{token}.nzb")
async def newznab_nzb(token: str, apikey: Optional[str] = Query(None)) -> JSONResponse:
    """Serve the pseudo-NZB for a release token (``v<video-id>`` or
    ``s<program-id>-<season-id>``). The NZB only carries metadata that our own
    SABnzbd-compatible API understands; it never references Usenet."""
    return _not_implemented()


# --------------------------------------------------------------------------- #
# SABnzbd-compatible download client
# --------------------------------------------------------------------------- #
@router.get("/sabnzbd/api")
async def sabnzbd_api_get(
    mode: str = Query(..., description="version | get_config | addurl | queue | history | ..."),
    apikey: Optional[str] = Query(None),
    output: Optional[str] = Query("json"),
    name: Optional[str] = Query(None, description="URL for addurl, or action/name for queue/history"),
    value: Optional[str] = Query(None, description="nzo_id for delete"),
    cat: Optional[str] = Query(None, description="tv | movies"),
    priority: Optional[int] = Query(None),
    nzbname: Optional[str] = Query(None),
    start: int = Query(0, ge=0),
    limit: int = Query(0, ge=0),
) -> JSONResponse:
    """SABnzbd ``api`` (GET form, used for most modes)."""
    return _not_implemented()


@router.post("/sabnzbd/api")
async def sabnzbd_api_post(
    mode: str = Query(..., description="addfile (multipart upload of the .nzb)"),
    apikey: Optional[str] = Query(None),
    output: Optional[str] = Query("json"),
    cat: Optional[str] = Query(None),
    priority: Optional[int] = Query(None),
    nzbname: Optional[str] = Query(None),
) -> JSONResponse:
    """SABnzbd ``api`` (POST form, ``mode=addfile`` with a multipart ``name`` file)."""
    return _not_implemented()
