"""Shared pytest configuration.

* Registers the ``live`` marker (tests that hit the real 3cat servers).
  Deselect them with ``pytest -m "not live"``.
* Makes the project root importable when pytest is run without ``-m``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "live: test talks to the real 3cat servers (network)")


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path_factory, monkeypatch) -> None:
    """Never read or write the user's real settings/queue during tests."""
    monkeypatch.setenv("TRESDL_DATA_DIR", str(tmp_path_factory.mktemp("tresdl-data")))
