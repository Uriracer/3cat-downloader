"""User settings: load/save as JSON in the per-user application data folder.

* ``app_data_dir()`` -> ``%APPDATA%/3CatDownloader`` on Windows, otherwise
  ``~/.3catdownloader``. The ``TRESDL_DATA_DIR`` environment variable overrides
  both (useful for tests and portable installs).
* Loading is forward compatible: unknown keys are ignored, missing or invalid
  values fall back to their defaults.
* Writes are atomic (temporary file + ``os.replace``).
* ``api_key`` (for the future Sonarr/Radarr integration) is generated with
  ``secrets.token_hex(16)`` on first run and persisted.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

from .naming import DEFAULT_TEMPLATE, validate_template

log = logging.getLogger(__name__)

APP_DIR_NAME = "3CatDownloader"
FALLBACK_DIR_NAME = ".3catdownloader"
SETTINGS_FILE = "settings.json"
DATA_DIR_ENV = "TRESDL_DATA_DIR"
QUALITIES = ("best", "1080", "720", "576", "480", "mp4")
MAX_PARALLEL_LIMIT = 8

_save_lock = threading.Lock()


def _new_api_key() -> str:
    return secrets.token_hex(16)


@dataclass
class Settings:
    """Persisted user preferences (field names are part of the REST API)."""
    download_dir: str = str(Path.home() / "Videos" / "3Cat")
    max_parallel: int = 2
    quality: str = "best"            # best|1080|720|576|480|mp4
    subtitles: bool = True
    embed_subtitles: bool = False
    audio_description: bool = False
    skip_existing: bool = True
    filename_template: str = DEFAULT_TEMPLATE
    server_port: int = 8765
    api_key: str = field(default_factory=_new_api_key)  # for future *arr integration
    #: Login password for server mode (see tresdl/auth.py); never sent to clients.
    password_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "Settings":
        """Build validated settings from untrusted data.

        Unknown keys are ignored; missing or invalid values use the defaults.
        """
        defaults = cls()
        values: dict[str, Any] = {}
        data = data if isinstance(data, Mapping) else {}
        for f in fields(cls):
            default = getattr(defaults, f.name)
            values[f.name] = _coerce(f.name, data.get(f.name, default), default)
        return cls(**values)

    def updated(self, changes: Mapping[str, Any]) -> "Settings":
        """Return a validated copy with ``changes`` applied (partial update)."""
        merged = self.to_dict()
        merged.update({k: v for k, v in dict(changes).items() if k in merged})
        if not merged.get("api_key"):
            merged["api_key"] = self.api_key
        return Settings.from_dict(merged)


def _coerce(name: str, value: Any, default: Any) -> Any:
    """Validate a single setting value, returning ``default`` if unusable."""
    try:
        if name in ("max_parallel", "server_port"):
            if isinstance(value, bool):
                return default
            number = int(value)
            if name == "max_parallel":
                return min(max(number, 1), MAX_PARALLEL_LIMIT)
            return number if 1024 <= number <= 65535 else default
        if isinstance(default, bool):
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
                return value.strip().lower() in ("true", "1")
            if isinstance(value, int):
                return bool(value)
            return default
        if name == "quality":
            q = str(value).strip().lower().removesuffix("p")
            return q if q in QUALITIES else default
        if name == "filename_template":
            if not isinstance(value, str):
                return default
            validate_template(value)
            return value
        if name == "api_key":
            return value.strip() if isinstance(value, str) and value.strip() else _new_api_key()
        if name == "password_hash":
            return value if isinstance(value, str) and value.startswith("pbkdf2_sha256$") else ""
        if name == "download_dir":
            return value.strip() if isinstance(value, str) and value.strip() else default
        return value if isinstance(value, type(default)) else default
    except (TypeError, ValueError) as exc:
        log.warning("Ignoring invalid setting %s=%r (%s)", name, value, exc)
        return default


def app_data_dir() -> Path:
    """Per-user data folder (created if needed)."""
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        path = Path(override)
    elif os.environ.get("APPDATA"):
        path = Path(os.environ["APPDATA"]) / APP_DIR_NAME
    else:
        path = Path.home() / FALLBACK_DIR_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Cannot create data dir %s: %s", path, exc)
    return path


def settings_path() -> Path:
    return app_data_dir() / SETTINGS_FILE


def atomic_write_json(path: Path | str, data: Any, *, compact: bool = False) -> None:
    """Write ``data`` as UTF-8 JSON to ``path`` atomically.

    ``compact`` drops indentation (much faster for large files such as the queue).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if compact:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: target briefly locked (antivirus, indexer...).
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_settings(path: Path | str | None = None) -> Settings:
    """Load settings, creating/repairing the file when needed.

    A corrupt file is kept as ``settings.json.bak`` and defaults are used.
    The file is (re)written when it is missing, lacks the API key or any key.
    """
    path = Path(path) if path is not None else settings_path()
    raw: dict[str, Any] | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        raw = loaded if isinstance(loaded, dict) else None
        if raw is None:
            raise ValueError("settings root is not an object")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("Cannot read settings %s (%s); using defaults", path, exc)
        try:
            os.replace(path, path.with_name(path.name + ".bak"))
        except OSError:
            pass

    settings = Settings.from_dict(raw or {})
    needs_save = raw is None or any(f.name not in raw for f in fields(Settings)) or (
        raw.get("api_key") != settings.api_key
    )
    if needs_save:
        try:
            save_settings(settings, path)
        except OSError as exc:
            log.warning("Cannot save settings %s: %s", path, exc)
    return settings


def save_settings(s: Settings, path: Path | str | None = None) -> None:
    """Persist ``s`` atomically."""
    path = Path(path) if path is not None else settings_path()
    with _save_lock:
        atomic_write_json(path, s.to_dict())
