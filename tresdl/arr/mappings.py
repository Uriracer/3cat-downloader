"""Manual correspondences: a 3cat programme or film -> its TheTVDB or TMDB id.

The automatic lookups (``lookup.py``) only work when TVmaze or TMDB know the
Catalan name. When they don't ("El xai Shaun" is "Shaun the Sheep" and TVmaze
has no Catalan aka), the user says which series or film it is, and the
indexer uses that before (and in addition to) the automatic names.

Stored in ``arr_mappings.json`` in the data directory::

    {"version": 1, "mappings": [{"kind": "series", "title": "El xai Shaun",
                                 "remote_id": 81838, "remote_title": "Shaun the Sheep"}]}

One 3cat name maps to one id; several 3cat names may map to the same id (a
series split in several 3cat programmes).
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from ..config import atomic_write_json
from .releases import normalize

log = logging.getLogger(__name__)

MAPPINGS_FILE = "arr_mappings.json"
MAPPINGS_VERSION = 1
KINDS = ("series", "movie")      # TheTVDB id / TMDB id


@dataclass
class Mapping:
    kind: str                # "series" | "movie"
    title: str               # name on 3cat: "El xai Shaun"
    remote_id: int           # TheTVDB id (series) or TMDB id (movie)
    remote_title: str = ""   # Sonarr/Radarr's title, only shown to the user

    def to_dict(self) -> dict:
        return asdict(self)


class MappingStore:
    """Thread-safe list of manual correspondences, persisted as JSON."""

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self._path = Path(data_dir) / MAPPINGS_FILE if data_dir else None
        self._lock = threading.Lock()
        self._items: list[Mapping] = self._load()

    def _load(self) -> list[Mapping]:
        if not self._path or not self._path.is_file():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            items = []
            for raw in data.get("mappings", []):
                if raw.get("kind") in KINDS and str(raw.get("title") or "").strip() and int(raw["remote_id"]) > 0:
                    items.append(Mapping(raw["kind"], str(raw["title"]).strip(), int(raw["remote_id"]),
                                         str(raw.get("remote_title") or "")))
            return items
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            log.warning("No s'ha pogut llegir %s: %s", self._path, exc)
            return []

    def _save(self) -> None:
        if self._path:
            atomic_write_json(self._path, {"version": MAPPINGS_VERSION,
                                           "mappings": [m.to_dict() for m in self._items]})

    def all(self) -> list[Mapping]:
        with self._lock:
            return sorted(self._items, key=lambda m: (m.kind, normalize(m.title)))

    def add(self, mapping: Mapping) -> Mapping:
        """Add or replace the correspondence of ``mapping.title``."""
        if mapping.kind not in KINDS:
            raise ValueError(mapping.kind)
        key = normalize(mapping.title)
        with self._lock:
            self._items = [m for m in self._items if not (m.kind == mapping.kind and normalize(m.title) == key)]
            self._items.append(mapping)
            self._save()
        return mapping

    def remove(self, kind: str, title: str) -> bool:
        key = normalize(title)
        with self._lock:
            kept = [m for m in self._items if not (m.kind == kind and normalize(m.title) == key)]
            if len(kept) == len(self._items):
                return False
            self._items = kept
            self._save()
        return True

    def remote_id(self, kind: str, title: str) -> Optional[int]:
        """TheTVDB/TMDB id given to the 3cat name ``title``, if any."""
        key = normalize(title)
        with self._lock:
            return next((m.remote_id for m in self._items if m.kind == kind and normalize(m.title) == key), None)

    def titles(self, kind: str, remote_id: Optional[int]) -> list[str]:
        """3cat names given to a TheTVDB/TMDB id."""
        if not remote_id:
            return []
        with self._lock:
            return [m.title for m in self._items if m.kind == kind and m.remote_id == remote_id]
