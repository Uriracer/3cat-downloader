# Server mode & distribution — contract

Goal: the same code base ships as
1. **Windows desktop app** (PyInstaller exe + installer) — current behaviour, unchanged UX.
2. **Docker image** `ghcr.io/<owner>/3cat-downloader` for `linux/amd64` and `linux/arm64`
   (Raspberry Pi 4/5 with 64-bit OS, Proxmox LXC/VM, any NAS).
3. **Native Linux service** (Debian / Ubuntu / Raspberry Pi OS, systemd) via `deploy/linux/install.sh`.
4. **Python package** (`pip install` / `pipx install` from a release wheel or git).

The target home setup: a Proxmox mini-PC running Jellyfin, Sonarr, Radarr, Jellyseerr, Prowlarr,
Jackett, qBittorrent… 3Cat Downloader runs next to them (same Docker network or same LAN) and writes
into the shared media/downloads storage. Real Sonarr/Radarr integration (Newznab + SABnzbd emulation,
see `tresdl/arr/README.md`) is a LATER step; this step only makes it deployable and safe on a LAN.

Repository name: `3cat-downloader`. Python package/import name: `tresdl`. Console scripts: `tresdl`
and `3cat-downloader` (both → `tresdl.cli:main`). `python -m tresdl` also works.
Version single source: `tresdl/__init__.py` `__version__` (pyproject uses dynamic version).
Never hard-code a GitHub owner: CI uses `${{ github.repository }}` / `${{ github.repository_owner }}`;
docs use the placeholder `<usuari>` (and say to replace it).

## Repository layout (after this step)

```
pyproject.toml                 build (setuptools), deps, extras, console scripts, package data (tresdl/web/**)
requirements.txt               runtime deps for server/headless (no pywebview)  — used by Docker/native
requirements-desktop.txt       -r requirements.txt + pywebview + pyinstaller
requirements-dev.txt           -r requirements-desktop.txt + pytest (+ tools used in CI)
run.py                         thin wrapper: from tresdl.cli import main; sys.exit(main())  (PyInstaller entry)
tresdl/cli.py                  all start-up logic formerly in run.py
tresdl/__main__.py             python -m tresdl
tresdl/auth.py                 password hashing, sessions, API key checks
tresdl/web/                    static UI (moved here, DONE)
3CatDownloader.spec            PyInstaller spec (paths already updated to tresdl/web)
Dockerfile, .dockerignore
docker/entrypoint.sh           PUID/PGID/UMASK handling, then exec tresdl
deploy/docker-compose.yml      standalone example
deploy/arr-stack/              full example stack: docker-compose.yml, .env.example, README.md
deploy/linux/install.sh        native installer/updater/uninstaller (Debian family, systemd)
deploy/linux/3cat-downloader.service
deploy/windows/3CatDownloader.iss   Inno Setup installer script
.github/workflows/ci.yml       tests (ubuntu + windows), lint shell/Dockerfile/actions, docker build (no push)
.github/workflows/release.yml  on tag v*: multi-arch image → GHCR, Windows zip + installer, wheel/sdist, GitHub Release
.github/dependabot.yml
docs/                          docker.md, raspberry-pi.md, proxmox.md, linux.md, windows.md, configuration.md (Catalan)
README.md                      Catalan, short, links to docs/
LICENSE                        MIT (owner: "Oriol Serrat"; user may change)
CHANGELOG.md
```

## Modes

* **Desktop mode** (default when bound to a loopback address): exactly today's behaviour — no
  login, strict Host/Origin checks (127.0.0.1/localhost/::1), `open-folder` enabled, pywebview window.
* **Server mode**: active when the bind host is not loopback (e.g. `0.0.0.0`) OR `--no-gui` with
  `TRESDL_AUTH=required`. Differences:
  - authentication required (unless `TRESDL_AUTH=none`, which logs a loud warning);
  - Host header check: allow any host unless `TRESDL_ALLOWED_HOSTS` is set (comma list, `*` = any);
  - CSRF: for state-changing methods (POST/PUT/PATCH/DELETE) a present `Origin` (or, if absent, `Referer`)
    must have the same host:port as the request `Host`; requests with neither header are allowed
    (non-browser clients such as Sonarr/curl) but still need auth;
  - `POST /api/open-folder` returns 400 `{"detail": "No disponible en mode servidor."}`;
  - never opens a window or browser.
* `--no-gui` alone on loopback = headless desktop mode (no auth, strict loopback checks) — used by tests.

## Environment variables (all optional; CLI flags win over env, env wins over settings.json)

