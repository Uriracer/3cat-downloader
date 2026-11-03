"""Jellyfin/Kodi metadata next to the downloaded videos (``.nfo`` and images).

Programmes that are not on TheTVDB/TMDB (news, magazines, sport...) would show
up in Jellyfin as bare file names. With these files Jellyfin shows 3cat's
title, description, date and pictures::

    <Series>/tvshow.nfo                       series title and description
    <Series>/poster.jpg                       series picture
    <Series>/Season 01/<video>.nfo            episode title, numbers, date, description
    <Series>/Season 01/<video>-thumb.jpg      episode picture

Existing files are never overwritten (the user, Jellyfin or another tool may
have edited them). Jellyfin reads ``.nfo`` files with its default library
settings (metadata reader "Nfo"); what they don't say, it can still look up
online.
"""
from __future__ import annotations

import logging
import os
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Optional

from . import naming
from .models import Episode

log = logging.getLogger(__name__)

SERIES_NFO = "tvshow.nfo"
SERIES_POSTER = "poster.jpg"
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# Two episodes of a series can finish at once and both write tvshow.nfo/poster.jpg.
_write_lock = threading.Lock()

FetchImage = Callable[[str], bytes]


def _default_fetch_image(url: str) -> bytes:
    import httpx   # lazy: only needed when there is a picture

    with httpx.stream("GET", url, timeout=20.0, follow_redirects=True) as resp:
        resp.raise_for_status()
        data = b""
        for chunk in resp.iter_bytes():
            data += chunk
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("image too large")
    return data


def _xml(root: ET.Element) -> bytes:
    ET.indent(root)
    body = ET.tostring(root, encoding="unicode").encode("utf-8")
    return b'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + body + b"\n"


def _add(parent: ET.Element, tag: str, value: Any, **attrs: str) -> None:
    if value is None or value == "":
        return
    ET.SubElement(parent, tag, attrs).text = str(value)


def episode_nfo(ep: Episode) -> bytes:
    """``<episodedetails>`` with the same numbers as the file name (see naming.py)."""
    values = naming.episode_values(ep)
    root = ET.Element("episodedetails")
    _add(root, "title", values["title"])
    _add(root, "showtitle", values["series"])
    if ep.season_number == 0 or values["season"] is None or values["episode"] is None:
        _add(root, "season", 0)      # Specials (see naming.py)
    else:
        _add(root, "season", values["season"])
        _add(root, "episode", values["episode"])
    _add(root, "aired", ep.air_date)
    _add(root, "plot", ep.description.strip())
    if ep.duration_seconds:
        _add(root, "runtime", max(1, round(ep.duration_seconds / 60)))
    _add(root, "uniqueid", ep.id, type="3cat", default="true")
    _add(root, "studio", "3Cat")
    return _xml(root)


def series_nfo(title: str, description: str = "", program_id: Optional[int] = None) -> bytes:
    root = ET.Element("tvshow")
    _add(root, "title", title)
    _add(root, "plot", description.strip())
    if program_id:
        _add(root, "uniqueid", program_id, type="3cat", default="true")
    _add(root, "studio", "3Cat")
    return _xml(root)


def _write_new(path: Path, data: bytes) -> bool:
    """Write ``path`` only if it does not exist yet (atomic)."""
    with _write_lock:
        if path.exists():
            return False
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    return True


def write_episode_metadata(video_path: str, ep: Episode, *, series_dir: Optional[str] = None,
                           program: Any = None, fetch_image: Optional[FetchImage] = None) -> list[str]:
    """Write the ``.nfo`` and pictures of ``ep`` (and of its series, once). Returns the files written.

    ``series_dir`` is the series folder (None when the file name template has
    no such folder); ``program`` is the 3cat programme (title, description,
    image), used for ``tvshow.nfo`` and ``poster.jpg``. Missing pictures or
    network errors are logged and skipped: the video is what matters.
    """
    fetch = fetch_image or _default_fetch_image
    video = Path(video_path)
    written: list[str] = []

    def picture(url: Optional[str], target: Path) -> None:
        if not url or not url.startswith(("http://", "https://")) or target.exists():
            return
        try:
            if _write_new(target, fetch(url)):
                written.append(str(target))
        except Exception as exc:
            log.warning("No s'ha pogut desar la imatge %s: %s", target.name, exc)

    nfo = video.with_suffix(".nfo")
    if _write_new(nfo, episode_nfo(ep)):
        written.append(str(nfo))
    picture(ep.image, video.with_name(f"{video.stem}-thumb.jpg"))

    if series_dir:
        folder = Path(series_dir)
        title = (getattr(program, "title", "") or ep.program_title or "").strip()
        if title and _write_new(folder / SERIES_NFO, series_nfo(
                title, getattr(program, "description", "") or "", ep.program_id or None)):
            written.append(str(folder / SERIES_NFO))
        picture(getattr(program, "image", None), folder / SERIES_POSTER)
    return written
