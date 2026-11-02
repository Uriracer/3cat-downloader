"""Static checks for the deployment files (Linux installer, Docker, compose).

They run everywhere (also on Windows): real Docker/systemd runs happen in CI.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "deploy" / "linux" / "install.sh"
UNIT = ROOT / "deploy" / "linux" / "3cat-downloader.service"

DEFAULTS = {
    "SVC_USER": "tresdl", "group": "tresdl", "ENV_FILE": "/etc/3cat-downloader/3cat-downloader.env",
    "VENV": "/opt/3cat-downloader/venv", "DATA_DIR": "/var/lib/3cat-downloader",
    "DOWNLOAD_DIR": "/srv/media/3cat",
}


def _script() -> str:
    return INSTALL.read_text(encoding="utf-8")


def test_unit_file_matches_installer_template() -> None:
    match = re.search(r'write_file "\$UNIT_FILE" 0644 <<EOF\n(.*?)\nEOF\n', _script(), re.S)
    assert match, "unit heredoc not found in install.sh"
    rendered = re.sub(r"\$\{?(\w+)\}?", lambda m: DEFAULTS[m.group(1)], match.group(1))
    assert rendered.strip() == UNIT.read_text(encoding="utf-8").strip()


def test_installer_defaults_match_unit() -> None:
    script = _script()
    assert 'DEFAULT_USER="tresdl"' in script
    assert 'DEFAULT_DOWNLOAD_DIR="/srv/media/3cat"' in script
    assert 'PREFIX="/opt/$APP"' in script and 'DATA_DIR="/var/lib/$APP"' in script


@pytest.mark.parametrize("path", ["deploy/linux/install.sh", "docker/entrypoint.sh", "deploy/docker/setup.sh"])
def test_scripts_have_lf_line_endings(path: str) -> None:
    assert b"\r\n" not in (ROOT / path).read_bytes()


def _bash() -> str | None:
    found = shutil.which("bash")
    if found and "system32" in found.lower():  # WSL launcher, not a real bash
        return None
    return found


@pytest.mark.skipif(_bash() is None, reason="bash not available")
def test_installer_syntax_and_help() -> None:
    bash = _bash()
    subprocess.run([bash, "-n", str(INSTALL)], check=True)
    out = subprocess.run([bash, str(INSTALL), "--help"], capture_output=True, text=True,
                         encoding="utf-8", check=True).stdout
    assert "--from-source" in out and "--uninstall" in out
    bad = subprocess.run([bash, str(INSTALL), "--port", "abc", "--dry-run"], capture_output=True,
                         text=True, encoding="utf-8")
    assert bad.returncode != 0 and "port" in bad.stderr


def _shellcheck() -> str | None:
    return shutil.which("shellcheck")


@pytest.mark.skipif(_shellcheck() is None, reason="shellcheck not installed (pip install shellcheck-py)")
@pytest.mark.parametrize("path,shell", [("deploy/linux/install.sh", "bash"), ("docker/entrypoint.sh", "sh"),
                                        ("deploy/docker/setup.sh", "bash")])
def test_shellcheck(path: str, shell: str) -> None:
    result = subprocess.run([_shellcheck(), "-s", shell, "-x", str(ROOT / path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


def test_dockerfile_contract() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for needle in ("TRESDL_DATA_DIR=/config", "TRESDL_DOWNLOAD_DIR=/downloads", "TRESDL_HOST=0.0.0.0",
                   "TRESDL_FFMPEG=/usr/bin/ffmpeg", "EXPOSE 8765", "HEALTHCHECK", "tini", "ffmpeg"):
        assert needle in text, needle
    # Every file the Dockerfile copies must exist and must not be excluded by .dockerignore.
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for src in re.findall(r"^COPY (?!--from)(.+?) \S+$", text, re.M):
        for item in src.split():
            assert (ROOT / item).exists(), item
            assert item.rstrip("/") not in ignored, item


def test_compose_files_parse() -> None:
    yaml = pytest.importorskip("yaml")
    for rel in ("deploy/docker-compose.yml", "deploy/arr-stack/docker-compose.yml"):
        data = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
        assert "3cat-downloader" in data["services"], rel
    stack = yaml.safe_load((ROOT / "deploy/arr-stack/docker-compose.yml").read_text(encoding="utf-8"))
    svc = stack["services"]["3cat-downloader"]
    # Same path inside the 3cat container as in Sonarr/Radarr (/data), so imports need no mapping.
    assert svc["environment"]["TRESDL_DOWNLOAD_DIR"] == "/data/downloads/3cat"
    assert "${DATA_ROOT}/downloads/3cat:/data/downloads/3cat" in svc["volumes"]
    assert "${DATA_ROOT}:/data" in stack["services"]["sonarr"]["volumes"]
