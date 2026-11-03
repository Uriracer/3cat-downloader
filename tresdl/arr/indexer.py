"""Newznab searches over the 3cat catalogue (Sonarr ``tvsearch``, Radarr ``movie``/``search``).

Matching is deliberately strict: a programme is only returned when its title
equals the searched title (accent/punctuation-insensitive), or one of the
names the metadata services give for the searched id/title (``lookup.py``).
Releases are named with the title Sonarr/Radarr use, so a loose match would
make them grab another programme's episodes under the wrong name.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import defaultdict
from dataclasses import replace
from typing import Any, Optional, Protocol

from ..models import Episode
from . import releases as rl
from .lookup import MovieNames, SeriesLookup, SeriesNames
from .releases import Release, Token

log = logging.getLogger(__name__)

HEIGHT_CACHE_TTL = 6 * 3600.0
MAX_RESULTS = 200
_YEAR_RE = re.compile(r"\s*[\(\[]?((?:19|20)\d{2})[\)\]]?\s*$")

normalize = rl.normalize


class IndexClient(Protocol):
    def search(self, query: str, limit: int = 30) -> list[Any]: ...
    def list_episodes(self, program_id: int, season_id: str | None = None,
                      full_episodes_only: bool = True) -> list[Episode]: ...
    def get_media(self, video_id: int) -> Any: ...
    def films(self, force: bool = False) -> list[Episode]: ...
    def recent_episodes(self, limit: int = 200) -> list[Episode]: ...


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


def remap_numbers(episodes: list[Episode], tvdb: list[list[Any]]) -> list[Episode]:
    """3cat episodes renumbered as TheTVDB (Sonarr) numbers them, when they differ.

    ``tvdb`` is TheTVDB's episode list, ``[season, number, air date]``.

    1. Every 3cat (season, episode) exists on TheTVDB: kept as they are.
    2. Same number of regular episodes on both sides: matched in order
       (3cat seasons merged or split differently, e.g. one long season).
    3. Otherwise, an episode whose numbers don't exist on TheTVDB takes the
       numbers of the only TheTVDB episode aired the same day.
    Anything else is left alone: Sonarr will only grab what matches.
    """
    if not tvdb:
        return episodes
    known = {(s, e) for s, e, _d in tvdb}
    numbered = [(i, ep) for i, ep in enumerate(episodes) if None not in _numbers(ep)]
    if not numbered or all(_numbers(ep) in known for _i, ep in numbered):
        return episodes
    regular = sorted((s, e, d) for s, e, d in tvdb if s and s > 0)
    out = list(episodes)
    if len(numbered) == len(regular) and len(numbered) > 1:
        ordered = sorted(numbered, key=lambda item: (_numbers(item[1]), item[1].air_date or ""))
        for (i, ep), (season, number, _d) in zip(ordered, regular):
            out[i] = replace(ep, season_number=season, episode_number=number)
        return out
    by_date: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for season, number, aired in regular:
        if aired:
            by_date[str(aired)[:10]].append((season, number))
    for i, ep in numbered:
        if _numbers(ep) in known:
            continue
        same_day = by_date.get(ep.air_date or "", [])
        if len(same_day) == 1:
            out[i] = replace(ep, season_number=same_day[0][0], episode_number=same_day[0][1])
    return out


def _guid(token: Token) -> str:
    return "3cat-" + hashlib.sha1(token.encode().encode("ascii")).hexdigest()[:20]


def _permalink(ep: Episode) -> str:
    return f"https://www.3cat.cat/3cat/video/{ep.id}/"


class Indexer:
    def __init__(self, client: IndexClient, lookup: Optional[SeriesLookup] = None) -> None:
        self._client = client
        self._lookup = lookup or SeriesLookup()
        self._lock = threading.Lock()
        self._heights: dict[int, tuple[float, Optional[int]]] = {}

    @property
    def lookup(self) -> SeriesLookup:
        return self._lookup

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

    def _episode_release(self, ep: Episode, series: str, height: Optional[int],
                         tvdb_id: Optional[int] = None) -> Optional[Release]:
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
            description=ep.description[:300], comments=_permalink(ep), guid=_guid(token), tvdb_id=tvdb_id,
        )

    def _pack_release(self, episodes: list[Episode], series: str, season: int,
                      height: Optional[int], tvdb_id: Optional[int] = None) -> Release:
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
            description=f"{len(episodes)} capítols", guid=_guid(token), tvdb_id=tvdb_id,
        )

    def _movie_release(self, film: Episode, title: str, year: Optional[int],
                       info: Optional[MovieNames] = None, height: Optional[int] = None) -> Release:
        name = rl.movie_name(title, year, height)
        token = Token(kind="m", title=name, videos=[film.id], category="movies")
        return Release(
            title=name, token=token, size=rl.estimate_size(film.duration_seconds, height),
            category=rl.CAT_MOVIES_HD if rl.bucket(height) >= 720 else rl.CAT_MOVIES_SD,
            published=film.air_date, description=film.description[:300],
            comments=_permalink(film), guid=_guid(token),
            tmdb_id=info.tmdb_id if info else None, imdb_id=info.imdb_id if info else "",
        )

    # ------------------------------------------------------------ series
    def _programmes_by_id(self, tvdb_id: Optional[int],
                          tvmaze_id: Optional[int]) -> tuple[list[Any], Optional[SeriesNames]]:
        """3cat programmes of a TheTVDB/TVmaze series, and what we know about it."""
        info = self._lookup.find(tvdb_id=tvdb_id, tvmaze_id=tvmaze_id)
        if info is None:
            return [], None
        if tvdb_id and not info.tvdb_id:
            info.tvdb_id = tvdb_id
        for name in info.names:
            found = self._programmes(name)
            if found:
                return found, info
        log.info("La sèrie %s (TheTVDB %s) no és a 3cat", info.names, info.tvdb_id)
        return [], info

    def _programmes_by_tvdb_title(self, query: str) -> tuple[list[Any], Optional[SeriesNames], str]:
        """Sonarr's title ("Dirty Linen") -> TheTVDB series -> their 3cat name ("Plats bruts").

        Only series whose TheTVDB title equals ``query`` (year included) are
        considered, and their original name must equal a 3cat programme: as
        strict as the plain title search.
        """
        wanted = normalize(query)   # Sonarr searches TheTVDB's exact title: "Genesis (2026)" -> "Genesis 2026"
        hits = [hit for hit in self._lookup.search(query) if normalize(hit.title) == wanted]
        for hit in hits[:5]:
            programmes, info = self._programmes_by_id(hit.tvdb_id, None)
            if programmes:
                log.info("«%s» és «%s» a 3cat (TheTVDB %s)", query, programmes[0].title, hit.tvdb_id)
                return programmes, info, hit.title
        return [], None, ""

    def tv_search(self, query: str, season: Optional[str] = None, ep: Optional[str] = None,
                  tvdb_id: Optional[int] = None, tvmaze_id: Optional[int] = None) -> list[Release]:
        """Sonarr ``t=tvsearch``: episodes (and a season pack) of the programme named ``query``.

        With ``tvdbid``/``tvmazeid`` (or a title only TheTVDB knows) the
        programme is found by its original name, the releases are named with
        Sonarr's title, numbered as TheTVDB and carry the TheTVDB id, so Sonarr
        matches them even when the titles differ.
        """
        title, _year = split_year(query)
        programmes: list[Any] = []
        info: Optional[SeriesNames] = None
        if tvdb_id or tvmaze_id:
            programmes, info = self._programmes_by_id(tvdb_id, tvmaze_id)
            if programmes:
                title = (info.sonarr_title if info else "") or query.strip() or programmes[0].title
        if not programmes and title:
            programmes = self._programmes(title) or self._programmes(query)
            info = None
        if not programmes and title:
            programmes, info, tvdb_title = self._programmes_by_tvdb_title(query)
            title = tvdb_title or title
        known_id = info.tvdb_id if info and programmes else None
        season_n = int(season) if season and season.isdigit() else None
        daily = None
        if season_n and season_n > 1900 and ep and "/" in ep:   # daily: season=2026, ep=11/02
            month, _, day = ep.partition("/")
            if month.isdigit() and day.isdigit():
                daily = f"{season_n:04d}-{int(month):02d}-{int(day):02d}"
        ep_n = int(ep) if ep and ep.isdigit() else None

        results: list[Release] = []
        for prog in programmes:
            episodes = self._client.list_episodes(prog.id)
            if not episodes:
                continue
            if info and info.episodes:
                episodes = remap_numbers(episodes, info.episodes)
            height = self._height(episodes[0])
            if daily:
                chosen = [e for e in episodes if e.air_date == daily]
            else:
                chosen = [e for e in episodes
                          if (season_n is None or _numbers(e)[0] == season_n)
                          and (ep_n is None or _numbers(e)[1] == ep_n)]
            for item in chosen:
                rel = self._episode_release(item, title, height, known_id)
                if rel:
                    results.append(rel)
            if season_n is not None and ep_n is None and not daily:
                numbered = [e for e in chosen if _numbers(e)[1] is not None]
                if len(numbered) >= 2:
                    results.insert(0, self._pack_release(numbered, title, season_n, height, known_id))
        return results[:MAX_RESULTS]

    # ------------------------------------------------------------ films
    def _films_named(self, names: list[str]) -> list[Episode]:
        wanted = {normalize(n) for n in names} - {""}
        if not wanted:
            return []
        return [f for f in self._client.films()
                if normalize(f.program_title) in wanted or normalize(f.title) in wanted]

    def movie_search(self, query: str, tmdb_id: Optional[int] = None,
                     imdb_id: Optional[str] = None) -> list[Release]:
        """Radarr ``t=movie&tmdbid=…`` / ``t=search&q=<title> <year>`` over the film catalogue.

        By id (or a title only TMDB knows, e.g. "13 Minutes"), the film is found
        by its original or Catalan/Spanish title ("13 minuts per matar Hitler"),
        named with Radarr's title and year and tagged with the TMDB/IMDb ids.
        """
        title, year = split_year(query)
        info: Optional[MovieNames] = None
        films: list[Episode] = []
        if tmdb_id or imdb_id:
            info = self._lookup.movie(tmdb_id=tmdb_id, imdb_id=imdb_id)
            films = self._films_named(info.names) if info else []
        if not films and title:
            films = self._films_named([title])
        if not films and title:
            wanted = normalize(title)
            for hit in self._lookup.movie_search(title, year)[:5]:
                if normalize(hit.title) != wanted or (year and hit.year and hit.year != year):
                    continue
                info = self._lookup.movie(tmdb_id=hit.tmdb_id)
                films = self._films_named(info.names) if info else []
                if films:
                    log.info("«%s» és «%s» a 3cat (TMDB %s)", query, films[0].title, hit.tmdb_id)
                    break
        if not films:
            return []
        if info and info.title:
            title, year = info.title, info.year or year
        return [self._movie_release(film, title, year, info, self._height(film)) for film in films]

    # ------------------------------------------------------------ rss
    def rss(self, movies: bool, limit: int = 100) -> list[Release]:
        """Latest releases (Sonarr/Radarr RSS sync and their connection test).

        Episodes of programmes already identified on TheTVDB are named with
        Sonarr's title and carry the TheTVDB id; the others are identified in
        the background and get it on a later sync (the feed never waits).
        """
        results: list[Release] = []
        if movies:
            for film in self._client.films()[:limit]:
                results.append(self._movie_release(film, film.program_title or film.title, None, None, 1080))
            return results
        for ep in self._client.recent_episodes(limit):
            if not ep.program_title or ep.program_id == 0:
                continue
            series, tvdb_id = ep.program_title, None
            info = self._lookup.programme_series_cached(ep.program_title)
            if info and info.sonarr_title:
                series, tvdb_id = info.sonarr_title, info.tvdb_id
                ep = remap_numbers([ep], info.episodes)[0]
            rel = self._episode_release(ep, series, 1080, tvdb_id)
            if rel:
                results.append(rel)
        return results
