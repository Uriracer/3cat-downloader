#!/usr/bin/env bash
# 3Cat Downloader — instal·lador natiu per a Debian, Ubuntu i Raspberry Pi OS (systemd).
#
#   Instal·lar/actualitzar:  curl -fsSL https://github.com/Uriracer/3cat-downloader/releases/latest/download/install.sh | sudo bash
#   Des del codi font:       sudo ./deploy/linux/install.sh --from-source .
#   Desinstal·lar:           sudo ./install.sh --uninstall [--purge]
#
# Executa «install.sh --help» per veure totes les opcions.
set -euo pipefail

APP="3cat-downloader"
# GitHub repository with the releases (the release workflow sets it to the publishing repo).
DEFAULT_REPO="Uriracer/3cat-downloader"
REPO="${TRESDL_REPO:-$DEFAULT_REPO}"

PREFIX="/opt/$APP"
VENV="$PREFIX/venv"
DATA_DIR="/var/lib/$APP"
ETC_DIR="/etc/$APP"
ENV_FILE="$ETC_DIR/$APP.env"
UNIT_FILE="/etc/systemd/system/$APP.service"
DEFAULT_USER="tresdl"
DEFAULT_PORT="8765"
DEFAULT_DOWNLOAD_DIR="/srv/media/3cat"
MIN_PY_MINOR=11

VERSION=""
FROM_SOURCE=""
SVC_USER=""
PORT=""
DOWNLOAD_DIR=""
ACTION="install"
PURGE=0
DRY_RUN=0

# --------------------------------------------------------------------------- helpers
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mAvís:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Run a command, or only print it with --dry-run.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

# Write stdin to a file (mode $2), or print it with --dry-run.
write_file() {
    local path="$1" mode="$2" content
    content="$(cat)"
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '[dry-run] escriure %s (mode %s):\n%s\n' "$path" "$mode" "$content"
        return 0
    fi
    install -d -m 0755 "$(dirname "$path")"
    printf '%s\n' "$content" > "$path.tmp"
    chmod "$mode" "$path.tmp"
    mv -f "$path.tmp" "$path"
}

