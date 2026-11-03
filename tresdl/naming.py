"""Filename sanitising and Sonarr/Radarr-style output paths.

The default layout is importable by Sonarr::

    <root>/<Series>/Season 01/<Series> - S01E01 - <Title>.mp4

Templates use ``str.format`` syntax with these placeholders:

``{series}``   programme title
``{season}``   season number (int)
``{episode}``  episode number inside the season (int)
``{absolute}`` absolute episode number (int)
``{title}``    clean episode title
``{date}``     air date ``YYYY-MM-DD``
``{id}``       3cat video id (int)

``/`` (or ``\\``) in the template separates folders. Every rendered path
component is sanitised individually, and the final path is guaranteed to stay
inside ``root``.

Fallbacks (a filename never contains ``None``):

* no season but some number  -> season 1, episode = absolute number (or the
  in-season number when there is no absolute number);
* season 0 (an extra/special of a programme that *has* seasons, see
  ``ThreeCatClient.list_episodes``) -> the Specials layout below, so it never
  lands on top of a real ``Season 01`` episode;
* season but no in-season number -> episode = absolute number;
* no number at all while the template needs one ->
  ``<Series>/Specials/<Series> - <YYYY-MM-DD> - <Title>`` (the video id is
  used instead of the date when the air date is unknown);
* ``{date}`` without an air date -> the video id;
* invalid template -> the default template.

Two different videos must never share an output path: callers that detect a
clash use :func:`disambiguate` (``<stem> [<video id>].<ext>``), which is
deterministic so ``skip_existing`` keeps working across runs.
"""
from __future__ import annotations

import logging
import os
import re
import string
import unicodedata
from pathlib import Path
from typing import Any

from .models import Episode

log = logging.getLogger(__name__)

DEFAULT_TEMPLATE = "{series}/Season {season:02d}/{series} - S{season:02d}E{episode:02d} - {title}"
SPECIALS_TEMPLATE = "{series}/Specials/{series} - {date} - {title}"
PLACEHOLDERS = ("series", "season", "episode", "absolute", "title", "date", "id")
#: A template must use at least one of these, or every episode of a season
#: (or of a whole series) would render to the same file name.
IDENTIFYING_PLACEHOLDERS = ("episode", "absolute", "title", "id")
MAX_COMPONENT_LENGTH = 150
AUDIO_DESCRIPTION_SUFFIX = " (Audiodescripció)"

_NUMERIC_FIELDS = ("season", "episode", "absolute")
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_COLON = re.compile(r"\s*:\s*")
_WHITESPACE = re.compile(r"\s+")
_FORMATTER = string.Formatter()


def sanitize(name: str) -> str:
    """Return ``name`` as a single Windows-safe path component.

    Strips/replaces ``<>:"/\\|?*`` and control characters, collapses
    whitespace, removes leading/trailing spaces and trailing dots, avoids
    reserved device names (``CON``, ``NUL``, ``COM1``...) and limits the result
    to :data:`MAX_COMPONENT_LENGTH` characters. Never returns an empty string.
    """
    text = unicodedata.normalize("NFC", str(name))
    text = _CONTROL_CHARS.sub("", _WHITESPACE.sub(" ", text))
    text = _COLON.sub(" - ", text)
    text = text.replace('"', "'")
    for ch in "/\\|":
        text = text.replace(ch, "-")
    for ch in "<>?*":
        text = text.replace(ch, "")
    text = _WHITESPACE.sub(" ", text).strip()
    text = text[:MAX_COMPONENT_LENGTH]
    text = text.rstrip(". ").strip()
    if not text:
        return "_"
    if text.split(".")[0].strip().upper() in _RESERVED_NAMES:
        text = "_" + text
    return text


def validate_template(template: str) -> None:
    """Raise ``ValueError`` (Catalan message) if ``template`` cannot be rendered."""
    if not isinstance(template, str) or not template.strip():
        raise ValueError("La plantilla de nom de fitxer és buida.")
    sample = Episode(
        id=1, program_id=1, program_title="Sèrie", title="Títol", raw_title="T1xC1 - Títol",
        season_number=1, episode_number=1, absolute_number=1, air_date="2020-01-01",
    )
    try:
        fields = _template_fields(template)
        unknown = sorted(fields - set(PLACEHOLDERS))
        if unknown:
            raise ValueError(
                "Camps desconeguts a la plantilla: " + ", ".join("{" + f + "}" for f in unknown)
            )
        if not fields & set(IDENTIFYING_PLACEHOLDERS):
            raise ValueError(
                "La plantilla ha d'identificar cada capítol: inclou {episode}, "
                "{absolute}, {title} o {id}."
            )
        _render(template, _values(sample))
    except ValueError:
        raise
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ValueError(f"La plantilla de nom de fitxer no és vàlida: {exc}") from exc


