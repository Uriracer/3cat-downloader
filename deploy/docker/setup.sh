#!/usr/bin/env bash
# 3Cat Downloader — instal·lació amb Docker en una ordre (Raspberry Pi 4/5, mini-PC, NAS...).
#
#   curl -fsSL https://raw.githubusercontent.com/Uriracer/3cat-downloader/main/deploy/docker/setup.sh | bash
#
# Opcions (després de «bash -s --»):
#   --dir DIR        on es desa el docker-compose.yml i la configuració (per defecte ~/3cat-downloader)
#   --media DIR      carpeta dels vídeos (per defecte /srv/media/3cat)
#   --port PORT      port web (per defecte 8765)
#   --password TEXT  contrasenya fixa (si no, la crees des del navegador)
#   --tag TAG        versió de la imatge (per defecte latest)
#   --yes            no preguntar res (fa servir els valors per defecte o les opcions)
#   --uninstall      atura i esborra el contenidor (conserva configuració i vídeos)
set -euo pipefail

IMAGE="ghcr.io/uriracer/3cat-downloader"
APP_DIR="${HOME}/3cat-downloader"
MEDIA_DIR="/srv/media/3cat"
PORT="8765"
PASSWORD=""
TAG="latest"
ASSUME_YES=0
UNINSTALL=0
DOCKER=(docker)

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mAvís:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) APP_DIR="${2:?falta el valor de --dir}"; shift ;;
        --media) MEDIA_DIR="${2:?falta el valor de --media}"; shift ;;
        --port) PORT="${2:?falta el valor de --port}"; shift ;;
        --password) PASSWORD="${2:?falta el valor de --password}"; shift ;;
        --tag) TAG="${2:?falta el valor de --tag}"; shift ;;
        --yes|-y) ASSUME_YES=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '2,15p' "$0" 2>/dev/null || true; exit 0 ;;
        *) die "opció desconeguda: $1" ;;
    esac
    shift
done

# Use «sudo docker» while the user is not (yet) in the docker group.
use_sudo_docker() {
    if ! docker info >/dev/null 2>&1 && [ -n "$SUDO" ]; then
        DOCKER=(sudo docker)
    fi
}

# Questions are read from the terminal even when the script comes from «curl | bash».
ask() {
    local prompt="$1" default="$2" answer=""
    if [ "$ASSUME_YES" -eq 1 ] || [ ! -r /dev/tty ]; then
        printf '%s' "$default"
        return
    fi
    printf '%s [%s]: ' "$prompt" "$default" > /dev/tty
    IFS= read -r answer < /dev/tty || true
    printf '%s' "${answer:-$default}"
}

[ "$(id -u)" -ne 0 ] || warn "És millor executar-lo amb el teu usuari (no root): els fitxers seran teus."
command -v sudo >/dev/null 2>&1 || [ "$(id -u)" -eq 0 ] || die "cal sudo"
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

# --------------------------------------------------------------------------- uninstall
if [ "$UNINSTALL" -eq 1 ]; then
    [ -f "$APP_DIR/docker-compose.yml" ] || die "no hi ha cap instal·lació a $APP_DIR"
    use_sudo_docker
    (cd "$APP_DIR" && "${DOCKER[@]}" compose down)
    info "Contenidor aturat i esborrat. Es conserven $APP_DIR/config i els vídeos."
    exit 0
fi

# --------------------------------------------------------------------------- checks
case "$(uname -m)" in
    aarch64|arm64|x86_64|amd64) ;;
    armv7l|armv6l)
        die "sistema de 32 bits ($(uname -m)): no hi ha imatge Docker. Instal·la Raspberry Pi OS de 64 bits o fes servir install.sh (docs/linux.md)." ;;
    *) die "arquitectura no compatible: $(uname -m)" ;;
esac

