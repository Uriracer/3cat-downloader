"""3Cat Downloader command line entry point (``tresdl`` / ``3cat-downloader``).

Starts the HTTP server (FastAPI + uvicorn) in a daemon thread and, in desktop
mode, shows the UI in a pywebview window (or the default browser).

Usage::

    tresdl                          # desktop window (Windows app)
    tresdl --no-gui                 # local server only, no window
    tresdl --host 0.0.0.0           # server mode for the LAN (Docker, Raspberry Pi…)
    tresdl --port 9000 --data-dir /srv/3cat-config

Only one instance runs per data folder (see ``tresdl/instance.py``).
SIGTERM/SIGINT stop it gracefully: in-flight downloads go back to the queue
and resume on the next start.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import multiprocessing
import os
import signal
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional

from . import __version__

log = logging.getLogger("tresdl.run")

WINDOW_TITLE = "3Cat Downloader"
WINDOW_SIZE = (1200, 800)
WINDOW_MIN_SIZE = (900, 600)
HEALTH_TIMEOUT = 30.0
INSTANCE_WAIT = 15.0    # seconds to wait for an already running instance to answer
STOP_TIMEOUT = 8.0      # seconds the download manager gets to stop (docker stop grace is 10 s)

#: Set by SIGTERM/SIGINT (and window close) to stop the main loop.
_shutdown = threading.Event()


# --------------------------------------------------------------------------- #
# Process setup
# --------------------------------------------------------------------------- #
def _fix_std_streams() -> None:
    """A windowed (PyInstaller --windowed) exe has no stdout/stderr; libraries
    that write to them (uvicorn, yt-dlp) would crash, so point them at devnull."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def _log_level(debug: bool) -> int:
    if debug:
        return logging.DEBUG
    from .runtime import ENV_LOG_LEVEL, env

    name = (env(ENV_LOG_LEVEL) or "INFO").upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def _setup_logging(debug: bool) -> Optional[Path]:
    """Log to stderr and to ``<data dir>/logs/3catdownloader.log``."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file: Optional[Path] = None
    try:
        from .config import app_data_dir

        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "3catdownloader.log"
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    except Exception as exc:  # logging must never prevent start-up
        print(f"No s'ha pogut crear el fitxer de registre: {exc}", file=sys.stderr)
    level = _log_level(debug)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=handlers, force=True)
    if level > logging.DEBUG:  # one line per 3cat request is too chatty for normal use
        logging.getLogger("httpx").setLevel(logging.WARNING)
    return log_file


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    from .runtime import DEFAULT_HOST, ENV_HOST, ENV_PORT, env

    parser = argparse.ArgumentParser(
        prog="tresdl",
        description="Descarrega temporades i sèries senceres de 3cat.",
    )
    parser.add_argument("--no-gui", action="store_true",
                        help="només el servidor, sense finestra")
    parser.add_argument("--host", default=None,
                        help=f"adreça on escoltar (per defecte {DEFAULT_HOST}; "
                             f"0.0.0.0 = tota la xarxa, mode servidor) [{ENV_HOST}]")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port del servidor (per defecte, el de la configuració: 8765) [{ENV_PORT}]")
    parser.add_argument("--data-dir", default=None,
                        help="carpeta de configuració, cua i registres [TRESDL_DATA_DIR]")
    parser.add_argument("--debug", action="store_true",
                        help="registre detallat (i eines de desenvolupament a la finestra)")
    parser.add_argument("--version", action="version", version=f"3Cat Downloader {__version__}")
    args = parser.parse_args(argv)

    if args.host is None:
        args.host = env(ENV_HOST) or DEFAULT_HOST
    if args.port is None and env(ENV_PORT):
        try:
            args.port = int(env(ENV_PORT) or "")
        except ValueError:
            parser.error(f"{ENV_PORT} ha de ser un número")
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("el port ha d'estar entre 0 i 65535")
    return args


def _install_signal_handlers() -> None:
    """SIGTERM/SIGINT (and SIGBREAK on Windows) request a graceful stop."""
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum: int, _frame: Any) -> None:
        log.info("Senyal %s rebut; aturant…", signum)
        _shutdown.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):  # pragma: no cover - platform specific
                pass


# --------------------------------------------------------------------------- #
# Networking helpers
# --------------------------------------------------------------------------- #
def _connect_host(bind_host: str) -> str:
    """Address to reach a server bound to ``bind_host`` from this machine."""
    if bind_host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if bind_host == "::":
        return "::1"
    return bind_host


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _bind(host: str, port: int) -> socket.socket:
    """Bind a listening-ready socket on host:port (0 = ephemeral)."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: never share a port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:  # POSIX: allow fast restarts (TIME_WAIT) without sharing a live port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host.strip("[]"), port))
    except OSError:
        sock.close()
        raise
    return sock


