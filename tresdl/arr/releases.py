"""Releases for Sonarr/Radarr: names, size estimates, tokens and pseudo-NZBs.

A *release* is what our Newznab indexer offers: one episode, a season pack or
a film. Sonarr/Radarr pick a release by its **name** (they parse series title,
SxxEyy / date, resolution and source from it), download its ``.nzb`` and hand
that file to our SABnzbd-compatible endpoint. The NZB only carries a
**token** describing what to download; it never references Usenet.
"""
from __future__ import annotations

import base64
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from xml.sax.saxutils import escape

GROUP = "3CAT"
LANGUAGE_TAG = "CATALAN"
NZB_NS = "http://www.newzbin.com/DTD/2003/nzb"
TOKEN_META = "x-3cat-token"

#: Newznab categories.
CAT_TV, CAT_TV_SD, CAT_TV_HD = 5000, 5030, 5040
CAT_MOVIES, CAT_MOVIES_SD, CAT_MOVIES_HD = 2000, 2030, 2040

#: Rough bit rates (bytes per second) used to estimate sizes: Sonarr/Radarr
#: reject 0-byte releases and compare the size with their quality limits.
_BYTES_PER_SECOND = {1080: 4_200_000 // 8, 720: 2_200_000 // 8, 576: 1_600_000 // 8, 480: 1_200_000 // 8}
_DEFAULT_DURATION = 45 * 60


def ascii_fold(text: str) -> str:
    """"Merlí · l'Estiu" -> "Merli l'Estiu" (accents and l·l removed)."""
    text = (text or "").replace("l·l", "ll").replace("L·L", "LL").replace("·", "")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def dotted(text: str, max_len: int = 80) -> str:
    """Scene-style name part: ASCII, words joined with dots."""
    words = re.findall(r"[A-Za-z0-9]+", ascii_fold(text).replace("'", ""))
    out = ".".join(words)
    if len(out) > max_len:
        out = out[:max_len].rsplit(".", 1)[0] or out[:max_len]
    return out


def resolution_tag(height: Optional[int]) -> str:
    if not height:
        return "720p"
    for limit, tag in ((1000, "1080p"), (700, "720p"), (560, "576p")):
        if height >= limit:
            return tag
    return "480p"


def bucket(height: Optional[int]) -> int:
    tag = resolution_tag(height)
    return int(tag[:-1])


def estimate_size(duration_seconds: Optional[int], height: Optional[int]) -> int:
    seconds = duration_seconds if duration_seconds and duration_seconds > 0 else _DEFAULT_DURATION
    return int(seconds * _BYTES_PER_SECOND[bucket(height)])


def _quality_suffix(height: Optional[int]) -> str:
    return f"{LANGUAGE_TAG}.{resolution_tag(height)}.WEB-DL.AAC.H264-{GROUP}"


def episode_name(series: str, *, season: Optional[int], episode: Optional[int],
                 title: str = "", air_date: Optional[str] = None, height: Optional[int] = None) -> str:
    """``Merli.S01E01.Els.Peripatetics.CATALAN.1080p.WEB-DL.AAC.H264-3CAT``.

    Without episode numbers but with a date it uses Sonarr's daily format
    (``Show.2026.11.02.Title…``).
    """
    parts = [dotted(series) or "3Cat"]
    if season is not None and episode is not None:
        parts.append(f"S{season:02d}E{episode:02d}")
    elif air_date:
        parts.append(air_date.replace("-", "."))
    ep_title = dotted(title, 60)
    if ep_title and not re.fullmatch(r"Capitol\.\d+", ep_title):
        parts.append(ep_title)
    parts.append(_quality_suffix(height))
    return ".".join(parts)


def season_pack_name(series: str, season: int, height: Optional[int]) -> str:
    return f"{dotted(series) or '3Cat'}.S{season:02d}.{_quality_suffix(height)}"


def movie_name(title: str, year: Optional[int], height: Optional[int]) -> str:
    parts = [dotted(title) or "3Cat"]
    if year:
        parts.append(str(year))
    parts.append(_quality_suffix(height))
    return ".".join(parts)


# --------------------------------------------------------------------------- tokens

@dataclass
class Token:
    """What a release downloads. Encoded into the NZB and the download URL."""
    kind: str                         # "e" episode(s) | "m" movie
    title: str                        # release name (also the job folder name)
    videos: list[int] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)   # video id -> file name (season packs)
    category: str = "tv"

    def encode(self) -> str:
        data = {"k": self.kind, "t": self.title, "v": self.videos, "c": self.category}
        if self.names:
            data["n"] = self.names
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, text: str) -> "Token":
        try:
            raw = base64.urlsafe_b64decode(text.strip() + "=" * (-len(text.strip()) % 4))
            data: Any = json.loads(raw.decode("utf-8"))
            videos = [int(v) for v in data["v"]]
            if data["k"] not in ("e", "m") or not videos or not str(data["t"]).strip():
                raise ValueError("bad token")
            names = {str(k): str(v) for k, v in (data.get("n") or {}).items()}
            return cls(kind=data["k"], title=str(data["t"]), videos=videos, names=names,
                       category=str(data.get("c") or "tv"))
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Testimoni de llançament no vàlid.") from exc