def episode_path(root: str, ep: Episode, ext: str = "mp4", template: str | None = None) -> str:
    """Return the absolute output path for ``ep`` below ``root``.

    ``ext`` is appended without a leading dot (pass ``""`` for no extension).
    Raises ``ValueError`` only if the result would escape ``root`` (which
    sanitising should already make impossible).
    """
    template = template or DEFAULT_TEMPLATE
    values = _values(ep)
    try:
        components = _render(_choose_template(template, values), values)
    except (KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
        log.warning("Invalid filename template %r (%s); using the default one", template, exc)
        components = _render(_choose_template(DEFAULT_TEMPLATE, values), values)

    root_path = Path(os.path.abspath(os.path.expanduser(root)))
    ext = ext.lstrip(".")
    if ext:
        components[-1] = f"{components[-1]}.{sanitize(ext)}"
    final = Path(os.path.abspath(root_path.joinpath(*components)))
    if os.path.commonpath([str(root_path), str(final)]) != str(root_path) or final == root_path:
        raise ValueError("El camí de sortida queda fora de la carpeta de descàrregues.")
    return str(final)


def series_dir(root: str, ep: Episode, template: str | None = None) -> str | None:
    """The series folder of ``ep`` (``<root>/<Series>``), or None when the
    template does not start with a ``{series}`` folder."""
    parts = _split_template(template or DEFAULT_TEMPLATE)
    if len(parts) < 2 or parts[0].strip() != "{series}":
        return None
    root_path = Path(os.path.abspath(os.path.expanduser(root)))
    return str(root_path / _render("{series}", _values(ep))[0])


def episode_values(ep: Episode) -> dict[str, Any]:
    """Placeholder values of ``ep`` (numbers with the fallbacks above), unsanitised."""
    return _values(ep)


def disambiguate(path: str, video_id: int) -> str:
    """``<stem> [<video_id>]<ext>``: a unique variant of ``path`` for one video.

    Used when two different videos would otherwise render to the same file
    (language versions, re-uploads, alternative cuts with identical numbers
    and titles). Idempotent.
    """
    p = Path(path)
    tag = f" [{int(video_id)}]"
    if p.stem.endswith(tag):
        return str(p)
    stem = p.stem[: max(1, MAX_COMPONENT_LENGTH - len(tag))].rstrip(". ")
    return str(p.with_name(f"{stem}{tag}{p.suffix}"))


def subtitle_path(video_path: str, lang: str, ext: str = "vtt") -> str:
    """``<stem>.<lang>.<ext>`` next to ``video_path``."""
    p = Path(video_path)
    return str(p.with_name(f"{p.stem}.{sanitize(lang or 'und')}.{ext}"))


def audio_description_path(video_path: str) -> str:
    """``<stem> (Audiodescripció).<ext>`` next to ``video_path``."""
    p = Path(video_path)
    return str(p.with_name(f"{p.stem}{AUDIO_DESCRIPTION_SUFFIX}{p.suffix}"))


# --------------------------------------------------------------------------- helpers

def _template_fields(template: str) -> set[str]:
    """Top-level field names used by ``template``."""
    names: set[str] = set()
    for _literal, field_name, _spec, _conv in _FORMATTER.parse(template):
        if field_name is not None:
            names.add(re.split(r"[.\[]", field_name, maxsplit=1)[0])
    return names


def _values(ep: Episode) -> dict[str, Any]:
    """Raw placeholder values for ``ep`` with the number fallbacks applied."""
    season = ep.season_number
    episode = ep.episode_number
    absolute = ep.absolute_number
    if season == 0:
        # Extra/special of a programme with seasons: never numbered as an
        # episode (that would collide with real episodes); see _choose_template.
        episode = None
    elif season is None and (episode is not None or absolute is not None):
        season = 1
        episode = absolute if absolute is not None else episode
    elif episode is None:
        episode = absolute
    if absolute is None and season != 0:
        absolute = episode
    title = (ep.title or ep.raw_title or "").strip() or f"Vídeo {ep.id}"
    return {
        "series": (ep.program_title or "").strip() or "3Cat",
        "season": season,
        "episode": episode,
        "absolute": absolute,
        "title": title,
        "date": ep.air_date or str(ep.id),
        "id": ep.id,
    }


def _choose_template(template: str, values: dict[str, Any]) -> str:
    """Use the Specials layout when a needed number is unknown."""
    needed = _template_fields(template) & set(_NUMERIC_FIELDS)
    if any(values[name] is None for name in needed):
        return SPECIALS_TEMPLATE
    return template


def _split_template(template: str) -> list[str]:
    """Split on ``/`` and ``\\`` that are outside ``{...}`` fields."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in template:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch in "/\\" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


class _Missing:
    """Placeholder value that refuses to be rendered (so "None" never leaks)."""

    def __format__(self, spec: str) -> str:
        raise ValueError("unresolved placeholder")

    def __str__(self) -> str:
        raise ValueError("unresolved placeholder")

    __repr__ = __str__


_MISSING = _Missing()


def _render(template: str, values: dict[str, Any]) -> list[str]:
    """Render ``template`` into sanitised, non-empty path components."""
    safe = {
        k: (_MISSING if v is None else sanitize(v) if isinstance(v, str) else v)
        for k, v in values.items()
    }
    components: list[str] = []
    for part in _split_template(template):
        if not part.strip():
            continue
        component = sanitize(part.format_map(safe))
        if component in {".", ".."}:
            component = "_"
        components.append(component)
    if not components:
        raise ValueError("empty template")
    return components
