"""Runtime configuration derived from the command line and environment.

Two modes (see docs/dev/CONTRACT-SERVER.md):

* **desktop** — bound to a loopback address: no login, strict Host/Origin
  checks, "open folder" allowed. This is the Windows app.
* **server** — bound to a non-loopback address (Docker, Raspberry Pi,
  Proxmox…): login required, any Host accepted unless restricted, CSRF
  protection by same-origin checks, "open folder" disabled.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

ENV_HOST = "TRESDL_HOST"
ENV_PORT = "TRESDL_PORT"
ENV_AUTH = "TRESDL_AUTH"
ENV_PASSWORD = "TRESDL_PASSWORD"
ENV_API_KEY = "TRESDL_API_KEY"
ENV_DOWNLOAD_DIR = "TRESDL_DOWNLOAD_DIR"
ENV_ALLOWED_HOSTS = "TRESDL_ALLOWED_HOSTS"
ENV_TRUST_PROXY = "TRESDL_TRUST_PROXY"
ENV_FFMPEG = "TRESDL_FFMPEG"
ENV_LOG_LEVEL = "TRESDL_LOG_LEVEL"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOOPBACK_NAMES: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: Settings fields that an environment variable can force (field -> variable).
LOCKABLE_FIELDS: dict[str, str] = {
    "download_dir": ENV_DOWNLOAD_DIR,
    "api_key": ENV_API_KEY,
}


def env(name: str) -> Optional[str]:
    """Environment value with surrounding whitespace removed; None if empty."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def env_flag(name: str) -> bool:
    return (env(name) or "").lower() in ("1", "true", "yes", "on")


def is_loopback(host: str) -> bool:
    """True for 127.0.0.0/8, ::1 and "localhost"."""
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class RuntimeConfig:
    """How this process serves the UI/API (immutable once the server starts)."""
    mode: str = "desktop"                                # "desktop" | "server"
    auth_required: bool = False
    #: Host names accepted in the Host header; None = any host.
    allowed_hosts: Optional[frozenset[str]] = LOOPBACK_NAMES
    password_env: Optional[str] = field(default=None, repr=False)
    trust_proxy: bool = False

    @property
    def server(self) -> bool:
        return self.mode == "server"


def build_runtime(host: str, *, no_gui: bool = False) -> RuntimeConfig:
    """Derive the runtime config from the bind host and environment."""
    auth_setting = (env(ENV_AUTH) or "auto").lower()
    if auth_setting not in ("auto", "required", "none"):
        log.warning("%s=%r no és vàlid (auto|required|none); s'utilitza 'auto'.", ENV_AUTH, auth_setting)
        auth_setting = "auto"

    server = not is_loopback(host) or (no_gui and auth_setting == "required")
    if not server:
        return RuntimeConfig(mode="desktop", auth_required=auth_setting == "required",
                             allowed_hosts=LOOPBACK_NAMES, password_env=env(ENV_PASSWORD))

    allowed: Optional[frozenset[str]] = None
    raw_hosts = env(ENV_ALLOWED_HOSTS)
    if raw_hosts and raw_hosts != "*":
        names = {h.strip().lower() for h in raw_hosts.split(",") if h.strip()}
        if "*" not in names:
            allowed = frozenset(names | LOOPBACK_NAMES)
    auth_required = auth_setting != "none"
    if not auth_required:
        log.warning("ATENCIÓ: %s=none — qualsevol persona de la xarxa pot fer servir l'aplicació.", ENV_AUTH)
    return RuntimeConfig(mode="server", auth_required=auth_required, allowed_hosts=allowed,
                         password_env=env(ENV_PASSWORD), trust_proxy=env_flag(ENV_TRUST_PROXY))


# --------------------------------------------------------------------------- ffmpeg

_ffmpeg_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}


def _usable(path: Optional[str]) -> bool:
    return bool(path) and os.path.isfile(path)  # type: ignore[arg-type]


def find_ffmpeg(refresh: bool = False) -> tuple[Optional[str], Optional[str]]:
    """Locate ffmpeg: ``TRESDL_FFMPEG`` → bundled imageio-ffmpeg → ``PATH``.

    Returns ``(path, None)`` or ``(None, catalan_error)``. Cached.
    """
    if not refresh and "result" in _ffmpeg_cache:
        return _ffmpeg_cache["result"]
    result: tuple[Optional[str], Optional[str]]
    forced = env(ENV_FFMPEG)
    if forced:
        path = forced if os.path.isfile(forced) else shutil.which(forced)
        result = (path, None) if path else (
            None, f"No s'ha trobat ffmpeg a «{forced}» (variable {ENV_FFMPEG}).")
    else:
        path = None
        try:
            import imageio_ffmpeg

            candidate = imageio_ffmpeg.get_ffmpeg_exe()
            if _usable(candidate):
                path = candidate
        except Exception:  # not installed, or no binary for this platform
            path = None
        path = path or shutil.which("ffmpeg")
        result = (path, None) if path else (
            None, "No s'ha trobat ffmpeg. Instal·la'l (per exemple «sudo apt install ffmpeg») "
                  f"o indica'n la ruta amb la variable {ENV_FFMPEG}.")
    _ffmpeg_cache["result"] = result
    return result


def ffmpeg_exe() -> str:
    """Path to ffmpeg or raise RuntimeError with a Catalan message."""
    path, error = find_ffmpeg()
    if not path:
        raise RuntimeError(error or "No s'ha trobat ffmpeg.")
    return path
