"""3Cat Downloader entry point.

Starts the local HTTP server (FastAPI + uvicorn, bound to 127.0.0.1 only) in a
daemon thread and shows the UI in a pywebview window. If pywebview is not
available, the default web browser is opened instead and the program keeps
running until Ctrl+C.

Usage::

    python run.py              # desktop window
    python run.py --no-gui     # server only (headless, e.g. for Sonarr/Radarr later)
    python run.py --port 9000  # use another port (falls back to a free one if taken)

Only one instance runs per data folder (see ``tresdl/instance.py``); a second
launch just shows the running one, whatever port it is on.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import multiprocessing
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("tresdl.run")

HOST = "127.0.0.1"
WINDOW_TITLE = "3Cat Downloader"
WINDOW_SIZE = (1200, 800)
WINDOW_MIN_SIZE = (900, 600)
HEALTH_TIMEOUT = 30.0
INSTANCE_WAIT = 15.0    # seconds to wait for an already running instance to answer


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


def _setup_logging(debug: bool) -> Optional[Path]:
    """Log to the console and to ``<app data>/logs/3catdownloader.log``."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file: Optional[Path] = None
    try:
        from tresdl.config import app_data_dir

        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "3catdownloader.log"
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    except Exception as exc:  # logging must never prevent start-up
        print(f"No s'ha pogut crear el fitxer de registre: {exc}", file=sys.stderr)
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    if not debug:  # one line per 3cat request is too chatty for normal use
        logging.getLogger("httpx").setLevel(logging.WARNING)
    return log_file


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="3CatDownloader",
        description="Descarrega temporades i sèries senceres de 3cat.",
    )
    parser.add_argument("--no-gui", action="store_true",
                        help="només el servidor, sense finestra (mode sense interfície)")
    parser.add_argument("--port", type=int, default=None,
                        help="port del servidor local (per defecte, el de la configuració)")
    parser.add_argument("--debug", action="store_true",
                        help="registre detallat i eines de desenvolupament a la finestra")
    args = parser.parse_args(argv)
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("el port ha d'estar entre 0 i 65535")
    return args


