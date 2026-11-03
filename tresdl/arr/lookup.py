"""Metadata ids -> the names a series or film has on 3cat.

Sonarr names series after TheTVDB's English title ("Genesis (2026)") and
Radarr names films after TMDB ("13 Minutes"), while 3cat uses the Catalan
ones ("Gènesi", "13 minuts per matar Hitler"). This module bridges them:

* **Series** (Sonarr): TVmaze gives a series' original name, its akas and its
  official site (often the 3cat page, whose slug is the programme's); Sonarr's
  own metadata service, SkyHook, gives Sonarr's title and TheTVDB's episode
  list (to fix numbering differences). The reverse direction (3cat programme ->
  TheTVDB, for the RSS feed) searches TVmaze by name.
* **Films** (Radarr): Radarr's metadata service gives the original title and
  the Catalan/Spanish translations of a TMDB/IMDb id.

Everything is cached on disk (``arr_metadata.json`` in the data directory), so
restarts don't hit the services again. Failures are only cached in memory, for
a few minutes. TVmaze allows 20 requests every 10 seconds: calls are spaced.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx

from .. import __version__
from .releases import normalize

log = logging.getLogger(__name__)

TVMAZE = "https://api.tvmaze.com"
SKYHOOK = "https://skyhook.sonarr.tv/v1/tvdb/shows/en"
SKYHOOK_SEARCH = "https://skyhook.sonarr.tv/v1/tvdb/search/en/"
RADARR_API = "https://api.radarr.video/v1"

SERIES_TTL = 12 * 3600.0          # names + episode list (airing series gain episodes)
SEARCH_TTL = 24 * 3600.0
PROGRAMME_TTL = 7 * 24 * 3600.0   # 3cat programme -> TheTVDB id
PROGRAMME_MISS_TTL = 3 * 24 * 3600.0
FAILURE_TTL = 15 * 60.0
CACHE_FILE = "arr_metadata.json"
CACHE_VERSION = 1
MIN_INTERVAL = {"api.tvmaze.com": 0.55}   # seconds between requests to the same host

_SLUG_RE = re.compile(r"(?:3cat\.cat/3cat|ccma\.cat/(?:tv3|3cat)(?:/alacarta)?)/([a-z0-9-]+)", re.I)
_CATALAN_NETWORKS = re.compile(r"\b(tv3|3cat|33|super3|sx3|esport3|3/24|ccma|catalunya)\b", re.I)
_MOVIE_LANGS = ("ca", "es")

Fetch = Callable[[str, dict], Optional[Any]]   # (url, params) -> parsed JSON, None when not found


# --------------------------------------------------------------------------- data

@dataclass
class TvdbHit:
    """One result of Sonarr's own title search (SkyHook)."""
    tvdb_id: int
    title: str                 # "Dirty Linen", "Genesis (2026)"
    first_aired: str = ""      # "1999-04-19"


@dataclass
class SeriesNames:
    """What we know about one TheTVDB series."""
    tvdb_id: Optional[int] = None
    tvmaze_id: Optional[int] = None
    sonarr_title: str = ""                            # "Genesis (2026)"
    names: list[str] = field(default_factory=list)    # names to look for on 3cat: "Gènesi", "genesi"…
    episodes: list[list[Any]] = field(default_factory=list)   # TheTVDB [season, number, "YYYY-MM-DD"|None]


@dataclass
class MovieHit:
    """One result of Radarr's own title search."""
    tmdb_id: int
    title: str
    year: Optional[int] = None


@dataclass
class MovieNames:
    """What we know about one TMDB film."""
    tmdb_id: Optional[int] = None
    imdb_id: str = ""                                 # "tt0385880"
    title: str = ""                                   # Radarr's title: "13 Minutes"
    year: Optional[int] = None
    names: list[str] = field(default_factory=list)    # original title + Catalan/Spanish titles


# --------------------------------------------------------------------------- http

_rate_lock = threading.Lock()
_last_call: dict[str, float] = {}


def _wait_turn(url: str) -> None:
    host = urlparse(url).hostname or ""
    interval = MIN_INTERVAL.get(host)
    if not interval:
        return
    with _rate_lock:
        now = time.monotonic()
        wait = _last_call.get(host, 0.0) + interval - now
        _last_call[host] = now + max(wait, 0.0)
    if wait > 0:
        time.sleep(wait)