# --------------------------------------------------------------------------- NZB

def build_nzb(token: Token) -> bytes:
    """A syntactically valid NZB (root ``nzb`` + one ``file``) carrying the token."""
    encoded = token.encode()
    title = escape(token.title)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n'
        f'<nzb xmlns="{NZB_NS}">\n'
        f'  <head>\n    <meta type="name">{title}</meta>\n'
        f'    <meta type="{TOKEN_META}">{encoded}</meta>\n  </head>\n'
        f'  <file poster="3Cat Downloader" date="0" subject="{title} [3cat]">\n'
        '    <groups><group>alt.binaries.3cat</group></groups>\n'
        f'    <segments><segment bytes="1" number="1">{encoded[:200]}@3cat.invalid</segment></segments>\n'
        '  </file>\n</nzb>\n'
    )
    return xml.encode("utf-8")


def token_from_nzb(data: bytes) -> Token:
    """Read the token back from an NZB produced by :func:`build_nzb`."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("El fitxer NZB no és vàlid.") from exc
    for meta in root.iter():
        if meta.tag.endswith("meta") and meta.get("type") == TOKEN_META and meta.text:
            return Token.decode(meta.text)
    raise ValueError("Aquest NZB no és de 3Cat Downloader.")


# --------------------------------------------------------------------------- RSS

@dataclass
class Release:
    title: str
    token: Token
    size: int
    category: int
    published: Optional[str] = None       # ISO date "YYYY-MM-DD"
    season: Optional[int] = None
    episode: Optional[int] = None
    description: str = ""
    comments: str = ""
    guid: str = ""


def _rfc822(iso_date: Optional[str]) -> str:
    try:
        moment = datetime.strptime(iso_date or "", "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
    except ValueError:
        moment = datetime(2000, 1, 1, tzinfo=timezone.utc)
    return moment.strftime("%a, %d %b %Y %H:%M:%S +0000")


def rss_feed(releases: list[Release], *, base_url: str, api_key: str,
             offset: int = 0, total: Optional[int] = None) -> str:
    """Newznab RSS 2.0 feed for ``releases``."""
    base = base_url.rstrip("/")
    items = []
    for rel in releases:
        link = f"{base}/arr/nzb/{rel.token.encode()}.nzb?apikey={api_key}"
        attrs = [("category", str(rel.category)), ("size", str(rel.size))]
        if rel.season is not None:
            attrs.append(("season", str(rel.season)))
        if rel.episode is not None:
            attrs.append(("episode", str(rel.episode)))
        attrs.append(("language", "Catalan"))
        attr_xml = "".join(f'<newznab:attr name="{n}" value="{escape(v)}"/>' for n, v in attrs)
        items.append(
            "<item>"
            f"<title>{escape(rel.title)}</title>"
            f'<guid isPermaLink="false">{escape(rel.guid or rel.token.encode()[:60])}</guid>'
            f"<link>{escape(link)}</link>"
            + (f"<comments>{escape(rel.comments)}</comments>" if rel.comments else "")
            + f"<pubDate>{_rfc822(rel.published)}</pubDate>"
            f"<category>{rel.category}</category>"
            f"<description>{escape(rel.description or rel.title)}</description>"
            f'<enclosure url="{escape(link)}" length="{rel.size}" type="application/x-nzb"/>'
            f"{attr_xml}</item>"
        )
    count = len(releases) if total is None else total
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        "<channel><title>3Cat Downloader</title>"
        "<description>Sèries i pel·lícules de 3cat</description>"
        f"<link>{escape(base)}/</link><language>ca</language>"
        f'<newznab:response offset="{offset}" total="{count}"/>'
        + "".join(items)
        + "</channel></rss>"
    )


def caps_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<caps>"
        '<server version="1.0" title="3Cat Downloader" strapline="Sèries i pel·lícules de 3cat"/>'
        '<limits max="200" default="100"/>'
        '<registration available="no" open="no"/>'
        "<searching>"
        '<search available="yes" supportedParams="q"/>'
        '<tv-search available="yes" supportedParams="q,season,ep"/>'
        '<movie-search available="yes" supportedParams="q"/>'
        "</searching>"
        "<categories>"
        f'<category id="{CAT_MOVIES}" name="Movies">'
        f'<subcat id="{CAT_MOVIES_SD}" name="SD"/><subcat id="{CAT_MOVIES_HD}" name="HD"/></category>'
        f'<category id="{CAT_TV}" name="TV">'
        f'<subcat id="{CAT_TV_SD}" name="SD"/><subcat id="{CAT_TV_HD}" name="HD"/></category>'
        "</categories></caps>"
    )


def error_xml(code: int, description: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><error code="{code}" description="{escape(description)}"/>'