# --------------------------------------------------------------------------- #
# Networking helpers
# --------------------------------------------------------------------------- #
def _bind(port: int) -> socket.socket:
    """Bind a listening-ready socket on HOST:port (0 = ephemeral)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: never share a port
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((HOST, port))
    except OSError:
        sock.close()
        raise
    return sock


def _health(port: int, timeout: float = 1.0) -> Optional[dict[str, Any]]:
    """GET /api/health on HOST:port; None if nothing (healthy) answers."""
    import httpx

    try:
        with httpx.Client(trust_env=False, timeout=timeout) as http:
            response = http.get(f"http://{HOST}:{port}/api/health")
        if response.status_code == 200:
            data = response.json()
            return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError):
        return None
    return None


def _wait_for_health(port: int, server_thread: threading.Thread, timeout: float = HEALTH_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not server_thread.is_alive():
            return False
        if _health(port, timeout=0.5):
            return True
        time.sleep(0.1)
    return False


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def _run_window(url: str, debug: bool) -> bool:
    """Show the pywebview window and block until it is closed.

    Returns False when pywebview cannot be imported or started.
    """
    try:
        import webview
    except Exception as exc:
        log.warning("pywebview no disponible (%s); s'obrirà el navegador.", exc)
        return False
    storage: Optional[str] = None
    try:
        from tresdl.config import app_data_dir

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


def _wait_until_interrupted(server_thread: Optional[threading.Thread] = None) -> None:
    """Block until Ctrl+C (or until the server thread dies)."""
    try:
        while server_thread is None or server_thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nAturant…", file=sys.stderr)


def _show_ui(url: str, args: argparse.Namespace, server_thread: Optional[threading.Thread]) -> None:
    if args.no_gui:
        print(f"3Cat Downloader escoltant a {url} (Ctrl+C per aturar)", file=sys.stderr)
        _wait_until_interrupted(server_thread)
        return
    if not _run_window(url, args.debug):
        webbrowser.open(url)
        print(f"3Cat Downloader obert al navegador: {url} (Ctrl+C per aturar)", file=sys.stderr)
        _wait_until_interrupted(server_thread)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    multiprocessing.freeze_support()
    _fix_std_streams()
    args = _parse_args(argv)
    log_file = _setup_logging(args.debug)

    from tresdl.config import app_data_dir
    from tresdl.instance import acquire_instance_lock

    # One instance per data folder, whatever port it ended up on. This must
    # happen before any DownloadManager exists: building one already rewrites
    # queue.json (jobs that were active are reset to queued).
    data_dir = app_data_dir()
    instance = acquire_instance_lock(data_dir)
    if instance is None:
        return _show_running_instance(data_dir, args)

    try:
        return _serve(args, log_file, instance)
    finally:
        instance.release()


def _show_running_instance(data_dir: Path, args: argparse.Namespace) -> int:
    """Another process holds the instance lock: show it, never start a manager."""
    from tresdl.instance import running_instance_port
    from tresdl.server import APP_ID

    deadline = time.monotonic() + INSTANCE_WAIT
    while True:  # the other instance may still be starting its server
        port = running_instance_port(data_dir)
        health = _health(port, timeout=0.5) if port else None
        if health and health.get("app") == APP_ID:
            break
        if time.monotonic() >= deadline:
            message = ("3Cat Downloader ja s'està executant, però no respon. "
                       "Tanca'l (o espera uns segons) i torna-ho a provar.")
            log.error(message)
            print(message, file=sys.stderr)
            return 1
        time.sleep(0.2)
    url = f"http://{HOST}:{port}/"
    log.info("3Cat Downloader ja s'està executant a %s", url)
    if args.no_gui:
        print(f"3Cat Downloader ja s'està executant a {url}", file=sys.stderr)
        return 1
    if not _run_window(url, args.debug):
        webbrowser.open(url)
    return 0


def _serve(args: argparse.Namespace, log_file: Optional[Path], instance: Any) -> int:
    """Run the server and the UI (this process holds the instance lock)."""
    import uvicorn

    from tresdl.api import ThreeCatClient
    from tresdl.downloader import DownloadManager
    from tresdl.server import SettingsStore, create_app

    store = SettingsStore()
    preferred = args.port if args.port is not None else int(store.get().server_port)

    # Bind first (our own socket, handed to uvicorn) so there is no race
    # between "is the port free?" and uvicorn binding it. Falling back to a
    # free port is safe: the instance lock, not the port, guards the queue.
    try:
        sock = _bind(preferred)
    except OSError:
        log.warning("El port %s està ocupat; s'utilitzarà un port lliure.", preferred)
        sock = _bind(0)
    port = sock.getsockname()[1]
    url = f"http://{HOST}:{port}/"
    instance.publish(port)

    client = ThreeCatClient()
    manager = DownloadManager(client, store.get)
    manager.start()
    app = create_app(client=client, manager=manager, settings_store=store)

    config = uvicorn.Config(app, host=HOST, port=port, log_config=None, access_log=False,
                            timeout_graceful_shutdown=3, lifespan="on")
    server = uvicorn.Server(config)
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                                     name="uvicorn", daemon=True)
    server_thread.start()

    exit_code = 0
    try:
        if not _wait_for_health(port, server_thread):
            log.error("El servidor no ha arrencat. Consulta el registre: %s", log_file or "(consola)")
            exit_code = 1
        else:
            log.info("Servidor a punt a %s", url)
            _show_ui(url, args, server_thread)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Tancant…")
        app.state.events.close()  # end SSE streams so uvicorn can stop at once
        server.should_exit = True
        try:
            manager.stop()
        except Exception:
            log.exception("Error en aturar el gestor de descàrregues")
        server_thread.join(timeout=10)
        try:
            sock.close()
        except OSError:
            pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
