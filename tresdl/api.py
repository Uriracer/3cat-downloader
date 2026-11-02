"""Client for the public 3cat (formerly CCMA) JSON APIs.

The :class:`ThreeCatClient` is the only module that talks to 3cat. It turns the
raw, somewhat irregular JSON returned by ``api.3cat.cat`` and
``api-media.3cat.cat`` into the dataclasses defined in :mod:`tresdl.models`.

Things the raw API does that this module smooths over:

* every response is wrapped in ``{"resposta": {...}}``;
* a "list" may come back as a single object (normalised by :func:`as_list`);
* titles/descriptions may contain HTML entities, tags and stray whitespace;
* dates come as ``"dd/mm/YYYY HH:MM:SS"`` (converted to ISO ``YYYY-MM-DD``);
* durations come as ``"HH:MM:SS:FF"`` (frames) or milliseconds;
* old MP4 CDN hosts must be rewritten to the current ones;
* seasons are only fully visible through the episode listing.

This module never bypasses DRM or geo-blocking: it only reports what the API
offers (``MediaInfo.geo``) and flags DASH manifests that carry
``ContentProtection``.
"""
from __future__ import annotations

import difflib
import html
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from .models import Episode, MediaInfo, MediaSource, Program, Season, Subtitle

__all__ = [
    "ThreeCatError",
    "ThreeCatNotFound",
    "ThreeCatClient",
    "DashInfo",
    "ParsedUrl",
    "as_list",
    "clean_text",
    "clean_episode_title",
    "parse_season_number",
    "parse_date",
    "parse_duration",
    "pick_image",
    "rewrite_cdn_url",
    "classify_source",
    "parse_mpd",
    "parse_url",
    "normalize_for_search",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

API_BASE = "https://api.3cat.cat"
MEDIA_URL = "https://api-media.3cat.cat/pvideo/media.jsp"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

#: Common query parameters understood by api.3cat.cat.
COMMON_PARAMS: dict[str, str] = {
    "_format": "json",
    "sdom": "img",
    "version": "2.0",
    "https": "true",
    "master": "yes",
}

CATALOGUE_TTL = 24 * 3600.0     # seconds
PROGRAM_CACHE_TTL = 3600.0      # seconds; programme detail (canonical series title)
MEDIA_CACHE_TTL = 300.0         # seconds; media.jsp answers reused by get_episode/get_media
MEDIA_CACHE_SIZE = 64
FILMS_TTL = 6 * 3600.0          # seconds; film catalogue (format PUF_PEL)
RECENT_TTL = 600.0              # seconds; latest full episodes (Sonarr RSS)

RETRY_ATTEMPTS = 3
RETRY_STATUSES = {429, 500, 502, 503, 504}

#: Old S3-backed MP4 hosts (with their path prefix) -> current CDN hosts.
CDN_REWRITES: tuple[tuple[str, str], ...] = (
    ("mp4-high-es.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_ALTA_IPTV_ES", "mp4-down-high-es.3catvideos.cat"),
    ("mp4-high-int.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_ALTA_IPTV_MON", "mp4-down-high-int.3catvideos.cat"),
    ("mp4-medium-es.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_MITJA_WEB_ES", "mp4-down-medium-es.3catvideos.cat"),
    ("mp4-medium-int.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_MITJA_WEB_MON", "mp4-down-medium-int.3catvideos.cat"),
)

#: URL path segments that are sections of the site, never programme slugs.
_URL_SECTION_SEGMENTS = {
    "3cat", "tv3", "alacarta", "sx3", "super3", "33", "324", "esport3",
    "catradio", "catinfo", "programa", "programes", "serie", "series",
}
#: URL path segments that follow a slug but are not slugs themselves.
_URL_TRAILING_SEGMENTS = {
    "capitols", "capitol", "episodis", "videos", "video", "clips", "temporada",
    "temporades", "inici", "fitxa", "extres", "audios", "noticies",
}

_IMAGE_SKIP_RELS = {"img_logo", "img_iptv", "img_iptv2", "img_bravia"}

#: Languages kept by default by :meth:`ThreeCatClient.list_episodes`. 3cat
#: publishes dubbed/original-version copies (``PU_ANGLES``, ``PU_ARANES``,
#: ``PU_VOS``...) as separate items with the same episode numbers.
DEFAULT_LANGUAGES: tuple[str, ...] = ("PU_CATALA",)

#: ``programa`` values that are generic containers, never a series name
#: (compared after :func:`normalize_for_search`).
_GENERIC_PROGRAMME_LABELS = {
    "pellicula", "pellicules", "la gran pellicula", "miniserie", "telefilm",
    "documental", "film", "cinema",
}

_TITLE_PREFIX_RES = (
    # "T1xC1 - Els peripatètics", "T2 x C13: ..."
    re.compile(r"^\s*T\s*(?P<season>\d+)\s*x\s*C\s*(?P<episode>\d+)\b\s*(?:[-–—:.|]\s*)?", re.I),
    # "Capítol 5 - Títol"
    re.compile(r"^\s*Cap[ií]tol\s+(?P<episode>\d+)\b\s*(?:[-–—:.|]\s*)?", re.I),
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class ThreeCatError(Exception):
    """Any failure talking to 3cat. ``str(err)`` is a Catalan, user-facing message."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class ThreeCatNotFound(ThreeCatError):
    """The requested programme/video does not exist (HTTP 404 or empty answer)."""


# --------------------------------------------------------------------------- #
# Pure helpers (no network; unit-tested directly)
# --------------------------------------------------------------------------- #

def as_list(value: Any) -> list:
    """Normalise an API value that may be ``None``, a single object or a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def clean_text(value: Any) -> str:
    """Unescape HTML entities, drop tags, collapse whitespace, strip."""
    if value is None:
        return ""
    text = str(value)
    # Unescape twice: some fields are double-encoded ("&amp;eacute;").
    text = html.unescape(html.unescape(text))
    text = _TAG_RE.sub(" ", text)
    text = text.replace(" ", " ").replace("​", "")
    return _WS_RE.sub(" ", text).strip()


def clean_episode_title(raw: str) -> tuple[str, Optional[int], Optional[int]]:
    """Strip a ``T<s>xC<e> - `` / ``Capítol <n> - `` prefix.

    Returns ``(title, season_number, episode_number)``; the numbers are those
    found in the prefix (``None`` when absent). When nothing is left after the
    prefix (e.g. ``"T1xC1"``) the title becomes ``"Capítol <n>"``.
    """
    text = clean_text(raw)
    for regex in _TITLE_PREFIX_RES:
        match = regex.match(text)
        if not match:
            continue
        groups = match.groupdict()
        season = int(groups["season"]) if groups.get("season") else None
        episode = int(groups["episode"]) if groups.get("episode") else None
        rest = text[match.end():].strip(" -–—:.|")
        if not rest:
            rest = f"Capítol {episode}" if episode is not None else text
        return rest, season, episode
    return text, None, None


def parse_season_number(season_id: Any = None, desc: Any = None) -> Optional[int]:
    """Season number from ``PUTEMP_<n>`` or a description like ``"2a Temporada"``."""
    if season_id:
        match = re.search(r"(\d+)\s*$", str(season_id))
        if match:
            return int(match.group(1))
    if desc:
        text = clean_text(desc)
        match = re.search(r"(\d+)\s*(?:a|ª|º|na|ra|ta|\.)?\s*temporada", text, re.I)
        if not match:
            match = re.search(r"temporada\s*(\d+)", text, re.I)
        if match:
            return int(match.group(1))
    return None


def parse_date(value: Any) -> Optional[str]:
    """Parse the date formats used by 3cat into ISO ``YYYY-MM-DD``.

    Accepts ``"14/09/2015 21:55:08"``, ``"14/09/2015"``, ISO strings
    (``"2015-09-14T21:55:08+02:00"``) and dicts ``{"text", "utc"}`` from media.jsp.
    """
    if isinstance(value, dict):
        return parse_date(value.get("utc")) or parse_date(value.get("text"))
    if not value:
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None
    return None


def parse_duration(value: Any) -> Optional[int]:
    """Duration in whole seconds.

    Accepts ``"HH:MM:SS:FF"`` (frames ignored), ``"HH:MM:SS"``, ``"MM:SS"``,
    plain seconds, and media.jsp dicts ``{"text", "milisegons"}``.
    """
    if isinstance(value, dict):
        ms = value.get("milisegons")
        if isinstance(ms, (int, float)) and ms > 0:
            return int(round(ms / 1000))
        return parse_duration(value.get("text"))
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    parts = str(value).strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 4:
        h, m, s = nums[:3]
    elif len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, (m, s) = 0, nums
    elif len(nums) == 1:
        h, m, s = 0, 0, nums[0]
    else:
        return None
    total = h * 3600 + m * 60 + s
    return total or None


def _parse_size(mida: Any) -> Optional[tuple[int, int]]:
    match = re.search(r"(\d+)\s*[xX]\s*(\d+)", str(mida or ""))
    if not match:
        return None
    w, h = int(match.group(1)), int(match.group(2))
    return (w, h) if w > 0 and h > 0 else None


def pick_image(images: Any) -> Optional[str]:
    """Pick the best image URL: prefer 16:9, then the largest.

    ``images`` is either the API list of ``{text, mida, rel_name}`` entries or
    a media.jsp dict ``{url, amplada, alcada}``. Logos and IPTV banners are
    ignored unless nothing else exists.
    """
    if isinstance(images, dict) and "url" in images:
        return images.get("url") or None
    entries = [i for i in as_list(images) if isinstance(i, dict) and (i.get("text") or i.get("url"))]
    if not entries:
        return None

    def score(entry: dict) -> tuple:
        rel = str(entry.get("rel_name") or "").lower()
        size = _parse_size(entry.get("mida"))
        master = str(entry.get("mida") or "").upper() == "MASTER"
        is_169 = "169" in rel or (size is not None and abs(size[0] / size[1] - 16 / 9) < 0.06)
        # A MASTER of an explicit 16:9 slot is the original; rank it as Full HD.
        area = size[0] * size[1] if size else (1920 * 1080 if master and "169" in rel else 0)
        usable = rel not in _IMAGE_SKIP_RELS
        return (usable, is_169, area)

    best = max(entries, key=score)
    return best.get("text") or best.get("url")


def rewrite_cdn_url(url: str) -> str:
    """Rewrite legacy MP4 CDN URLs to the current ``*.3catvideos.cat`` hosts."""
    for old, new in CDN_REWRITES:
        if old in url:
            url = url.replace(old, new)
            break
    if url.startswith("http://") and "3catvideos.cat" in url:
        url = "https://" + url[len("http://"):]
    return url


def classify_source(url: str, label: str) -> MediaSource:
    """Build a :class:`MediaSource` from a media.jsp ``{file, label}`` entry."""
    url = rewrite_cdn_url(url)
    lower = url.lower().split("?", 1)[0]
    lab = (label or "").strip()
    if lab.upper() == "DASH" or lower.endswith(".mpd"):
        kind = "dash"
    elif lab.upper() == "HLS" or lower.endswith(".m3u8"):
        kind = "hls"
    else:
        kind = "mp4"
    height: Optional[int] = None
    match = re.search(r"(\d{3,4})\s*p\b", lab, re.I)
    if match:
        height = int(match.group(1))
    return MediaSource(url=url, kind=kind, label=lab or kind.upper(), height=height)


@dataclass
class DashInfo:
    """What we learn from a DASH manifest."""
    max_height: Optional[int]
    protected: bool              # True when the MPD declares ContentProtection (DRM)


def parse_mpd(text: str) -> DashInfo:
    """Extract max video height and DRM flag from an MPD document (regex based,
    so a hostile manifest cannot trigger XML entity expansion)."""
    heights = [int(h) for h in re.findall(r"\bheight\s*=\s*\"(\d+)\"", text)]
    return DashInfo(max_height=max(heights) if heights else None,
                    protected=bool(re.search(r"<\s*(?:\w+:)?ContentProtection\b", text)))


@dataclass
class ParsedUrl:
    """Result of parsing a 3cat/ccma URL without touching the network."""
    video_id: Optional[int]
    slugs: list[str]             # candidate programme slugs, most likely first


def parse_url(text: str) -> Optional[ParsedUrl]:
    """Parse a 3cat.cat / ccma.cat URL.

    Returns ``None`` when ``text`` is not a URL at all. Raises
    :class:`ThreeCatError` when it is a URL of another website.
    """
    raw = (text or "").strip()
    if not raw or " " in raw:
        return None
    candidate = raw
    if not re.match(r"^[a-z][a-z0-9+.-]*://", candidate, re.I):
        if not re.match(r"^(?:www\.)?[\w-]+(?:\.[\w-]+)+/", candidate + "/"):
            return None
        if not re.search(r"(?:^|\.)(?:3cat|ccma)\.cat(?:/|$)", candidate.split("?", 1)[0], re.I):
            # Looks like "foo.bar" but not a 3cat host and no scheme: not a URL for us.
            return None
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if not (host == "3cat.cat" or host.endswith(".3cat.cat") or host == "ccma.cat" or host.endswith(".ccma.cat")):
        raise ThreeCatError("Aquest enllaç no és de 3Cat. Enganxa un enllaç de www.3cat.cat.")
    segments = [s for s in parsed.path.split("/") if s]
    video_id: Optional[int] = None
    for i, seg in enumerate(segments[:-1]):
        if seg.lower() == "video" and segments[i + 1].isdigit():
            video_id = int(segments[i + 1])
            segments = segments[:i]
            break
    else:
        # Legacy forms such as ".../video.html?id=123" or "?video=123".
        match = re.search(r"(?:^|&)(?:id|video|idint)=(\d+)", parsed.query)
        if match and any(s.lower().startswith("video") for s in segments):
            video_id = int(match.group(1))
    slugs = [
        s.lower() for s in segments
        if s.lower() not in _URL_SECTION_SEGMENTS
        and s.lower() not in _URL_TRAILING_SEGMENTS
        and not s.isdigit()
        and "." not in s
    ]
    return ParsedUrl(video_id=video_id, slugs=slugs)


def normalize_for_search(text: str) -> str:
    """Lower-case, accent-free, punctuation-free version of ``text``."""
    text = clean_text(text).lower().replace("l·l", "ll").replace("l.l", "ll")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def _match_score(query: str, title: str, description: str = "") -> float:
    """Fuzzy relevance (0..100) of an already-normalised query against a title."""
    if not query or not title:
        return 0.0
    if title == query:
        return 100.0
    if title.startswith(query):
        return 90.0
    if re.search(r"\b" + re.escape(query), title):
        return 80.0
    if query in title:
        return 70.0
    q_tokens = query.split()
    t_tokens = title.split()
    if q_tokens and all(any(t.startswith(q) for t in t_tokens) for q in q_tokens):
        return 65.0
    # Typo tolerance: best token-level similarity, averaged over query tokens.
    if q_tokens and t_tokens:
        token_scores = [max(difflib.SequenceMatcher(None, q, t).ratio() for t in t_tokens) for q in q_tokens]
        token_avg = sum(token_scores) / len(token_scores)
    else:
        token_avg = 0.0
    whole = difflib.SequenceMatcher(None, query, title).ratio()
    best = max(token_avg, whole)
    score = best * 60.0 if best >= 0.75 else 0.0
    if description and len(query) >= 4 and re.search(r"\b" + re.escape(query), description):
        score = max(score, 40.0)
    return score


def _first_dict(value: Any) -> dict:
    for item in as_list(value):
        if isinstance(item, dict):
            return item
    return {}


def _main_entry(value: Any) -> dict:
    """The entry flagged ``main`` in a list of ``{id, desc, main}``, else the first."""
    entries = [e for e in as_list(value) if isinstance(e, dict)]
    for entry in entries:
        if entry.get("main"):
            return entry
    return entries[0] if entries else {}


def _to_int(value: Any) -> Optional[int]:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number


def _positive(value: Any) -> Optional[int]:
    number = _to_int(value)
    return number if number is not None and number > 0 else None


def _item_language(item: dict) -> str:
    """Main language id of a videos item (``"PU_CATALA"``...), ``""`` if unknown."""
    return str(_main_entry(item.get("idiomes")).get("id") or "").strip().upper()


def _episode_number(title_season: Optional[int], title_episode: Optional[int],
                    capitol_temporada: Any, has_season: bool) -> Optional[int]:
    """In-season episode number, shared by the listing and single-video paths.

    An explicit ``T<s>xC<e>`` title prefix wins: it is what 3cat shows and it
    is self-consistent, while ``capitol_temporada`` is sometimes the absolute
    number or off by one. ``capitol_temporada`` is used otherwise, and a bare
    ``Capítol <n>`` prefix only as the last resort for videos with a season.
    """
    if title_season is not None and title_episode is not None:
        return title_episode
    number = _positive(capitol_temporada)
    if number is None and has_season:
        number = title_episode
    return number


def _episode_sort_key(ep: Episode) -> tuple:
    return (
        ep.season_number is None, ep.season_number or 0,
        ep.episode_number is None, ep.episode_number or 0,
        ep.air_date is None, ep.air_date or "",
        ep.absolute_number is None, ep.absolute_number or 0,
        ep.id,
    )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class ThreeCatClient:
    """Thread-safe client for the 3cat APIs.

    One instance can be shared by the server and the download workers.
    """

    #: Page size used for the episode listing (tests may lower it).
    page_size: int = 1000

    def __init__(self, http: httpx.Client | None = None, timeout: float = 20.0) -> None:
        self._owns_http = http is None
        self._http = http or httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.8",
                     "Accept-Language": "ca,es;q=0.8,en;q=0.5"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._timeout = timeout
        self._lock = threading.RLock()
        self._catalogue: list[Program] = []
        self._catalogue_at: float = 0.0
        self._films: list[Episode] = []
        self._films_at: float = 0.0
        self._recent: list[Episode] = []
        self._recent_at: float = 0.0
        self._media_cache: "OrderedDict[int, tuple[float, dict]]" = OrderedDict()
        self._season_titles: dict[str, str] = {}
        # program id -> (fetched at, Program, programme detail lists seasons)
        self._programs: dict[int, tuple[float, Program, bool]] = {}
        # program id -> its episode listing has seasons (learned by list_episodes)
        self._seasonal: dict[int, bool] = {}

    # ------------------------------------------------------------------ infra
    def close(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "ThreeCatClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, url: str, params: Optional[dict] = None) -> httpx.Response:
        """GET with up to :data:`RETRY_ATTEMPTS` attempts on transient errors."""
        last_exc: Optional[Exception] = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = self._http.get(url, params=params)
            except httpx.TransportError as exc:      # timeouts, DNS, resets...
                last_exc = exc
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                last_exc = ThreeCatError(
                    f"El servidor de 3Cat ha respost amb l'error {response.status_code}.",
                    status=response.status_code)
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(0.5 * (2 ** attempt))
        if isinstance(last_exc, ThreeCatError):
            raise last_exc
        raise ThreeCatError(
            "No s'ha pogut connectar amb 3Cat. Comprova la connexió a internet.") from last_exc

    def _get_json(self, url: str, params: Optional[dict] = None) -> Any:
        response = self._get(url, params)
        if response.status_code == 404:
            raise ThreeCatNotFound("No s'ha trobat el contingut a 3Cat.", status=404)
        if response.status_code >= 400:
            raise ThreeCatError(
                f"El servidor de 3Cat ha respost amb l'error {response.status_code}.",
                status=response.status_code)
        try:
            return response.json()
        except ValueError as exc:
            raise ThreeCatError("Resposta inesperada del servidor de 3Cat.") from exc

    def _api(self, path: str, **params: Any) -> dict:
        """Call api.3cat.cat and return the unwrapped ``resposta`` dict."""
        query = dict(COMMON_PARAMS)
        query.update({k: v for k, v in params.items() if v is not None})
        data = self._get_json(f"{API_BASE}/{path.lstrip('/')}", query)
        resposta = data.get("resposta") if isinstance(data, dict) else None
        if not isinstance(resposta, dict):
            raise ThreeCatError("Resposta inesperada del servidor de 3Cat.")
        status = str(resposta.get("status", "OK")).upper()
        if status not in ("OK", ""):
            raise ThreeCatError(f"3Cat ha retornat un error ({resposta.get('status')}).")
        return resposta

    # -------------------------------------------------------------- programmes
    @staticmethod
    def _program_from_item(item: dict) -> Program:
        return Program(
            id=_to_int(item.get("id")) or 0,
            slug=str(item.get("nombonic") or item.get("nom_bonic") or "").strip(),
            title=clean_text(item.get("titol")),
            description=clean_text(item.get("entradeta") or item.get("entradeta_promo")),
            image=pick_image(item.get("imatges")),
            info=clean_text(item.get("info_distribucio")),
        )

    def catalogue(self, force: bool = False) -> list[Program]:
        """All programmes (cached in memory for :data:`CATALOGUE_TTL`)."""
        with self._lock:
            fresh = self._catalogue and (time.monotonic() - self._catalogue_at) < CATALOGUE_TTL
            if fresh and not force:
                return list(self._catalogue)
        resposta = self._api("programestv", items_pagina=2000)
        items = resposta.get("items") or {}
        programs: list[Program] = []
        seen: set[int] = set()
        for letter in as_list(items.get("lletra") if isinstance(items, dict) else None):
            for raw in as_list(letter.get("item") if isinstance(letter, dict) else None):
                if not isinstance(raw, dict):
                    continue
                program = self._program_from_item(raw)
                if program.id and program.id not in seen:
                    seen.add(program.id)
                    programs.append(program)
        # Some API versions return a flat "item" list instead of letters.
        if not programs and isinstance(items, dict):
            for raw in as_list(items.get("item")):
                if isinstance(raw, dict):
                    program = self._program_from_item(raw)
                    if program.id and program.id not in seen:
                        seen.add(program.id)
                        programs.append(program)
        if not programs:
            raise ThreeCatError("No s'ha pogut obtenir el catàleg de programes de 3Cat.")
        with self._lock:
            self._catalogue = programs
            self._catalogue_at = time.monotonic()
        return list(programs)

    def search(self, query: str, limit: int = 30) -> list[Program]:
        """Search programmes.

        Uses the 3cat search engine and merges in accent-insensitive fuzzy
        matches from the cached catalogue (the fallback when the engine fails
        or finds nothing). Results are ranked by title similarity.
        """
        query = (query or "").strip()
        if not query or limit <= 0:
            return []
        remote: list[Program] = []
        try:
            resposta = self._api("cercador/tot", text=query, tipologia="PTVC_PROGRAMA",
                                 items_pagina=max(limit, 20), pagina=1)
            items = resposta.get("items") or {}
            for raw in as_list(items.get("item") if isinstance(items, dict) else items):
                if isinstance(raw, dict) and raw.get("id"):
                    remote.append(self._program_from_item(raw))
        except ThreeCatError:
            remote = []
        try:
            local = self.catalogue()
        except ThreeCatError:
            local = []
            if not remote:
                raise

        norm_q = normalize_for_search(query)
        scored: dict[int, tuple[float, int, Program]] = {}
        for order, program in enumerate(remote):
            s = _match_score(norm_q, normalize_for_search(program.title))
            # Engine hits are relevant even when the title does not match.
            scored[program.id] = (max(s, 30.0), order, program)
        offset = len(remote)
        for order, program in enumerate(local):
            s = _match_score(norm_q, normalize_for_search(program.title),
                             normalize_for_search(program.description))
            if s <= 0:
                continue
            if program.id in scored:
                prev = scored[program.id]
                # Prefer the catalogue copy when it carries more data (e.g. info).
                merged = prev[2] if prev[2].info or not program.info else program
                scored[program.id] = (max(prev[0], s), prev[1], merged)
            else:
                scored[program.id] = (s, offset + order, program)
        ranked = sorted(scored.values(), key=lambda t: (-t[0], t[1]))
        return [p for _, _, p in ranked[:limit]]

    def get_program(self, program_id: int) -> Program:
        """Programme detail. Raises :class:`ThreeCatNotFound` if it does not exist.

        Answers are cached for :data:`PROGRAM_CACHE_TTL` (the title is needed
        by every episode listing and single-video lookup).
        """
        program_id = int(program_id)
        now = time.monotonic()
        with self._lock:
            hit = self._programs.get(program_id)
            if hit and now - hit[0] < PROGRAM_CACHE_TTL:
                return replace(hit[1])
        try:
            resposta = self._api(f"programestv/{program_id}/")
        except ThreeCatNotFound:
            raise ThreeCatNotFound(f"No existeix cap programa amb l'identificador {program_id}.", 404)
        item = _first_dict(resposta.get("item"))
        if not item:
            raise ThreeCatNotFound(f"No existeix cap programa amb l'identificador {program_id}.", 404)
        program = self._program_from_item(item)
        if not program.id:
            program.id = program_id
        has_seasons = any(isinstance(t, dict) for t in as_list(item.get("temporades")))
        with self._lock:
            self._programs[program_id] = (now, replace(program), has_seasons)
        return program

    def _canonical_title(self, program_id: int) -> str:
        """The programme's own title (detail, else catalogue); ``""`` if unknown.

        This is the series name used for folders and file names. The per-item
        ``programa`` field of the listings is not reliable (it often names a
        sub-show, a generic label such as "Pel·lícula", or differs in case).
        """
        if not program_id:
            return ""
        try:
            return self.get_program(program_id).title
        except ThreeCatError:
            program = self._program_from_catalogue(program_id)
            return program.title if program else ""

    def _match_programme(self, name: str) -> Program | None:
        """The single catalogue programme whose title equals ``name`` (accent-insensitive)."""
        wanted = normalize_for_search(name)
        if not wanted or wanted in _GENERIC_PROGRAMME_LABELS:
            return None
        try:
            matches = [p for p in self.catalogue() if normalize_for_search(p.title) == wanted]
        except ThreeCatError:
            return None
        return matches[0] if len(matches) == 1 else None

    def program_by_slug(self, slug: str) -> Program | None:
        """Find a programme by its ``nombonic`` slug (case-insensitive)."""
        wanted = (slug or "").strip().strip("/").lower()
        if not wanted:
            return None
        for program in self.catalogue():
            if program.slug.lower() == wanted:
                return program
        # The catalogue only lists currently published programmes; try the engine.
        try:
            for program in self.search(wanted.replace("-", " "), limit=10):
                if program.slug.lower() == wanted:
                    return program
        except ThreeCatError:
            pass
        return None

    def _program_from_catalogue(self, program_id: int) -> Program | None:
        try:
            for program in self.catalogue():
                if program.id == program_id:
                    return program
        except ThreeCatError:
            pass
        return None

    # ---------------------------------------------------------------- episodes
    def _episode_from_item(self, item: dict, program_id: int, program_title: str) -> Episode:
        raw_title = clean_text(item.get("titol") or item.get("permatitle"))
        title, t_season, t_episode = clean_episode_title(raw_title)

        season = _main_entry(item.get("temporades"))
        season_id = str(season.get("id") or "").strip()
        season_desc = clean_text(season.get("desc"))
        season_number = parse_season_number(season_id, season_desc)
        if season_id and season_desc:
            with self._lock:
                self._season_titles[season_id] = season_desc
        if season_number is None and not season_id:
            season_number = t_season
        if not season_id and season_number is not None:
            # Season known only from the "T<s>xC<e>" title: use the canonical
            # id so the episode joins the real season (list_seasons groups by id).
            season_id = f"PUTEMP_{season_number}"

        episode_number = _episode_number(t_season, t_episode, item.get("capitol_temporada"),
                                         bool(season_id))

        prog_entries = [e for e in as_list(item.get("programes_tv")) if isinstance(e, dict)]
        prog_entry = next((e for e in prog_entries if program_id and _to_int(e.get("id")) == program_id),
                          prog_entries[0] if prog_entries else {})
        pid = program_id or _to_int(prog_entry.get("id")) or 0
        ptitle = (program_title
                  or clean_text(prog_entry.get("titol") or prog_entry.get("desc"))
                  or clean_text(item.get("programa")))

        return Episode(
            id=int(item["id"]),
            program_id=pid,
            program_title=ptitle,
            title=title or raw_title,
            raw_title=raw_title,
            season_id=season_id,
            season_number=season_number,
            episode_number=episode_number,
            absolute_number=_positive(item.get("capitol")),
            air_date=parse_date(item.get("data_emissio") or item.get("data_publicacio")),
            duration_seconds=parse_duration(item.get("durada")),
            description=clean_text(item.get("entradeta") or item.get("entradeta_promo")),
            image=pick_image(item.get("imatges")),
        )

    def list_episodes(self, program_id: int, season_id: str | None = None,
                      full_episodes_only: bool = True, *,
                      languages: tuple[str, ...] | None = DEFAULT_LANGUAGES) -> list[Episode]:
        """Every episode of a programme (all pages), optionally one season only.

        Sorted by ``(season_number, episode_number, air_date)``; ``None`` sorts last.

        * The series title is the programme's own title (:meth:`get_program`),
          the same for every episode; item fields are only a fallback.
        * Items in other languages than ``languages`` (dubbed or original
          version copies) are left out, unless nothing else is left.
          ``languages=None`` keeps everything.
        * In a programme that has seasons, videos without a season are extras:
          they get ``season_number = 0`` (named as Specials by :mod:`naming`).
        * ``season_id`` is applied after these rules (locally), so episodes
          whose season is only known from their title are included.
        """
        program_id = int(program_id)
        params: dict[str, Any] = {"programatv_id": program_id, "items_pagina": self.page_size}
        if full_episodes_only:
            params["tipus_contingut"] = "PPD"
        program_title = self._canonical_title(program_id)

        raw_items: dict[int, dict] = {}
        page = 1
        while True:
            resposta = self._api("videos", pagina=page, **params)
            items = resposta.get("items") or {}
            page_items = as_list(items.get("item") if isinstance(items, dict) else items)
            for raw in page_items:
                vid = _to_int(raw.get("id")) if isinstance(raw, dict) else None
                if vid is not None:
                    raw_items.setdefault(vid, raw)
            pagination = resposta.get("paginacio") or {}
            total_pages = _to_int(pagination.get("total_pagines")) or 0
            if not page_items or page >= total_pages:
                break
            page += 1

        selected = list(raw_items.values())
        if languages is not None:
            wanted = {lang.upper() for lang in languages}
            kept = [r for r in selected if not _item_language(r) or _item_language(r) in wanted]
            if kept:          # a programme only available in V.O. is still listed
                selected = kept
        episodes = [self._episode_from_item(raw, program_id, program_title) for raw in selected]

        seasonal = any(ep.season_number is not None for ep in episodes)
        with self._lock:
            self._seasonal[program_id] = seasonal
        if seasonal:
            for ep in episodes:
                if ep.season_number is None:
                    ep.season_number = 0
        if season_id:
            episodes = [ep for ep in episodes if ep.season_id == season_id]
        return sorted(episodes, key=_episode_sort_key)

    def films(self, force: bool = False) -> list[Episode]:
        """Every film on 3cat (format ``PUF_PEL``), newest first; cached.

        Each film is an :class:`Episode` with ``program_id = 0`` and
        ``program_title`` = its own title (same rule as :meth:`get_episode`).
        """
        with self._lock:
            if self._films and not force and time.monotonic() - self._films_at < FILMS_TTL:
                return list(self._films)
        raw: dict[int, dict] = {}
        page = 1
        while True:
            resposta = self._api("videos", fills="format_contingut", format_contingut="PUF_PEL",
                                 items_pagina=1000, pagina=page)
            items = resposta.get("items") or {}
            page_items = as_list(items.get("item") if isinstance(items, dict) else items)
            for item in page_items:
                vid = _to_int(item.get("id")) if isinstance(item, dict) else None
                if vid is not None:
                    raw.setdefault(vid, item)
            total_pages = _to_int((resposta.get("paginacio") or {}).get("total_pagines")) or 0
            if not page_items or page >= total_pages or page >= 10:
                break
            page += 1
        films: list[Episode] = []
        for item in raw.values():
            ep = self._episode_from_item(item, 0, "")
            ep.program_id = 0
            ep.program_title = ep.title or ep.raw_title
            ep.season_id, ep.season_number, ep.episode_number = "", None, None
            films.append(ep)
        films.sort(key=lambda e: e.air_date or "", reverse=True)
        with self._lock:
            self._films, self._films_at = films, time.monotonic()
        return list(films)

    def recent_episodes(self, limit: int = 200) -> list[Episode]:
        """Latest full episodes published on 3cat (all programmes); cached briefly."""
        with self._lock:
            if self._recent and time.monotonic() - self._recent_at < RECENT_TTL:
                return list(self._recent[:limit])
        resposta = self._api("videos", tipus_contingut="PPD", items_pagina=max(limit, 50), pagina=1,
                             ordre="-data_publicacio")
        items = resposta.get("items") or {}
        episodes: list[Episode] = []
        for item in as_list(items.get("item") if isinstance(items, dict) else items):
            if not isinstance(item, dict) or _to_int(item.get("id")) is None:
                continue
            try:
                episodes.append(self._episode_from_item(item, 0, ""))
            except (KeyError, TypeError, ValueError):
                continue
        with self._lock:
            self._recent, self._recent_at = episodes, time.monotonic()
        return episodes[:limit]

    def list_seasons(self, episodes: list[Episode]) -> list[Season]:
        """Derive the seasons present in an episode list, sorted by number."""
        groups: "OrderedDict[str, list[Episode]]" = OrderedDict()
        for episode in episodes:
            groups.setdefault(episode.season_id or "", []).append(episode)
        seasons: list[Season] = []
        for sid, eps in groups.items():
            number = next((e.season_number for e in eps if e.season_number is not None), None)
            if sid:
                with self._lock:
                    title = self._season_titles.get(sid, "")
                if not title:
                    title = f"Temporada {number}" if number is not None else sid
            elif number == 0:
                title = "Especials i extres"
            else:
                title = f"Temporada {number}" if number is not None else "Sense temporada"
            seasons.append(Season(id=sid, number=number, title=title, episode_count=len(eps)))
        # Real seasons first; specials (0) and "no season" last.
        seasons.sort(key=lambda s: (not s.number, s.number is None, s.number or 0, s.id))
        return seasons

    # ------------------------------------------------------------------- media
    def _media_json(self, video_id: int) -> dict:
        """Raw media.jsp answer (cached for :data:`MEDIA_CACHE_TTL`)."""
        video_id = int(video_id)
        now = time.monotonic()
        with self._lock:
            hit = self._media_cache.get(video_id)
            if hit and now - hit[0] < MEDIA_CACHE_TTL:
                self._media_cache.move_to_end(video_id)
                return hit[1]
        params = {"media": "video", "versio": "vast", "idint": str(video_id),
                  "profile": "pc_3cat", "format": "dm"}
        try:
            data = self._get_json(MEDIA_URL, params)
        except ThreeCatNotFound:
            raise ThreeCatNotFound(f"No existeix cap vídeo amb l'identificador {video_id}.", 404)
        if not isinstance(data, dict) or not isinstance(data.get("informacio"), dict):
            raise ThreeCatNotFound(f"No existeix cap vídeo amb l'identificador {video_id}.", 404)
        with self._lock:
            self._media_cache[video_id] = (now, data)
            self._media_cache.move_to_end(video_id)
            while len(self._media_cache) > MEDIA_CACHE_SIZE:
                self._media_cache.popitem(last=False)
        return data

    @staticmethod
    def _sources_from(media: Any) -> list[MediaSource]:
        sources: list[MediaSource] = []
        seen: set[str] = set()
        media = media if isinstance(media, dict) else {}
        for entry in as_list(media.get("url")):
            if isinstance(entry, str):
                entry = {"file": entry, "label": ""}
            if not isinstance(entry, dict) or not entry.get("file"):
                continue
            source = classify_source(str(entry["file"]), str(entry.get("label") or ""))
            if source.url in seen:
                continue
            seen.add(source.url)
            sources.append(source)
        # Best first: DASH (adaptive, up to 1080p), then HLS, then MP4 by height.
        rank = {"dash": 0, "hls": 1, "mp4": 2}
        sources.sort(key=lambda s: (rank.get(s.kind, 3), -(s.height or 0)))
        return sources

    def dash_info(self, url: str) -> DashInfo:
        """Download and inspect a DASH manifest (max height, DRM flag)."""
        response = self._get(url)
        if response.status_code >= 400:
            raise ThreeCatError(
                f"No s'ha pogut llegir el manifest DASH (error {response.status_code}).",
                status=response.status_code)
        return parse_mpd(response.text)

    def get_media(self, video_id: int, *, parse_dash: bool = False) -> MediaInfo:
        """Streams, subtitles and audio-description variant of a video.

        With ``parse_dash=True`` the DASH manifests are fetched to fill
        ``height``; DRM-protected manifests are dropped and, if nothing is
        left, a :class:`ThreeCatError` is raised (DRM is never bypassed).
        """
        data = self._media_json(video_id)
        info = data.get("informacio") or {}
        media = data.get("media") if isinstance(data.get("media"), dict) else {}
        estat = info.get("estat") if isinstance(info.get("estat"), dict) else {}
        active = bool(estat.get("actiu", True))

        sources = self._sources_from(media)
        audio_description: list[MediaSource] = []
        for variant in as_list(data.get("variants")):
            if isinstance(variant, dict) and str(variant.get("id", "")).upper() == "AUD":
                audio_description.extend(self._sources_from(variant.get("media")))

        subtitles: list[Subtitle] = []
        for sub in as_list(data.get("subtitols")):
            if not isinstance(sub, dict) or not sub.get("url"):
                continue
            subtitles.append(Subtitle(
                url=str(sub["url"]),
                lang=str(sub.get("iso") or "ca").lower(),
                label=clean_text(sub.get("text")) or str(sub.get("iso") or "ca"),
                format=str(sub.get("format") or "vtt").lower(),
            ))

        if parse_dash:
            had_sources = bool(sources)
            sources = self._apply_dash_info(sources)
            audio_description = self._apply_dash_info(audio_description)
            if had_sources and not sources:
                raise ThreeCatError(
                    "Aquest vídeo està protegit amb DRM i no es pot descarregar.")

        return MediaInfo(
            video_id=_to_int(info.get("id")) or int(video_id),
            title=clean_text(info.get("titol") or info.get("titol_complet")),
            available=active and bool(sources),
            geo=str(media.get("geo") or "").strip().upper(),
            sources=sources,
            subtitles=subtitles,
            audio_description=audio_description,
        )

    def _apply_dash_info(self, sources: list[MediaSource]) -> list[MediaSource]:
        kept: list[MediaSource] = []
        for source in sources:
            if source.kind == "dash":
                try:
                    dash = self.dash_info(source.url)
                except ThreeCatError:
                    kept.append(source)          # unknown; let the downloader try
                    continue
                if dash.protected:
                    continue
                source.height = dash.max_height
            kept.append(source)
        return kept

    def get_episode(self, video_id: int) -> Episode:
        """Build an :class:`Episode` for a single video from media.jsp.

        For videos without a programme (films) ``program_id`` is 0 and
        ``program_title`` is the video's own title.
        """
        data = self._media_json(video_id)
        info = data.get("informacio") or {}
        raw_title = clean_text(info.get("titol") or info.get("titol_complet"))
        title, t_season, t_episode = clean_episode_title(raw_title)

        season = info.get("temporada") if isinstance(info.get("temporada"), dict) else {}
        season_id = str(season.get("idName") or season.get("id_name") or "").strip()
        season_desc = clean_text(season.get("name") or season.get("desc"))
        season_number = parse_season_number(season_id, season_desc)
        if season_id and season_desc:
            with self._lock:
                self._season_titles[season_id] = season_desc
        if season_number is None and not season_id:
            season_number = t_season
        if not season_id and season_number is not None:
            season_id = f"PUTEMP_{season_number}"
        has_marker = bool(season_id) or t_season is not None

        program_id = _positive(info.get("programa_id")) or 0
        programa = clean_text(info.get("programa"))
        generic = normalize_for_search(programa) in _GENERIC_PROGRAMME_LABELS
        if program_id:
            program_title = self._canonical_title(program_id) or programa
        else:
            match = self._match_programme(programa) if programa else None
            if match is not None:
                program_id, program_title = match.id, match.title
            elif programa and has_marker and not generic:
                program_title = programa
            else:
                program_title = ""              # a film: named after itself
        if not program_title:
            program_title = title or raw_title
        if program_id and season_number is None:
            with self._lock:
                cached = self._programs.get(program_id)
                seasonal = self._seasonal.get(program_id)
            if seasonal or (seasonal is None and cached and cached[2]):
                # Extra of a programme with seasons: same rule as list_episodes.
                season_number = 0

        return Episode(
            id=_to_int(info.get("id")) or int(video_id),
            program_id=program_id,
            program_title=program_title,
            title=title or raw_title,
            raw_title=raw_title,
            season_id=season_id,
            season_number=season_number,
            episode_number=_episode_number(t_season, t_episode, info.get("capitol_temporada"),
                                           has_marker),
            absolute_number=_positive(info.get("capitol")),
            air_date=parse_date(info.get("data_emissio")),
            duration_seconds=parse_duration(info.get("durada")),
            description=clean_text(info.get("descripcio")),
            image=pick_image(data.get("imatges")),
        )

    # ----------------------------------------------------------------- resolve
    def _resolve_video(self, video_id: int) -> dict:
        episode = self.get_episode(video_id)
        program: Program | None = None
        if episode.program_id:
            try:
                program = self.get_program(episode.program_id)
            except ThreeCatError:
                program = self._program_from_catalogue(episode.program_id)
        return {"type": "video", "episode": episode, "program": program}

    def resolve(self, text: str) -> dict:
        """Interpret user input (URL, id or free text).

        Returns one of::

            {"type": "program", "program": Program}
            {"type": "video", "episode": Episode, "program": Program | None}
            {"type": "search", "results": list[Program]}
        """
        text = (text or "").strip()
        if not text:
            return {"type": "search", "results": []}

        parsed = parse_url(text)
        if parsed is not None:
            if parsed.video_id is not None:
                return self._resolve_video(parsed.video_id)
            for slug in parsed.slugs:
                program = self.program_by_slug(slug)
                if program is not None:
                    return {"type": "program", "program": program}
            query = parsed.slugs[0].replace("-", " ") if parsed.slugs else ""
            return {"type": "search", "results": self.search(query) if query else []}

        if text.isdigit():
            number = int(text)
            try:
                return self._resolve_video(number)
            except ThreeCatNotFound:
                pass
            try:
                return {"type": "program", "program": self.get_program(number)}
            except ThreeCatNotFound:
                pass
            return {"type": "search", "results": self.search(text)}

        program = self.program_by_slug(text) if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", text) else None
        if program is not None:
            return {"type": "program", "program": program}
        return {"type": "search", "results": self.search(text)}