def _default_fetch(url: str, params: dict) -> Optional[Any]:
    headers = {"User-Agent": f"3cat-downloader/{__version__}"}
    with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as http:
        for attempt in range(3):
            _wait_turn(url)
            resp = http.get(url, params=params)
            if resp.status_code == 429 and attempt < 2:   # rate limited: wait as asked, then retry
                try:
                    delay = float(resp.headers.get("retry-after", "2"))
                except ValueError:
                    delay = 2.0
                time.sleep(min(max(delay, 1.0), 10.0))
                continue
            break
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _unique(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for name in names:
        key = normalize(name)
        if key and key not in seen:
            seen.add(key)
            out.append(name.strip())
    return out


# --------------------------------------------------------------------------- lookup

class SeriesLookup:
    """Cached lookups of series (TVmaze, SkyHook) and films (Radarr's metadata API)."""

    def __init__(self, fetch: Optional[Fetch] = None, cache_dir: Optional[Path] = None,
                 background: bool = True) -> None:
        self._fetch = fetch or _default_fetch
        self._lock = threading.RLock()
        self._path = Path(cache_dir) / CACHE_FILE if cache_dir else None
        self._entries: dict[str, list[Any]] = self._load()     # key -> [expires (unix time), value]
        self._failures: dict[str, float] = {}                  # key -> retry after (monotonic)
        self._background = background
        self._pending: "queue.Queue[str]" = queue.Queue()
        self._queued: set[str] = set()
        self._worker: Optional[threading.Thread] = None

    # ------------------------------------------------------------ cache
    def _load(self) -> dict[str, list[Any]]:
        if not self._path or not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("version") != CACHE_VERSION:
                return {}
            now = time.time()
            return {k: v for k, v in data.get("entries", {}).items()
                    if isinstance(v, list) and len(v) == 2 and v[0] > now}
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("No s'ha pogut llegir %s: %s", self._path, exc)
            return {}

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "entries": self._entries},
                                      ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("No s'ha pogut desar %s: %s", self._path, exc)

    def _cached(self, key: str, ttl: float, produce: Callable[[], Any], *,
                miss_ttl: Optional[float] = None, service: str = "") -> tuple[bool, Any]:
        """(found, value) for ``key``: from the cache, else ``produce()``.

        ``found`` is False only when the service failed (nothing cached).
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry[0] > time.time():
                return True, entry[1]
            if self._failures.get(key, 0.0) > time.monotonic():
                return False, None
        try:
            value = produce()
        except Exception as exc:
            log.warning("No s'ha pogut consultar %s (%s): %s", service or "el servei", key, exc)
            with self._lock:
                self._failures[key] = time.monotonic() + FAILURE_TTL
            return False, None
        with self._lock:
            life = ttl if value not in (None, [], {}) else (miss_ttl or ttl)
            self._entries[key] = [time.time() + life, value]
            self._save()
        return True, value

    def peek(self, key: str) -> tuple[bool, Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry[0] > time.time():
                return True, entry[1]
        return False, None

    # ------------------------------------------------------------ series
    def search(self, title: str) -> list[TvdbHit]:
        """TheTVDB series called ``title``, as Sonarr finds them (SkyHook)."""
        if not title.strip():
            return []

        def produce() -> list[dict]:
            return [asdict(TvdbHit(int(s["tvdbId"]), str(s.get("title") or ""), str(s.get("firstAired") or "")))
                    for s in (self._fetch(SKYHOOK_SEARCH, {"term": title}) or [])
                    if isinstance(s, dict) and s.get("tvdbId")]

        _ok, value = self._cached(f"search:{normalize(title)}", SEARCH_TTL, produce, service="SkyHook")
        return [TvdbHit(**v) for v in value or []]

    def find(self, tvdb_id: Optional[int] = None, tvmaze_id: Optional[int] = None) -> Optional[SeriesNames]:
        """Names and episodes of a series by TheTVDB or TVmaze id; None if unknown or offline."""
        if not (tvdb_id or tvmaze_id):
            return None
        key = f"tvdb:{tvdb_id}" if tvdb_id else f"tvmaze:{tvmaze_id}"
        _ok, value = self._cached(key, SERIES_TTL, lambda: self._series(tvdb_id, tvmaze_id),
                                  miss_ttl=FAILURE_TTL * 4, service="TVmaze")
        return SeriesNames(**value) if value else None

    def _series(self, tvdb_id: Optional[int], tvmaze_id: Optional[int]) -> Optional[dict]:
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
                info.episodes = [[e.get("seasonNumber"), e.get("episodeNumber"), e.get("airDate")]
                                 for e in meta.get("episodes") or []
                                 if isinstance(e, dict) and e.get("seasonNumber") is not None
                                 and e.get("episodeNumber") is not None]
            except Exception as exc:
                log.debug("SkyHook %s: %s", info.tvdb_id, exc)
        info.names = _unique(names)
        return asdict(info)

    def programme_series(self, name: str) -> Optional[SeriesNames]:
        """The TheTVDB series of a 3cat programme name (TVmaze search), or None."""
        found, tvdb_id = self._cached(f"prog:{normalize(name)}", PROGRAMME_TTL,
                                      lambda: self._programme_tvdb(name),
                                      miss_ttl=PROGRAMME_MISS_TTL, service="TVmaze")
        return self.find(tvdb_id=tvdb_id) if found and tvdb_id else None

    def programme_series_cached(self, name: str) -> Optional[SeriesNames]:
        """Like :meth:`programme_series`, without waiting: unknown names are resolved in the background."""
        found, tvdb_id = self.peek(f"prog:{normalize(name)}")
        if found:
            if not tvdb_id:
                return None
            ok, value = self.peek(f"tvdb:{tvdb_id}")
            if ok:
                return SeriesNames(**value) if value else None
        self._enqueue(name)
        return None

    def _programme_tvdb(self, name: str) -> Optional[int]:
        wanted = normalize(name)
        for result in self._fetch(f"{TVMAZE}/search/shows", {"q": name}) or []:
            show = (result or {}).get("show") or {}
            tvdb_id = (show.get("externals") or {}).get("thetvdb")
            if not tvdb_id or normalize(show.get("name") or "") != wanted:
                continue
            channel = " ".join(str((show.get(k) or {}).get("name") or "") for k in ("network", "webChannel"))
            if (show.get("language") == "Catalan" or _CATALAN_NETWORKS.search(channel)
                    or _SLUG_RE.search(show.get("officialSite") or "")):
                return int(tvdb_id)
        return None

    def _enqueue(self, name: str) -> None:
        if not self._background:
            return
        key = normalize(name)
        with self._lock:
            if key in self._queued or self._failures.get(f"prog:{key}", 0.0) > time.monotonic():
                return
            self._queued.add(key)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="arr-metadata", daemon=True)
                self._worker.start()
        self._pending.put(name)

    def _work(self) -> None:
        while True:
            try:
                name = self._pending.get(timeout=30)
            except queue.Empty:
                with self._lock:
                    if self._pending.empty():
                        self._worker = None
                        return
                continue
            try:
                info = self.programme_series(name)
                if info:
                    log.info("«%s» és «%s» a TheTVDB (%s)", name, info.sonarr_title, info.tvdb_id)
            except Exception as exc:   # never kill the worker
                log.debug("Background lookup %s: %s", name, exc)
            finally:
                with self._lock:
                    self._queued.discard(normalize(name))

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Wait for the background lookups (tests)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._queued:
                    return
            time.sleep(0.02)

    # ------------------------------------------------------------ films
    def movie(self, tmdb_id: Optional[int] = None, imdb_id: Optional[str] = None) -> Optional[MovieNames]:
        """Titles of a film by TMDB or IMDb id (Radarr's metadata); None if unknown or offline."""
        imdb = f"tt{str(imdb_id).lower().removeprefix('tt')}" if imdb_id else ""
        if not (tmdb_id or imdb):
            return None
        key = f"movie:tmdb:{tmdb_id}" if tmdb_id else f"movie:imdb:{imdb}"
        url = f"{RADARR_API}/movie/{tmdb_id}" if tmdb_id else f"{RADARR_API}/movie/imdb/{imdb}"
        _ok, value = self._cached(key, SEARCH_TTL, lambda: self._movie(url), miss_ttl=FAILURE_TTL * 4,
                                  service="Radarr")
        return MovieNames(**value) if value else None

    def _movie(self, url: str) -> Optional[dict]:
        data = self._fetch(url, {})
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        names = [str(data.get("OriginalTitle") or ""), str(data.get("Title") or "")]
        names += [str(t.get("Title") or "") for t in data.get("Translations") or []
                  if str(t.get("Language") or "")[:2].lower() in _MOVIE_LANGS]
        names += [str(t.get("Title") or "") for t in data.get("AlternativeTitles") or []
                  if str(t.get("Language") or "")[:2].lower() in _MOVIE_LANGS]
        return asdict(MovieNames(tmdb_id=data.get("TmdbId"), imdb_id=str(data.get("ImdbId") or ""),
                                 title=str(data.get("Title") or ""), year=data.get("Year") or None,
                                 names=_unique(names)))

    def movie_search(self, title: str, year: Optional[int] = None) -> list[MovieHit]:
        """TMDB films called ``title``, as Radarr finds them."""
        if not title.strip():
            return []
        params: dict[str, Any] = {"q": title}
        if year:
            params["year"] = year

        def produce() -> list[dict]:
            return [asdict(MovieHit(int(m["TmdbId"]), str(m.get("Title") or ""), m.get("Year") or None))
                    for m in (self._fetch(f"{RADARR_API}/search", params) or [])
                    if isinstance(m, dict) and m.get("TmdbId")]

        _ok, value = self._cached(f"msearch:{normalize(title)}|{year or ''}", SEARCH_TTL, produce,
                                  service="Radarr")
        return [MovieHit(**v) for v in value or []]


MetadataLookup = SeriesLookup