| Var | Default | Meaning |
|---|---|---|
| `TRESDL_DATA_DIR` | `%APPDATA%\3CatDownloader` / `~/.3catdownloader` (Docker: `/config`, native: `/var/lib/3cat-downloader`) | settings, queue, logs, session secret |
| `TRESDL_HOST` | `127.0.0.1` (Docker: `0.0.0.0`) | bind address (`--host`) |
| `TRESDL_PORT` | `8765` | port (`--port`). In server mode the port is NOT silently changed: if taken, exit with a clear error (services must be predictable). Desktop mode keeps the fallback. |
| `TRESDL_DOWNLOAD_DIR` | settings value (Docker: `/downloads`) | forces download_dir; the field becomes read-only |
| `TRESDL_AUTH` | `auto` | `auto` (required in server mode, off in desktop), `required`, `none` |
| `TRESDL_PASSWORD` | — | if set, this is the login password (stored nowhere; UI cannot change it) |
| `TRESDL_API_KEY` | settings value | forces api_key (read-only in UI) |
| `TRESDL_ALLOWED_HOSTS` | `*` in server mode | comma-separated host names accepted in `Host` |
| `TRESDL_FFMPEG` | auto | ffmpeg path. Auto order: env → bundled imageio-ffmpeg if importable and its binary exists → `shutil.which("ffmpeg")`. Clear Catalan error in /api/health + job errors if none. |
| `TRESDL_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING` (`--debug` = DEBUG) |
| `PUID` / `PGID` / `UMASK` / `TZ` | 1000 / 1000 / 022 / `Europe/Madrid` | Docker only (entrypoint) |

`GET /api/settings` returns the settings plus `"locked": ["download_dir", ...]` listing fields forced
by environment, and `PUT` that tries to change a locked field returns 400 with a Catalan message
naming the variable. `imageio-ffmpeg` stays a runtime dependency in requirements.txt (it has linux
x86_64/aarch64 wheels) but Docker/native installs use the distro `ffmpeg` (set `TRESDL_FFMPEG` or rely
on the auto order — Docker sets `TRESDL_FFMPEG=/usr/bin/ffmpeg`).

## Authentication (tresdl/auth.py + server.py)

* Password stored as `password_hash` in settings.json: `pbkdf2_sha256$<iterations>$<salt b64>$<hash b64>`
  (hashlib.pbkdf2_hmac, ≥ 600 000 iterations), compared with `hmac.compare_digest`.
* Session: random secret in `<data_dir>/secret.key` (created 0600 on first use). Cookie `tresdl_session`
  = signed (HMAC-SHA256) `<expiry>.<nonce>`; HttpOnly, SameSite=Strict, Path=/, Max-Age 30 days,
  `Secure` only when the request came over https (X-Forwarded-Proto honoured only if
  `TRESDL_TRUST_PROXY=1`). Changing the password rotates the secret (logs everyone out).
* API key: `X-Api-Key` header or `apikey` query parameter → authenticated (for Sonarr/Radarr/scripts).
* Login throttling: per client IP, after 5 failures within 10 min, 429 for 1 min… simple in-memory.
* Endpoints (always reachable without auth):
  - `GET /api/auth/status` → `{"mode": "desktop"|"server", "auth_required": bool, "configured": bool, "authenticated": bool, "password_from_env": bool}`
  - `POST /api/auth/setup {"password"}` → only when auth required, not configured and no env password; min length 8; sets cookie.
  - `POST /api/auth/login {"password"}` → sets cookie; 401 `{"detail": "Contrasenya incorrecta."}`
  - `POST /api/auth/logout` → clears cookie.
* `PUT /api/auth/password {"current", "new"}` (auth required; 400 when password_from_env).
* Also unauthenticated: `GET /api/health` (only `{"ok": true, "version": ...}` when not authenticated in
  server mode; full payload otherwise) and the static UI files. Everything else under `/api/` → 401
  `{"detail": "Cal iniciar sessió."}` when auth is required and missing. SSE `/api/events` needs auth too
  (cookie works with EventSource).
* `GET /api/health` full payload adds `"mode"`, `"ffmpeg"` (bool), `"ffmpeg_error"` (str|null).

## UI (tresdl/web)

* On load call `/api/auth/status`. If `auth_required && !authenticated` show a centred login card
  (or "Crea una contrasenya" card when `!configured`) before anything else; on 401 from any call go back to it.
* Header: when mode == server show a small "Mode servidor" badge and a "Tanca la sessió" button.
* Settings: fields listed in `locked` are disabled with hint "Definit per la variable d'entorn TRESDL_…";
  section "Seguretat" in server mode with change-password form (hidden when `password_from_env`);
  API key shown with a copy button.
* Server mode: hide every "Obre la carpeta" button; show the job's output path as selectable text instead.
* Keep the no-innerHTML rule for API data.

## CLI (tresdl/cli.py)