APP_DIR="$(ask "Carpeta de la instal·lació" "$APP_DIR")"
MEDIA_DIR="$(ask "Carpeta dels vídeos" "$MEDIA_DIR")"
PORT="$(ask "Port web" "$PORT")"
case "$PORT" in ''|*[!0-9]*) die "el port ha de ser un número" ;; esac
case "$MEDIA_DIR" in /*) ;; *) die "la carpeta dels vídeos ha de ser una ruta absoluta" ;; esac

# --------------------------------------------------------------------------- docker
if ! command -v docker >/dev/null 2>&1; then
    info "Instal·lant Docker (get.docker.com)…"
    curl -fsSL https://get.docker.com | $SUDO sh
fi
if [ -n "$SUDO" ] && ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
    info "Afegint $USER al grup docker (efectiu a la propera sessió)…"
    $SUDO usermod -aG docker "$USER"
fi
use_sudo_docker
"${DOCKER[@]}" compose version >/dev/null 2>&1 || die "falta el connector «docker compose» (sudo apt install docker-compose-plugin)"

# --------------------------------------------------------------------------- folders
PUID="$(id -u)"
PGID="$(id -g)"
mkdir -p "$APP_DIR/config"
if [ ! -d "$MEDIA_DIR" ]; then
    info "Creant $MEDIA_DIR…"
    $SUDO mkdir -p "$MEDIA_DIR"
    $SUDO chown "$PUID:$PGID" "$MEDIA_DIR"
fi
if [ ! -w "$MEDIA_DIR" ]; then
    warn "$MEDIA_DIR no és teva; la poso a nom de $(id -un)."
    $SUDO chown "$PUID:$PGID" "$MEDIA_DIR"
fi

# --------------------------------------------------------------------------- compose
COMPOSE="$APP_DIR/docker-compose.yml"
if [ -f "$COMPOSE" ]; then
    cp "$COMPOSE" "$COMPOSE.bak"
    info "Ja hi havia un docker-compose.yml: còpia a docker-compose.yml.bak"
fi
TZ_NAME="$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo Europe/Madrid)"
{
    echo "# Generat per deploy/docker/setup.sh — 3Cat Downloader"
    echo "services:"
    echo "  3cat-downloader:"
    echo "    image: ${IMAGE}:${TAG}"
    echo "    container_name: 3cat-downloader"
    echo "    environment:"
    echo "      PUID: \"${PUID}\""
    echo "      PGID: \"${PGID}\""
    echo "      TZ: ${TZ_NAME:-Europe/Madrid}"
    if [ -n "$PASSWORD" ]; then
        echo "      TRESDL_PASSWORD: \"${PASSWORD//\"/\\\"}\""
    fi
    echo "    volumes:"
    echo "      - ./config:/config"
    echo "      - ${MEDIA_DIR}:/downloads"
    echo "    ports:"
    echo "      - \"${PORT}:8765\""
    echo "    restart: unless-stopped"
    echo "    stop_grace_period: 30s"
} > "$COMPOSE"
[ -z "$PASSWORD" ] || chmod 600 "$COMPOSE"

# --------------------------------------------------------------------------- start
info "Descarregant la imatge i arrencant el contenidor…"
(cd "$APP_DIR" && "${DOCKER[@]}" compose pull && "${DOCKER[@]}" compose up -d)

info "Esperant que respongui…"
ok=0
for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
        ok=1
        break
    fi
    sleep 2
done
if [ "$ok" -ne 1 ]; then
    warn "No respon encara. Mira els registres: cd $APP_DIR && docker compose logs"
    exit 1
fi

printf '\n\033[1;32m3Cat Downloader està en marxa!\033[0m\n\n  Obre-ho al navegador:\n'
for ip in $(hostname -I 2>/dev/null); do
    case "$ip" in *:*) ;; *) printf '    http://%s:%s/\n' "$ip" "$PORT" ;; esac
done
cat <<EOF

  El primer cop et demanarà que creïs una contrasenya.
  Vídeos:        $MEDIA_DIR
  Configuració:  $APP_DIR/config
  Registres:     cd $APP_DIR && docker compose logs -f
  Actualitzar:   cd $APP_DIR && docker compose pull && docker compose up -d
EOF
