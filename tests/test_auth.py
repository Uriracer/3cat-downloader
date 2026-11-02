"""Server mode: authentication, CSRF/Host rules, locked settings, health payload."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_server import FakeClient, FakeManager  # tests/ is on sys.path (pytest rootdir import)
from tresdl import auth as authmod
from tresdl import server
from tresdl.config import Settings
from tresdl.runtime import LOOPBACK_NAMES, RuntimeConfig, build_runtime, find_ffmpeg

LAN = "http://192.168.1.50:8765"
PASSWORD = "contrasenya-segura"


def make_store(tmp_path: Path, overrides: dict | None = None) -> server.SettingsStore:
    saved: list[Settings] = []
    initial = Settings(download_dir=str(tmp_path / "dl"), api_key="clau-api-de-prova")
    store = server.SettingsStore(loader=lambda: initial, saver=saved.append, overrides=overrides or {})
    store.saved = saved  # type: ignore[attr-defined]
    return store


def make_app(tmp_path: Path, *, runtime: RuntimeConfig | None = None, overrides: dict | None = None,
             password_env: str | None = None):
    rt = runtime or RuntimeConfig(mode="server", auth_required=True, allowed_hosts=None,
                                  password_env=password_env)
    store = make_store(tmp_path, overrides)
    auth = authmod.AuthManager(tmp_path / "data", required=rt.auth_required, settings_get=store.get,
                               settings_set=store.set, password_env=rt.password_env)
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text("<!doctype html><title>3Cat</title>", encoding="utf-8")
    app = server.create_app(FakeClient(), FakeManager(), store, web_dir=web, runtime=rt, auth=auth)
    return app, store, auth


@pytest.fixture()
def srv(tmp_path: Path):
    app, store, auth = make_app(tmp_path)
    with TestClient(app, base_url=LAN) as http:
        yield http, store, auth


def setup_password(http: TestClient) -> None:
    r = http.post("/api/auth/setup", json={"password": PASSWORD}, headers={"Origin": LAN})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- unit

def test_hash_and_verify_password() -> None:
    encoded = authmod.hash_password("hola-hola", iterations=1000)
    assert encoded.startswith("pbkdf2_sha256$1000$")
    assert authmod.verify_password("hola-hola", encoded)
    assert not authmod.verify_password("hola-holA", encoded)
    assert not authmod.verify_password("x", "garbage")


def test_session_tokens(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    auth = authmod.AuthManager(tmp_path, required=True, settings_get=store.get, settings_set=store.set)
    token = auth.issue_session(now=1000)
    assert auth.check_session(token, now=1001)
    assert not auth.check_session(token, now=1000 + authmod.SESSION_SECONDS + 1)  # expired
    expiry, nonce, sig = token.split(".")
    assert not auth.check_session(f"{int(expiry) + 99}.{nonce}.{sig}", now=1001)  # tampered
    assert not auth.check_session("x.y", now=1001)
    auth.set_password("una-altra-clau")  # rotates the secret: old sessions die
    assert not auth.check_session(token, now=1001)
    assert (tmp_path / authmod.SECRET_FILE).is_file()


def test_login_throttle() -> None:
    now = [0.0]
    t = authmod.LoginThrottle(max_failures=3, window=60, lockout=30, clock=lambda: now[0])
    for _ in range(3):
        assert t.retry_after("1.2.3.4") == 0
        t.failure("1.2.3.4")
    assert t.retry_after("1.2.3.4") > 0
    assert t.retry_after("5.6.7.8") == 0
    now[0] = 31
    assert t.retry_after("1.2.3.4") == 0


def test_build_runtime(monkeypatch) -> None:
    for var in ("TRESDL_AUTH", "TRESDL_ALLOWED_HOSTS", "TRESDL_PASSWORD", "TRESDL_TRUST_PROXY"):
        monkeypatch.delenv(var, raising=False)
    desktop = build_runtime("127.0.0.1")
    assert desktop.mode == "desktop" and not desktop.auth_required
    assert desktop.allowed_hosts == LOOPBACK_NAMES
    server_rt = build_runtime("0.0.0.0")
    assert server_rt.mode == "server" and server_rt.auth_required and server_rt.allowed_hosts is None
    monkeypatch.setenv("TRESDL_ALLOWED_HOSTS", "nas.local, 3cat.casa")
    assert build_runtime("0.0.0.0").allowed_hosts == frozenset({"nas.local", "3cat.casa"}) | LOOPBACK_NAMES
    monkeypatch.setenv("TRESDL_AUTH", "none")
    assert not build_runtime("0.0.0.0").auth_required
    monkeypatch.setenv("TRESDL_AUTH", "required")
    assert build_runtime("127.0.0.1", no_gui=True).mode == "server"


def test_find_ffmpeg_env_override(monkeypatch, tmp_path: Path) -> None:
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"")
    monkeypatch.setenv("TRESDL_FFMPEG", str(fake))
    assert find_ffmpeg(refresh=True) == (str(fake), None)
    monkeypatch.setenv("TRESDL_FFMPEG", str(tmp_path / "missing"))
    path, error = find_ffmpeg(refresh=True)
    assert path is None and "TRESDL_FFMPEG" in error
    monkeypatch.delenv("TRESDL_FFMPEG")
    assert find_ffmpeg(refresh=True)[0]  # bundled imageio-ffmpeg in the dev venv


# --------------------------------------------------------------------------- flows

def test_first_run_setup_login_logout(srv) -> None:
    http, store, _ = srv
    status = http.get("/api/auth/status").json()
    assert status == {"mode": "server", "auth_required": True, "configured": False,
                      "authenticated": False, "password_from_env": False}
    assert http.get("/api/settings").status_code == 401
    r = http.post("/api/auth/setup", json={"password": "curta"}, headers={"Origin": LAN})
    assert r.status_code == 400 and "8" in r.json()["detail"]
    setup_password(http)
    assert http.get("/api/settings").status_code == 200  # cookie set by setup
    assert http.post("/api/auth/setup", json={"password": PASSWORD}, headers={"Origin": LAN}).status_code == 409
    assert store.get().password_hash.startswith("pbkdf2_sha256$")

    http.post("/api/auth/logout", headers={"Origin": LAN})
    assert http.get("/api/settings").status_code == 401
    r = http.post("/api/auth/login", json={"password": "dolenta!!"}, headers={"Origin": LAN})
    assert r.status_code == 401 and r.json()["detail"] == "Contrasenya incorrecta."
    r = http.post("/api/auth/login", json={"password": PASSWORD}, headers={"Origin": LAN})
    assert r.status_code == 200 and r.json()["authenticated"] is True
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    assert http.get("/api/downloads").status_code == 200


def test_settings_never_expose_password_hash(srv) -> None:
    http, _, _ = srv
    setup_password(http)
    data = http.get("/api/settings").json()
    assert "password_hash" not in data and data["locked"] == []
    r = http.put("/api/settings", json={"password_hash": "pbkdf2_sha256$1$a$b", "max_parallel": 3},
                 headers={"Origin": LAN})
    assert r.status_code == 200 and "password_hash" not in r.json()
    assert http.post("/api/auth/login", json={"password": PASSWORD}, headers={"Origin": LAN}).status_code == 200


def test_protected_paths_need_auth(srv) -> None:
    http, _, _ = srv
    setup_password(http)
    http.cookies.clear()
    for method, path in [("GET", "/api/downloads"), ("GET", "/api/events"), ("GET", "/api/docs"),
                         ("GET", "/api/openapi.json"), ("POST", "/api/resolve"), ("GET", "/api"),
                         ("GET", "/api/settings/"), ("HEAD", "/api/downloads"), ("GET", "/api/search?q=x")]:
        r = http.request(method, path, headers={"Origin": LAN})
        assert r.status_code == 401, (method, path, r.status_code)
    assert http.get("/").status_code == 200  # the UI itself (login screen) is public
    assert http.get("/api/health").json() == {"ok": True, "version": server.APP_VERSION, "app": server.APP_ID}


def test_api_key_header_and_query(srv) -> None:
    http, _, _ = srv
    assert http.get("/api/downloads", headers={"X-Api-Key": "clau-api-de-prova"}).status_code == 200
    assert http.get("/api/downloads?apikey=clau-api-de-prova").status_code == 200
    assert http.get("/api/downloads", headers={"X-Api-Key": "dolenta"}).status_code == 401
    health = http.get("/api/health", headers={"X-Api-Key": "clau-api-de-prova"}).json()
    assert health["mode"] == "server" and "ffmpeg" in health
    # Non-browser POST (no Origin/Referer) with the key is allowed.
    r = http.post("/api/downloads", json={"episode_ids": [1]}, headers={"X-Api-Key": "clau-api-de-prova"})
    assert r.status_code != 401 and r.status_code != 403


def test_csrf_rules(srv) -> None:
    http, _, _ = srv
    setup_password(http)
    body = {"max_parallel": 2}
    assert http.put("/api/settings", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert http.put("/api/settings", json=body, headers={"Origin": "http://192.168.1.50:9999"}).status_code == 403
    assert http.put("/api/settings", json=body, headers={"Origin": "null"}).status_code == 403
    assert http.put("/api/settings", json=body,
                    headers={"Referer": "http://evil.example/page"}).status_code == 403
    assert http.put("/api/settings", json=body, headers={"Referer": f"{LAN}/"}).status_code == 200
    assert http.put("/api/settings", json=body, headers={"Origin": LAN}).status_code == 200


def test_login_throttled(srv) -> None:
    http, _, _ = srv
    setup_password(http)
    for _ in range(authmod.THROTTLE_MAX_FAILURES):
        http.post("/api/auth/login", json={"password": "dolenta!!"}, headers={"Origin": LAN})
    r = http.post("/api/auth/login", json={"password": PASSWORD}, headers={"Origin": LAN})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_change_password(srv) -> None:
    http, _, _ = srv
    setup_password(http)
    r = http.put("/api/auth/password", json={"current": "dolenta", "new": "nova-contrasenya"},
                 headers={"Origin": LAN})
    assert r.status_code == 400
    r = http.put("/api/auth/password", json={"current": PASSWORD, "new": "nova-contrasenya"},
                 headers={"Origin": LAN})
    assert r.status_code == 200
    assert http.get("/api/settings").status_code == 200  # fresh cookie issued
    http.cookies.clear()
    assert http.post("/api/auth/login", json={"password": "nova-contrasenya"},
                     headers={"Origin": LAN}).status_code == 200


def test_password_from_env(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path, password_env="clau-de-entorn")
    with TestClient(app, base_url=LAN) as http:
        status = http.get("/api/auth/status").json()
        assert status["configured"] and status["password_from_env"]
        assert http.post("/api/auth/setup", json={"password": PASSWORD}, headers={"Origin": LAN}).status_code == 409
        assert http.post("/api/auth/login", json={"password": "clau-de-entorn"},
                         headers={"Origin": LAN}).status_code == 200
        r = http.put("/api/auth/password", json={"current": "clau-de-entorn", "new": "nova-contrasenya"},
                     headers={"Origin": LAN})
        assert r.status_code == 400 and "TRESDL_PASSWORD" in r.json()["detail"]


def test_locked_settings(tmp_path: Path) -> None:
    forced = str(tmp_path / "forcat")
    app, store, _ = make_app(tmp_path, overrides={"download_dir": forced})
    with TestClient(app, base_url=LAN, headers={"X-Api-Key": "clau-api-de-prova"}) as http:
        data = http.get("/api/settings").json()
        assert data["download_dir"] == forced and data["locked"] == ["download_dir"]
        r = http.put("/api/settings", json={"download_dir": str(tmp_path / "altra")})
        assert r.status_code == 400 and "TRESDL_DOWNLOAD_DIR" in r.json()["detail"]
        r = http.put("/api/settings", json={"download_dir": forced, "max_parallel": 4})
        assert r.status_code == 200 and r.json()["max_parallel"] == 4
    # The forced value is never written to disk.
    assert store.saved[-1].download_dir == str(tmp_path / "dl")
    assert store.get().download_dir == forced


def test_open_folder_disabled_in_server_mode(srv) -> None:
    http, _, _ = srv
    r = http.post("/api/open-folder", json={}, headers={"X-Api-Key": "clau-api-de-prova"})
    assert r.status_code == 400 and r.json()["detail"] == "No disponible en mode servidor."


def test_allowed_hosts_restriction(tmp_path: Path) -> None:
    rt = RuntimeConfig(mode="server", auth_required=True, allowed_hosts=frozenset({"nas.local"}))
    app, _, _ = make_app(tmp_path, runtime=rt)
    with TestClient(app, base_url="http://nas.local:8765") as http:
        assert http.get("/api/auth/status").status_code == 200
    with TestClient(app, base_url="http://attacker.example") as http:
        assert http.get("/api/auth/status").status_code == 403


def test_desktop_mode_unchanged(tmp_path: Path) -> None:
    app, _, _ = make_app(tmp_path, runtime=RuntimeConfig())
    with TestClient(app, base_url="http://127.0.0.1") as http:
        status = http.get("/api/auth/status").json()
        assert status["mode"] == "desktop" and not status["auth_required"] and status["authenticated"]
        assert http.get("/api/settings").status_code == 200
        assert "ffmpeg" in http.get("/api/health").json()
        assert http.post("/api/auth/login", json={"password": "x"}).status_code == 400
    with TestClient(app, base_url="http://192.168.1.50") as http:
        assert http.get("/api/settings").status_code == 403  # strict Host check
