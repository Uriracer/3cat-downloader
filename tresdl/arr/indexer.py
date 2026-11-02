"""Newznab searches over the 3cat catalogue (Sonarr ``tvsearch``, Radarr ``search``).

Matching is deliberately strict: a programme is only returned when its title
equals the searched title (accent/punctuation-insensitive). Releases are named
with the title Sonarr/Radarr searched for, so a loose match would make them
grab another programme's episodes under the wrong name.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from typing import Any, Optional, Protocol

from ..models import Episode
from . import releases as rl
from .releases import Release, Token

log = logging.getLogger(__name__)

HEIGHT_CACHE_TTL = 6 * 3600.0
MAX_RESULTS = 200
_YEAR_RE = re.compile(r"\s*[\(\[]?((?:19|20)\d{2})[\)\]]?\s*$")


class IndexClient(Protocol):
    def search(self, query: str, limit: int = 30) -> list[Any]: ...
    def list_episodes(self, program_id: int, season_id: str | None = None,
                      full_episodes_only: bool = True) -> list[Episode]: ...
    def get_media(self, video_id: int) -> Any: ...
    def films(self, force: bool = False) -> list[Episode]: ...
    def recent_episodes(self, limit: int = 200) -> list[Episode]: ...


def normalize(text: str) -> str:
    """Lower-case ASCII words: "Merlí (2015)" -> "merli 2015"."""
    return " ".join(re.findall(r"[a-z0-9]+", rl.ascii_fold(text or "").lower().replace("&", " and ")))


def split_year(query: str) -> tuple[str, Optional[int]]:
    """"La plaga 2022" -> ("La plaga", 2022)."""
    match = _YEAR_RE.search(query or "")
    if match and match.start() > 0:
        return query[:match.start()].strip(), int(match.group(1))
    return (query or "").strip(), None


def _numbers(ep: Episode) -> tuple[Optional[int], Optional[int]]:
    """(season, episode) as Sonarr sees them; same rule as tresdl.naming."""
    if ep.season_number is not None and ep.episode_number is not None:
        return ep.season_number, ep.episode_number
    if ep.season_number is None and ep.absolute_number is not None and ep.episode_number is None:
        return 1, ep.absolute_number
    if ep.season_number is None and ep.episode_number is not None:
        return 1, ep.episode_number
    return ep.season_number, ep.episode_number


def _guid(token: Token) -> str:
    return "3cat-" + hashlib.sha1(token.encode().encode("ascii")).hexdigest()[:20]


def _permalink(ep: Episode) -> str:
    return f"https://www.3cat.cat/3cat/video/{ep.id}/"


class Indexer:
    def __init__(self, client: IndexClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._heights: dict[int, tuple[float, Optional[int]]] = {}

    # ------------------------------------------------------------ helpers
    def _height(self, ep: Episode) -> Optional[int]:
        """Best resolution of a programme, probed on one of its videos (cached)."""
        key = ep.program_id or -ep.id
        with self._lock:
            cached = self._heights.get(key)
            if cached and time.monotonic() - cached[0] < HEIGHT_CACHE_TTL:
                return cached[1]
        height: Optional[int] = None
        try:
            try:
                media = self._client.get_media(ep.id, parse_dash=True)  # type: ignore[call-arg]
            except TypeError:
                media = self._client.get_media(ep.id)
            heights = [s.height or (1080 if s.kind == "dash" else None) for s in media.sources]
            height = max((h for h in heights if h), default=None)
        except Exception as exc:  # unavailable / geo-blocked: still list it, the download explains
            log.debug("Cannot probe video %s: %s", ep.id, exc)
        with self._lock:
            self._heights[key] = (time.monotonic(), height)
        return height

    def _programmes(self, query: str) -> list[Any]:
        wanted = normalize(query)
        if not wanted:
            return []
        found = []
        for prog in self._client.search(query, limit=15):
            names = {normalize(prog.title), normalize(getattr(prog, "slug", "").replace("-", " "))}
            if wanted in names:
                found.append(prog)
        return found

    def _episode_release(self, ep: Episode, series: str, height: Optional[int]) -> Optional[Release]:
        season, episode = _numbers(ep)
        if (season is None or episode is None) and not ep.air_date:
            return None
        name = rl.episode_name(series, season=season, episode=episode, title=ep.title,
                               air_date=ep.air_date, height=height)
        token = Token(kind="e", title=name, videos=[ep.id], category="tv")
        return Release(
            title=name, token=token, size=rl.estimate_size(ep.duration_seconds, height),
            category=rl.CAT_TV_HD if rl.bucket(height) >= 720 else rl.CAT_TV_SD,
            published=ep.air_date, season=season, episode=episode,
            description=ep.description[:300], comments=_permalink(ep), guid=_guid(token),
        )

    def _pack_release(self, episodes: list[Episode], series: str, season: int,
                      height: Optional[int]) -> Release:
        name = rl.season_pack_name(series, season, height)
        names = {}
        for ep in episodes:
            s, e = _numbers(ep)
            names[str(ep.id)] = rl.episode_name(series, season=s, episode=e, title=ep.title,
                                                air_date=ep.air_date, height=height)
        token = Token(kind="e", title=name, videos=[ep.id for ep in episodes], names=names, category="tv")
        size = sum(rl.estimate_size(ep.duration_seconds, height) for ep in episodes)
        latest = max((ep.air_date for ep in episodes if ep.air_date), default=None)
        return Release(
            title=name, token=token, size=size,
            category=rl.CAT_TV_HD if rl.bucket(height) >= 720 else rl.CAT_TV_SD,
            published=latest, season=season,
            description=f"{len(episodes)} capítols", guid=_guid(token),
        )

    # ------------------------------------------------------------ searches
    def tv_search(self, query: str, season: Optional[str] = None, ep: Optional[str] = None) -> list[Release]:
        """Sonarr ``t=tvsearch``: episodes (and a season pack) of the programme named ``query``."""
        title, _year = split_year(query)
        season_n = int(season) if season and season.isdigit() else None
        daily = None
        if season_n and season_n > 1900 and ep and "/" in ep:   # daily: season=2026, ep=11/02
            month, _, day = ep.partition("/")
            if month.isdigit() and day.isdigit():
                daily = f"{season_n:04d}-{int(month):02d}-{int(day):02d}"
        ep_n = int(ep) if ep and ep.isdigit() else None

        results: list[Release] = []
        for prog in self._programmes(title) or self._programmes(query):
            episodes = self._client.list_episodes(prog.id)
            if not episodes:
                continue
            height = self._height(episodes[0])
            if daily:
                chosen = [e for e in episodes if e.air_date == daily]
            else:
                chosen = [e for e in episodes
                          if (season_n is None or _numbers(e)[0] == season_n)
                          and (ep_n is None or _numbers(e)[1] == ep_n)]
            for item in chosen:
                rel = self._episode_release(item, title, height)
                if rel:
                    results.append(rel)
            if season_n is not None and ep_n is None and not daily:
                numbered = [e for e in chosen if _numbers(e)[1] is not None]
                if len(numbered) >= 2:
                    results.insert(0, self._pack_release(numbered, title, season_n, height))
        return results[:MAX_RESULTS]

    def movie_search(self, query: str) -> list[Release]:
        """Radarr ``t=search&q=<title> <year>`` over the film catalogue."""
        title, year = split_year(query)
        wanted = normalize(title)
        results: list[Release] = []
        if not wanted:
            return results
        for film in self._client.films():
            if normalize(film.program_title) != wanted and normalize(film.title) != wanted:
                continue
            height = self._height(film)
            name = rl.movie_name(title, year, height)   # the title Radarr searched for
            token = Token(kind="m", title=name, videos=[film.id], category="movies")
            results.append(Release(
                title=name, token=token, size=rl.estimate_size(film.duration_seconds, height),
                category=rl.CAT_MOVIES_HD if rl.bucket(height) >= 720 else rl.CAT_MOVIES_SD,
                published=film.air_date, description=film.description[:300],
                comments=_permalink(film), guid=_guid(token),
            ))
        return results

    def rss(self, movies: bool, limit: int = 100) -> list[Release]:
        """Latest releases (Sonarr/Radarr RSS sync and their connection test)."""
        results: list[Release] = []
        if movies:
            for film in self._client.films()[:limit]:
                name = rl.movie_name(film.program_title or film.title, None, 1080)
                token = Token(kind="m", title=name, videos=[film.id], category="movies")
                results.append(Release(
                    title=name, token=token, size=rl.estimate_size(film.duration_seconds, 1080),
                    category=rl.CAT_MOVIES_HD, published=film.air_date,
                    comments=_permalink(film), guid=_guid(token)))
            return results
        for ep in self._client.recent_episodes(limit):
            if not ep.program_title or ep.program_id == 0:
                continue
            rel = self._episode_release(ep, ep.program_title, 1080)
            if rel:
                results.append(rel)
        return results
