"""TheTVDB / TVmaze ids -> the names a series has on 3cat.

Sonarr names series after TheTVDB's English title ("Genesis (2026)"), while
3cat uses the Catalan one ("Gènesi"). When Sonarr searches by id
(``tvdbid`` / ``tvmazeid``), TVmaze gives the original name and the official
site (often the 3cat page, whose slug is the programme's), and Sonarr's own
metadata service (SkyHook) gives the title Sonarr uses, to name the releases.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from .. import __version__

log = logging.getLogger(__name__)

TVMAZE = "https://api.tvmaze.com"
SKYHOOK = "https://skyhook.sonarr.tv/v1/tvdb/shows/en"
CACHE_TTL = 24 * 3600.0
FAILURE_TTL = 15 * 60.0
_SLUG_RE = re.compile(r"(?:3cat\.cat/3cat|ccma\.cat/(?:tv3|3cat)(?:/alacarta)?)/([a-z0-9-]+)", re.I)

Fetch = Callable[[str, dict], Optional[Any]]   # (url, params) -> parsed JSON, None when not found


@dataclass
class SeriesNames:
    """What we know about one TheTVDB series."""
    tvdb_id: Optional[int] = None
    tvmaze_id: Optional[int] = None
    sonarr_title: str = ""                       # "Genesis (2026)"
    names: list[str] = field(default_factory=list)   # names to look for on 3cat: "Gènesi", "genesi"…


def _default_fetch(url: str, params: dict) -> Optional[Any]:
    headers = {"User-Agent": f"3cat-downloader/{__version__}"}
    with httpx.Client(timeout=10.0, follow_redirects=True, headers=headers) as http:
        resp = http.get(url, params=params)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


class SeriesLookup:
    def __init__(self, fetch: Optional[Fetch] = None) -> None:
        self._fetch = fetch or _default_fetch
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, int], tuple[float, Optional[SeriesNames]]] = {}

    def find(self, tvdb_id: Optional[int] = None, tvmaze_id: Optional[int] = None) -> Optional[SeriesNames]:
        """Names of a series by TheTVDB or TVmaze id (cached); None if unknown or offline."""
        key = ("tvdb", tvdb_id) if tvdb_id else ("tvmaze", tvmaze_id) if tvmaze_id else None
        if key is None:
            return None
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
        if cached and now < cached[0]:
            return cached[1]
        try:
            found = self._lookup(tvdb_id, tvmaze_id)
            expires = now + CACHE_TTL
        except Exception as exc:   # TVmaze down: try again later, the title search still works
            log.warning("No s'ha pogut consultar TVmaze (%s=%s): %s", key[0], key[1], exc)
            found, expires = None, now + FAILURE_TTL
        with self._lock:
            self._cache[key] = (expires, found)
        return found

    def _lookup(self, tvdb_id: Optional[int], tvmaze_id: Optional[int]) -> Optional[SeriesNames]:
        if tvmaze_id:
            show = self._fetch(f"{TVMAZE}/shows/{tvmaze_id}", {})
        else:
            show = self._fetch(f"{TVMAZE}/lookup/shows", {"thetvdb": tvdb_id})
        if not isinstance(show, dict):
            return None
        info = SeriesNames(tvmaze_id=show.get("id") or tvmaze_id,
                           tvdb_id=tvdb_id or (show.get("externals") or {}).get("thetvdb"))
        names = [show.get("name") or ""]
        match = _SLUG_RE.search(show.get("officialSite") or "")
        if match:
            names.append(match.group(1).replace("-", " "))
        if info.tvmaze_id:
            try:
                akas = self._fetch(f"{TVMAZE}/shows/{info.tvmaze_id}/akas", {}) or []
                names += [a.get("name") or "" for a in akas if isinstance(a, dict)]
            except Exception as exc:
                log.debug("TVmaze akas %s: %s", info.tvmaze_id, exc)
        if info.tvdb_id:
            try:
                meta = self._fetch(f"{SKYHOOK}/{info.tvdb_id}", {}) or {}
                info.sonarr_title = str(meta.get("title") or "")
            except Exception as exc:
                log.debug("SkyHook %s: %s", info.tvdb_id, exc)
        seen: set[str] = set()
        info.names = [n for n in names if n.strip() and not (n.lower() in seen or seen.add(n.lower()))]
        return info
