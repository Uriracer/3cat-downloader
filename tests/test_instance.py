"""Tests for tresdl.instance (one running instance per data folder)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tresdl.instance import acquire_instance_lock, running_instance_port

ROOT = Path(__file__).resolve().parent.parent

_PROBE = (
    "import sys; from tresdl.instance import acquire_instance_lock;"
    "lock = acquire_instance_lock(sys.argv[1]);"
    "print('acquired' if lock else 'held')"
)


def _probe(data_dir: Path) -> str:
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(data_dir)], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_second_process_cannot_take_the_lock(tmp_path: Path) -> None:
    lock = acquire_instance_lock(tmp_path)
    assert lock is not None
    try:
        lock.publish(43905)
        assert running_instance_port(tmp_path) == 43905
        assert _probe(tmp_path) == "held"
        assert acquire_instance_lock(tmp_path) is None     # a second handle, same process
    finally:
        lock.release()
    assert running_instance_port(tmp_path) is None
    assert _probe(tmp_path) == "acquired"                  # released (also on crash/exit)


def test_lock_is_per_data_dir(tmp_path: Path) -> None:
    a = acquire_instance_lock(tmp_path / "a")
    b = acquire_instance_lock(tmp_path / "b")
    try:
        assert a is not None and b is not None
    finally:
        for lock in (a, b):
            if lock:
                lock.release()


def test_running_instance_port_ignores_garbage(tmp_path: Path) -> None:
    (tmp_path / "instance.json").write_text("{nope", encoding="utf-8")
    assert running_instance_port(tmp_path) is None
    (tmp_path / "instance.json").write_text('{"port": 0}', encoding="utf-8")
    assert running_instance_port(tmp_path) is None