def _health(host: str, port: int, timeout: float = 1.0) -> Optional[dict[str, Any]]:
    """GET /api/health; None if nothing (healthy) answers."""
    import httpx

    try:
        with httpx.Client(trust_env=False, timeout=timeout) as http:
            response = http.get(f"http://{_url_host(host)}:{port}/api/health")
        if response.status_code == 200:
            data = response.json()
            return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None
    return None


def _wait_for_health(host: str, port: int, server_thread: threading.Thread,
                     timeout: float = HEALTH_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server_thread.is_alive() or _shutdown.is_set():
            return False
        if _health(host, port, timeout=0.5):
            return True
        time.sleep(0.1)
    return False


def _lan_addresses() -> list[str]:
    """Best-effort list of this machine's LAN IPv4 addresses (for the start-up log)."""
    addresses: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET: no packet is sent for UDP connect
            addresses.append(s.getsockname()[0])
    except OSError:
        pass
    return [a for a in addresses if not a.startswith("127.")]


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def _has_display() -> bool:
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _run_window(url: str, debug: bool) -> bool:
    """Show the pywebview window and block until it is closed.

    Returns False when pywebview cannot be imported or started.
    """
    if not _has_display():
        return False
    try:
        import webview
    except Exception as exc:
        log.info("pywebview no disponible (%s); s'obrirà el navegador.", exc)
        return False
    storage: Optional[str] = None
    try:
        from .config import app_data_dir

        storage_dir = app_data_dir() / "webview"
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage = str(storage_dir)
    except Exception:
        storage = None
    try:
        webview.create_window(WINDOW_TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
                              min_size=WINDOW_MIN_SIZE, text_select=True)
        if storage:
            webview.start(debug=debug, private_mode=False, storage_path=storage)
        else:
            webview.start(debug=debug)
    except Exception as exc:
        log.warning("No s'ha pogut obrir la finestra (%s); s'obrirà el navegador.", exc)
        return False
    return True


def _wait_until_stopped(server_thread: Optional[threading.Thread] = None) -> None:
    """Block until a stop signal (or until the server thread dies)."""
    try:
        while server_thread is None or server_thread.is_alive():
            if _shutdown.wait(0.5):
                break
    except KeyboardInterrupt:
        pass
    print("\nAturant…", file=sys.stderr)


def _show_ui(url: str, args: argparse.Namespace, server_mode: bool,
             server_thread: Optional[threading.Thread]) -> None:
    if args.no_gui or server_mode:
        print(f"3Cat Downloader escoltant a {url} (Ctrl+C per aturar)", file=sys.stderr)
        _wait_until_stopped(server_thread)
        return
    if not _run_window(url, args.debug):
        if _has_display():
            webbrowser.open(url)
            print(f"3Cat Downloader obert al navegador: {url} (Ctrl+C per aturar)", file=sys.stderr)
        else:
            print(f"3Cat Downloader escoltant a {url} (Ctrl+C per aturar)", file=sys.stderr)
        _wait_until_stopped(server_thread)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    multiprocessing.freeze_support()
    _fix_std_streams()
    args = _parse_args(argv)
    if args.data_dir:
        os.environ["TRESDL_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    log_file = _setup_logging(args.debug)
    _shutdown.clear()
    _install_signal_handlers()

    from .config import app_data_dir
    from .instance import acquire_instance_lock

    # One instance per data folder, whatever port it ended up on. This must
    # happen before any DownloadManager exists: building one already rewrites
    # queue.json (jobs that were active are reset to queued).
    data_dir = app_data_dir()
    instance = acquire_instance_lock(data_dir)
    if instance is None:
        return _show_running_instance(data_dir, args)

    try:
        return _serve(args, log_file, instance, data_dir)
    finally:
        instance.release()


def _show_running_instance(data_dir: Path, args: argparse.Namespace) -> int:
    """Another process holds the instance lock: show it, never start a manager."""
    from .instance import running_instance_port
    from .server import APP_ID

    connect = _connect_host(args.host)
    deadline = time.monotonic() + INSTANCE_WAIT
    while True:  # the other instance may still be starting its server
        port = running_instance_port(data_dir)
        health = _health(connect, port, timeout=0.5) if port else None
        if health and health.get("app") == APP_ID:
            break
        if time.monotonic() >= deadline:
            message = ("3Cat Downloader ja s'està executant, però no respon. "
                       "Tanca'l (o espera uns segons) i torna-ho a provar.")
            log.error(message)
            print(message, file=sys.stderr)
            return 1
        time.sleep(0.2)
    url = f"http://{_url_host(connect)}:{port}/"
    log.info("3Cat Downloader ja s'està executant a %s", url)
    print(f"3Cat Downloader ja s'està executant a {url}", file=sys.stderr)
    if args.no_gui:
        return 1
    from .runtime import is_loopback

    if is_loopback(args.host) and not _run_window(url, args.debug) and _has_display():
        webbrowser.open(url)
    return 0


def _serve(args: argparse.Namespace, log_file: Optional[Path], instance: Any, data_dir: Path) -> int:
    """Run the server and the UI (this process holds the instance lock)."""
    import uvicorn

    from .api import ThreeCatClient
    from .downloader import DownloadManager
    from .runtime import build_runtime, find_ffmpeg
    from .server import SettingsStore, create_app

    runtime = build_runtime(args.host, no_gui=args.no_gui)
    store = SettingsStore()
    preferred = args.port if args.port is not None else int(store.get().server_port)

    # Bind first (our own socket, handed to uvicorn) so there is no race
    # between "is the port free?" and uvicorn binding it.
    try:
        sock = _bind(args.host, preferred)
    except OSError as exc:
        if runtime.server:
            # A service must be predictable (Sonarr, bookmarks, port mappings).
            message = f"No es pot escoltar a {args.host}:{preferred} ({exc}). Hi ha un altre programa al port?"
            log.error(message)
            print(message, file=sys.stderr)
            return 2
        log.warning("El port %s està ocupat; s'utilitzarà un port lliure.", preferred)
        sock = _bind(args.host, 0)
    port = sock.getsockname()[1]
    connect = _connect_host(args.host)
    url = f"http://{_url_host(connect)}:{port}/"
    instance.publish(port)

    client = ThreeCatClient()
    manager = DownloadManager(client, store.get)
    manager.start()
    app = create_app(client=client, manager=manager, settings_store=store, runtime=runtime)

    config = uvicorn.Config(app, log_config=None, access_log=False,
                            timeout_graceful_shutdown=3, lifespan="on",
                            proxy_headers=runtime.trust_proxy, forwarded_allow_ips="*" if runtime.trust_proxy else None)
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                                     name="uvicorn", daemon=True)
    server_thread.start()

    ffmpeg, ffmpeg_error = find_ffmpeg()
    exit_code = 0
    try:
        if not _wait_for_health(connect, port, server_thread):
            if not _shutdown.is_set():
                log.error("El servidor no ha arrencat. Consulta el registre: %s", log_file or "(consola)")
                exit_code = 1
        else:
            settings = store.get()
            if runtime.server:
                lan = ", ".join(f"http://{ip}:{port}/" for ip in _lan_addresses()) or url
            else:
                lan = url
            log.info("3Cat Downloader %s a punt — mode %s, %s | dades: %s | descàrregues: %s | "
                     "ffmpeg: %s | inici de sessió: %s",
                     __version__, "servidor" if runtime.server else "escriptori", lan, data_dir,
                     settings.download_dir, ffmpeg or f"NO TROBAT ({ffmpeg_error})",
                     "obligatori" if runtime.auth_required else "desactivat")
            _show_ui(url, args, runtime.server, server_thread)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Tancant…")
        app.state.events.close()  # end SSE streams so uvicorn can stop at once
        server.should_exit = True
        try:
            manager.stop(timeout=STOP_TIMEOUT)
        except TypeError:  # managers without a timeout argument
            manager.stop()
        except Exception:
            log.exception("Error en aturar el gestor de descàrregues")
        server_thread.join(timeout=5)
        try:
            sock.close()
        except OSError:
            pass
        log.info("Aturat.")
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
