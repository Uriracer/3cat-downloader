"""Command line: argument/env precedence and a real server-mode start/stop."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tresdl import cli

ROOT = Path(__file__).resolve().parent.parent


def test_args_env_precedence(monkeypatch) -> None:
    monkeypatch.setenv("TRESDL_HOST", "0.0.0.0")
    monkeypatch.setenv("TRESDL_PORT", "9123")
    args = cli._parse_args([])
    assert (args.host, args.port) == ("0.0.0.0", 9123)
    args = cli._parse_args(["--host", "127.0.0.1", "--port", "9000"])
    assert (args.host, args.port) == ("127.0.0.1", 9000)
    monkeypatch.delenv("TRESDL_HOST")
    monkeypatch.delenv("TRESDL_PORT")
    args = cli._parse_args([])
    assert (args.host, args.port) == ("127.0.0.1", None)
    monkeypatch.setenv("TRESDL_PORT", "nope")
    with pytest.raises(SystemExit):
        cli._parse_args([])


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._parse_args(["--version"])
    assert exc.value.code == 0
    from tresdl import __version__

    assert __version__ in capsys.readouterr().out


def test_connect_host() -> None:
    assert cli._connect_host("0.0.0.0") == "127.0.0.1"
    assert cli._connect_host("::") == "::1"
    assert cli._connect_host("192.168.1.5") == "192.168.1.5"
    assert cli._url_host("::1") == "[::1]"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(tmp_path: Path, port: int, extra_env: dict[str, str]) -> subprocess.Popen:
    env = {**os.environ, "TRESDL_DATA_DIR": str(tmp_path / "data"),
           "TRESDL_DOWNLOAD_DIR": str(tmp_path / "dl"), "PYTHONUTF8": "1", **extra_env}
    env.pop("TRESDL_HOST", None)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(
        [sys.executable, "-m", "tresdl", "--no-gui", "--host", "0.0.0.0", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=flags, text=True, encoding="utf-8", errors="replace")


def _wait_health(port: int, proc: subprocess.Popen, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early: {proc.stdout.read() if proc.stdout else ''}")
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5, trust_env=False)
            if r.status_code == 200:
                return r.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("server did not start")


def _stop(proc: subprocess.Popen) -> str:
    if sys.platform == "win32":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        raise AssertionError(f"server did not stop gracefully:\n{out}")
    return out


def test_server_mode_start_and_graceful_stop(tmp_path: Path) -> None:
    port = _free_port()
    proc = _start(tmp_path, port, {"TRESDL_API_KEY": "clau-de-prova-cli"})
    try:
        health = _wait_health(port, proc)
        assert health == {"ok": True, "version": health["version"], "app": "3cat-downloader"}  # minimal: not logged in
        base = f"http://127.0.0.1:{port}"
        status = httpx.get(f"{base}/api/auth/status", trust_env=False).json()
        assert status["mode"] == "server" and status["auth_required"] and not status["configured"]
        assert httpx.get(f"{base}/api/settings", trust_env=False).status_code == 401
        settings = httpx.get(f"{base}/api/settings", headers={"X-Api-Key": "clau-de-prova-cli"},
                             trust_env=False).json()
        assert settings["download_dir"] == str(tmp_path / "dl")
        assert sorted(settings["locked"]) == ["api_key", "download_dir"]
    finally:
        out = _stop(proc) if proc.poll() is None else (proc.stdout.read() if proc.stdout else "")
    assert proc.returncode == 0, out
    assert "mode servidor" in out and "Aturat." in out


def test_server_mode_port_taken_is_an_error(tmp_path: Path) -> None:
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        proc = _start(tmp_path, port, {})
        out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 2, out
    assert "No es pot escoltar" in out
