"""Tests for tresdl.config."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tresdl import config
from tresdl.config import Settings, app_data_dir, load_settings, save_settings
from tresdl.naming import DEFAULT_TEMPLATE


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    return tmp_path / "appdata" / "3CatDownloader"


def test_app_data_dir_uses_appdata(data_dir: Path) -> None:
    assert app_data_dir() == data_dir
    assert data_dir.is_dir()


def test_app_data_dir_fallback_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert app_data_dir() == tmp_path / ".3catdownloader"


def test_app_data_dir_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path / "custom"))
    assert app_data_dir() == tmp_path / "custom"


def test_defaults_match_contract() -> None:
    s = Settings()
    assert s.max_parallel == 2 and s.quality == "best" and s.server_port == 8765
    assert s.subtitles and not s.embed_subtitles and not s.audio_description and s.skip_existing
    assert s.filename_template == DEFAULT_TEMPLATE
    assert s.download_dir.endswith(str(Path("Videos") / "3Cat"))
    assert len(s.api_key) == 32 and int(s.api_key, 16) >= 0


def test_first_run_creates_file_with_persistent_api_key(data_dir: Path) -> None:
    s1 = load_settings()
    path = data_dir / "settings.json"
    assert path.exists()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["api_key"] == s1.api_key
    s2 = load_settings()
    assert s2.api_key == s1.api_key


def test_roundtrip(data_dir: Path) -> None:
    s = load_settings()
    s.quality = "720"
    s.max_parallel = 4
    s.download_dir = str(data_dir / "Vídeos")
    save_settings(s)
    assert load_settings() == s


def test_forward_compatible_loading(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "settings.json"
    path.write_text(json.dumps({
        "quality": "480", "future_option": {"x": 1}, "api_key": "abc123",
    }), encoding="utf-8")
    s = load_settings()
    assert s.quality == "480"
    assert s.api_key == "abc123"
    assert s.max_parallel == 2               # missing -> default
    assert not hasattr(s, "future_option")   # unknown -> ignored
    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["max_parallel"] == 2 and rewritten["api_key"] == "abc123"


def test_invalid_values_fall_back(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({
        "quality": "4k", "max_parallel": 99, "server_port": 80, "subtitles": "yes",
        "skip_existing": "false", "filename_template": "{bogus}", "download_dir": "",
        "api_key": "",
    }), encoding="utf-8")
    s = load_settings()
    assert s.quality == "best"
    assert s.max_parallel == config.MAX_PARALLEL_LIMIT
    assert s.server_port == 8765
    assert s.subtitles is True
    assert s.skip_existing is False
    assert s.filename_template == DEFAULT_TEMPLATE
    assert s.download_dir == Settings().download_dir
    assert len(s.api_key) == 32


def test_quality_accepts_p_suffix() -> None:
    assert Settings.from_dict({"quality": "720p"}).quality == "720"
    assert Settings.from_dict({"quality": "MP4"}).quality == "mp4"


def test_corrupt_file_is_backed_up(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    s = load_settings()
    assert s.quality == "best"
    assert (data_dir / "settings.json.bak").read_text(encoding="utf-8") == "{not json"
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == s.api_key


def test_atomic_write_leaves_no_temp_files(data_dir: Path) -> None:
    for _ in range(5):
        save_settings(Settings())
    names = sorted(p.name for p in data_dir.iterdir())
    assert names == ["settings.json"]


def test_explicit_path(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "s.json"
    s = Settings(quality="576")
    save_settings(s, path)
    assert load_settings(path) == s


def test_updated_partial() -> None:
    s = Settings()
    s2 = s.updated({"quality": "1080", "max_parallel": "3", "unknown": 1, "api_key": ""})
    assert s2.quality == "1080" and s2.max_parallel == 3 and s2.api_key == s.api_key
    assert s.quality == "best"