usage() {
    cat <<EOF
Ús: install.sh [opcions]

  --version vX.Y.Z      instal·la aquesta versió (per defecte, l'última publicada)
  --from-source DIR     instal·la des d'una còpia local del codi (p. ex. «.»)
  --user USUARI         usuari del servei (per defecte: $DEFAULT_USER; p. ex. «media»
                        per compartir fitxers amb Sonarr/Radarr/Jellyfin)
  --port PORT           port web (per defecte: $DEFAULT_PORT)
  --download-dir DIR    carpeta dels vídeos (per defecte: $DEFAULT_DOWNLOAD_DIR)
  --update              actualitza el programa i conserva la configuració
  --uninstall           desinstal·la el servei i el programa (es conserven dades i vídeos)
  --purge               amb --uninstall, esborra també la configuració i l'usuari $DEFAULT_USER
  --dry-run             mostra què faria sense canviar res (no cal ser root)
  -h, --help            aquesta ajuda

Variables: TRESDL_REPO=propietari/3cat-downloader (repositori de GitHub de les versions).
Els vídeos descarregats no s'esborren mai.
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --version) [ $# -ge 2 ] || die "falta el valor de --version"; VERSION="$2"; shift ;;
            --from-source) [ $# -ge 2 ] || die "falta el valor de --from-source"; FROM_SOURCE="$2"; shift ;;
            --user) [ $# -ge 2 ] || die "falta el valor de --user"; SVC_USER="$2"; shift ;;
            --port) [ $# -ge 2 ] || die "falta el valor de --port"; PORT="$2"; shift ;;
            --download-dir) [ $# -ge 2 ] || die "falta el valor de --download-dir"; DOWNLOAD_DIR="$2"; shift ;;
            --update) ACTION="update" ;;
            --uninstall) ACTION="uninstall" ;;
            --purge) PURGE=1 ;;
            --dry-run) DRY_RUN=1 ;;
            -h|--help) usage; exit 0 ;;
            *) die "opció desconeguda: $1 (prova --help)" ;;
        esac
        shift
    done
    if [ -n "$PORT" ]; then
        case "$PORT" in *[!0-9]*) die "el port ha de ser un número" ;; esac
        { [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ]; } || die "el port ha d'estar entre 1 i 65535"
    fi
    if [ -n "$DOWNLOAD_DIR" ]; then
        case "$DOWNLOAD_DIR" in /*) ;; *) die "--download-dir ha de ser una ruta absoluta" ;; esac
    fi
    if [ -n "$VERSION" ]; then
        case "$VERSION" in v*) ;; *) VERSION="v$VERSION" ;; esac
    fi
    if [ "$PURGE" -eq 1 ] && [ "$ACTION" != "uninstall" ]; then
        die "--purge només es pot fer servir amb --uninstall"
    fi
}

ensure_root() {
    [ "$DRY_RUN" -eq 1 ] && return 0
    [ "$(id -u)" -eq 0 ] && return 0
    if [ -f "$0" ] && command -v sudo >/dev/null 2>&1; then
        info "Cal ser root; es torna a executar amb sudo…"
        exec sudo -E bash "$0" "$@"
    fi
    die "executa aquest script com a root (p. ex. «curl … | sudo bash»)"
}

check_os() {
    [ -r /etc/os-release ] || die "no s'ha pogut identificar el sistema (falta /etc/os-release)"
    local id like
    # shellcheck source=/dev/null
    id="$(. /etc/os-release && printf '%s' "${ID:-}")"
    # shellcheck source=/dev/null
    like="$(. /etc/os-release && printf '%s' "${ID_LIKE:-}")"
    case " $id $like " in
        *" debian "*|*" ubuntu "*|*" raspbian "*) ;;
        *) die "sistema no compatible ($id). Fes servir Debian, Ubuntu o Raspberry Pi OS, o bé Docker." ;;
    esac
    command -v systemctl >/dev/null 2>&1 || die "cal systemd (systemctl)"
    case "$(uname -m)" in
        x86_64|aarch64|arm64) ;;
        armv7l|armv6l)
            warn "Sistema de 32 bits ($(uname -m)). Funciona, però es recomana Raspberry Pi OS de 64 bits."
            export PIP_EXTRA_INDEX_URL="https://www.piwheels.org/simple"
            ;;
        *) warn "Arquitectura no provada: $(uname -m)" ;;
    esac
}

install_packages() {
    info "Instal·lant dependències del sistema (python3, venv, ffmpeg)…"
    run env DEBIAN_FRONTEND=noninteractive apt-get update -q
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
        python3 python3-venv python3-pip ffmpeg ca-certificates curl
}

check_python() {
    [ "$DRY_RUN" -eq 1 ] && ! command -v python3 >/dev/null 2>&1 && return 0
    local minor
    minor="$(python3 -c 'import sys; print(sys.version_info[1] if sys.version_info[0] == 3 else 0)')"
    [ "$minor" -ge "$MIN_PY_MINOR" ] || die "cal Python 3.$MIN_PY_MINOR o més nou (tens $(python3 --version 2>&1))"
}

# Value of KEY in the existing env file (empty if missing).
env_value() {
    [ -r "$ENV_FILE" ] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

resolve_settings() {
    # On update, keep what the env file already says unless overridden.
    [ -n "$PORT" ] || PORT="$(env_value TRESDL_PORT)"
    [ -n "$PORT" ] || PORT="$DEFAULT_PORT"
    [ -n "$DOWNLOAD_DIR" ] || DOWNLOAD_DIR="$(env_value TRESDL_DOWNLOAD_DIR)"
    [ -n "$DOWNLOAD_DIR" ] || DOWNLOAD_DIR="$DEFAULT_DOWNLOAD_DIR"
    if [ -z "$SVC_USER" ] && [ -r "$UNIT_FILE" ]; then
        SVC_USER="$(sed -n 's/^User=//p' "$UNIT_FILE" | tail -n 1)"
    fi
    [ -n "$SVC_USER" ] || SVC_USER="$DEFAULT_USER"
}

ensure_user() {
    if id "$SVC_USER" >/dev/null 2>&1; then
        return 0
    fi
    [ "$SVC_USER" = "$DEFAULT_USER" ] || die "l'usuari «$SVC_USER» no existeix (crea'l primer o omet --user)"
    info "Creant l'usuari de sistema $SVC_USER…"
    run useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home \
        --shell /usr/sbin/nologin "$SVC_USER"
}

svc_group() {
    if [ "$DRY_RUN" -eq 1 ] && ! id "$SVC_USER" >/dev/null 2>&1; then
        printf '%s' "$SVC_USER"
    else
        id -gn "$SVC_USER"
    fi
}

prepare_dirs() {
    local group
    group="$(svc_group)"
    run install -d -m 0755 "$PREFIX" "$ETC_DIR"
    run install -d -m 0750 -o "$SVC_USER" -g "$group" "$DATA_DIR"
    if [ ! -d "$DOWNLOAD_DIR" ]; then
        run install -d -m 0775 -o "$SVC_USER" -g "$group" "$DOWNLOAD_DIR"
    elif [ "$DRY_RUN" -eq 0 ] && ! runuser -u "$SVC_USER" -- test -w "$DOWNLOAD_DIR"; then
        warn "l'usuari $SVC_USER no pot escriure a $DOWNLOAD_DIR. Revisa'n els permisos."
    fi
}

package_spec() {
    if [ -n "$FROM_SOURCE" ]; then
        [ -f "$FROM_SOURCE/pyproject.toml" ] || die "$FROM_SOURCE no conté el codi de 3Cat Downloader"
        (cd "$FROM_SOURCE" && pwd)
        return 0
    fi
    case "$REPO" in
        __*__|"") die "indica el repositori amb TRESDL_REPO=propietari/3cat-downloader o fes servir --from-source" ;;
    esac
    local tag="$VERSION"
    if [ -z "$tag" ]; then
        tag="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
            | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)" \
            || die "no s'ha pogut consultar l'última versió de $REPO"
        [ -n "$tag" ] || die "no s'ha trobat cap versió publicada a $REPO"
    fi
    printf 'https://github.com/%s/releases/download/%s/3cat_downloader-%s-py3-none-any.whl' \
        "$REPO" "$tag" "${tag#v}"
}

install_app() {
    local spec
    spec="$(package_spec)"
    info "Instal·lant 3Cat Downloader a $VENV ($spec)…"
    if [ ! -x "$VENV/bin/python" ]; then
        run python3 -m venv "$VENV"
    fi
    run "$VENV/bin/python" -m pip install -q --upgrade pip
    run "$VENV/bin/python" -m pip install -q --upgrade "$spec"
    # Debian's ffmpeg is used: the binary bundled by imageio-ffmpeg is not needed.
    if [ "$DRY_RUN" -eq 0 ]; then
        find "$VENV" -path '*imageio_ffmpeg/binaries/ffmpeg-*' -type f -delete 2>/dev/null || true
    fi
}

write_env_file() {
    if [ -r "$ENV_FILE" ]; then
        info "Es conserva la configuració de $ENV_FILE"
        set_env_value TRESDL_PORT "$PORT"
        set_env_value TRESDL_DOWNLOAD_DIR "$DOWNLOAD_DIR"
        return 0
    fi
    write_file "$ENV_FILE" 0640 <<EOF
# 3Cat Downloader — variables d'entorn del servei (vegeu docs/configuration.md).
# Després de canviar res: sudo systemctl restart $APP
TRESDL_HOST=0.0.0.0
TRESDL_PORT=$PORT
TRESDL_DATA_DIR=$DATA_DIR
TRESDL_DOWNLOAD_DIR=$DOWNLOAD_DIR
TRESDL_FFMPEG=/usr/bin/ffmpeg
# Contrasenya fixa (si no, la crees des del navegador el primer cop):
#TRESDL_PASSWORD=
#TRESDL_LOG_LEVEL=INFO
EOF
    run chgrp "$(svc_group)" "$ENV_FILE"
}

set_env_value() {
    local key="$1" value="$2"
    [ "$(env_value "$key")" = "$value" ] && return 0
    if grep -q "^$key=" "$ENV_FILE"; then
        run sed -i "s|^$key=.*|$key=$value|" "$ENV_FILE"
    else
        if [ "$DRY_RUN" -eq 1 ]; then
            printf '[dry-run] afegir %s=%s a %s\n' "$key" "$value" "$ENV_FILE"
        else
            printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
        fi
    fi
}

# Keep in sync with deploy/linux/3cat-downloader.service (checked by tests).
write_unit() {
    local group
    group="$(svc_group)"
    write_file "$UNIT_FILE" 0644 <<EOF
[Unit]
Description=3Cat Downloader (descàrregues de 3cat)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$group
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/tresdl --no-gui
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0002
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=$DATA_DIR
ReadWritePaths=-$DOWNLOAD_DIR

[Install]
WantedBy=multi-user.target
EOF
}

start_service() {
    run systemctl daemon-reload
    run systemctl enable "$APP.service"
    run systemctl restart "$APP.service"
}

print_summary() {
    local ips="" ip
    if [ "$DRY_RUN" -eq 0 ]; then
        ips="$(hostname -I 2>/dev/null || true)"
    fi
    info "Fet! 3Cat Downloader s'està executant com a servei."
    printf '\n  Obre-ho al navegador:\n'
    if [ -n "$ips" ]; then
        for ip in $ips; do
            case "$ip" in *:*) ;; *) printf '    http://%s:%s/\n' "$ip" "$PORT" ;; esac
        done
    else
        printf '    http://<ip-del-servidor>:%s/\n' "$PORT"
    fi
    cat <<EOF

  El primer cop et demanarà que creïs una contrasenya.
  Vídeos:          $DOWNLOAD_DIR
  Configuració:    $ENV_FILE  (i $DATA_DIR)
  Registres:       journalctl -u $APP -f
  Actualitzar:     torna a executar aquest script amb --update
EOF
}

uninstall() {
    info "Desinstal·lant 3Cat Downloader…"
    if [ -e "$UNIT_FILE" ] || [ "$DRY_RUN" -eq 1 ]; then
        run systemctl disable --now "$APP.service" || true
        run rm -f "$UNIT_FILE"
        run systemctl daemon-reload
    fi
    run rm -rf "$PREFIX"
    if [ "$PURGE" -eq 1 ]; then
        run rm -rf "$DATA_DIR" "$ETC_DIR"
        if id "$DEFAULT_USER" >/dev/null 2>&1; then
            run userdel "$DEFAULT_USER" || true
        fi
        info "S'han esborrat la configuració i l'usuari $DEFAULT_USER."
    else
        info "Es conserven $DATA_DIR i $ETC_DIR (fes servir --purge per esborrar-los)."
    fi
    info "Els vídeos descarregats no s'han tocat."
}

main() {
    parse_args "$@"
    ensure_root "$@"
    if [ "$ACTION" = "uninstall" ]; then
        uninstall
        return 0
    fi
    check_os
    install_packages
    check_python
    resolve_settings
    ensure_user
    prepare_dirs
    install_app
    write_env_file
    write_unit
    start_service
    print_summary
}

main "$@"