`tresdl [--no-gui] [--host H] [--port P] [--data-dir D] [--debug] [--version]`
* Non-loopback host ⇒ server mode ⇒ implies `--no-gui`.
* On Linux/macOS with no `DISPLAY`/`WAYLAND_DISPLAY`, or when pywebview is not installed, never try a
  window; in desktop mode fall back to printing the URL (open the browser only on Windows/macOS or when a
  display exists).
* SIGTERM and SIGINT ⇒ graceful shutdown: stop accepting requests, `manager.stop()` (in-flight jobs back to
  QUEUED, partial files kept), release instance lock, exit 0 within ~10 s (docker stop default grace).
* Logs to stderr always and to `<data_dir>/logs/3catdownloader.log` (rotating).
* Startup log line prints mode, bind URL, data dir, download dir, ffmpeg path, auth state.

## Docker image

* Base `python:3.13-slim` (Debian trixie). Multi-stage: build wheels, final image has the venv,
  `ffmpeg`, `tini`, `ca-certificates`, `tzdata`; no compilers. Runs `tini -- /entrypoint.sh`.
* Entrypoint: if running as root, create/adjust group+user `tresdl` to PGID/PUID, `chown` `/config`
  (not recursive on `/downloads`; only ensure it is writable and warn otherwise), apply `UMASK`, then
  `exec setpriv --reuid=$PUID --regid=$PGID --init-groups tresdl --no-gui`; if already non-root
  (`--user`), just exec.
* `ENV TRESDL_DATA_DIR=/config TRESDL_DOWNLOAD_DIR=/downloads TRESDL_HOST=0.0.0.0 TRESDL_PORT=8765 TRESDL_FFMPEG=/usr/bin/ffmpeg PYTHONUNBUFFERED=1`
* `EXPOSE 8765`, `VOLUME ["/config", "/downloads"]`, `HEALTHCHECK` with python urllib against `/api/health`.
* OCI labels (source, version, licenses) via build args.
* Geo note for docs: 3cat content is often limited to Spain; the container must NOT go through a foreign
  VPN (e.g. if qBittorrent uses gluetun, keep 3cat-downloader outside that network namespace).

## Native Linux (deploy/linux/install.sh)

Bash, `set -euo pipefail`, shellcheck-clean, idempotent, root required (re-exec with sudo if available).
`install.sh [--version vX.Y.Z|--from-source DIR] [--user USER] [--port P] [--download-dir DIR] [--update] [--uninstall [--purge]]`
* Supports Debian 12/13, Ubuntu 22.04+, Raspberry Pi OS (bookworm/trixie, 64-bit recommended; 32-bit best
  effort using piwheels). apt installs `python3 python3-venv python3-pip ffmpeg ca-certificates curl`.
* Python ≥ 3.11 required; abort with a clear message otherwise.
* Layout: venv `/opt/3cat-downloader/venv`, data `/var/lib/3cat-downloader`, env file
  `/etc/3cat-downloader/3cat-downloader.env` (EnvironmentFile; created once, preserved on update),
  unit `/etc/systemd/system/3cat-downloader.service`.
* Default service user: system user `tresdl` (created if missing); `--user media` to share files with the
  arr stack. Default download dir `/srv/media/3cat` (created, owned by the user).
* Installs the package: `--from-source DIR` → `pip install DIR`; otherwise from the GitHub release wheel of
  the given/latest version (`https://github.com/<owner>/3cat-downloader/releases/...`; owner configurable via
  `TRESDL_REPO=owner/name`, with a default placeholder that the release workflow rewrites in the attached copy).
* Unit: `ExecStart=/opt/3cat-downloader/venv/bin/tresdl --no-gui`, `Restart=on-failure`, hardening
  (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`, `PrivateTmp`, `ReadWritePaths=` data
  dir and download dir, `UMask=0002`), `TimeoutStopSec=20`.
* Prints the URL(s) `http://<lan-ip>:<port>/` and next steps (create password in the UI).

## CI / release

* `ci.yml` (push + PR): pytest `-m "not live"` on ubuntu-latest (3.11, 3.13) and windows-latest (3.11);
  shellcheck on `deploy/linux/*.sh docker/*.sh`; hadolint on Dockerfile; actionlint; `docker buildx build`
  for linux/amd64 (no push) + container smoke test (`/api/health` responds, `/api/auth/status` says server).
* `release.yml` (tag `v*.*.*`): check tag == `__version__`; test; build & push multi-arch image
  (amd64, arm64 via QEMU) to `ghcr.io/${{ github.repository_owner }}/3cat-downloader` with tags `X.Y.Z`, `X.Y`,
  `latest`; build Windows onedir with PyInstaller, zip it, build Inno Setup installer; build sdist + wheel;
  create GitHub Release with all artifacts + `install.sh` (owner placeholder replaced) + checksums
  (SHA256SUMS). Live tests are never run in CI (geo-restricted API).
