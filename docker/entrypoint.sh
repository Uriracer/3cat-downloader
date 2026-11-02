#!/bin/sh
# 3Cat Downloader container entrypoint.
#
# Runs the app as PUID:PGID (default 1000:1000) so the files it writes have the
# same owner as the rest of your media stack (Sonarr, Radarr, Jellyfin...).
# If the container is started with --user, it just runs as that user.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"
DATA_DIR="${TRESDL_DATA_DIR:-/config}"
DOWNLOAD_DIR="${TRESDL_DOWNLOAD_DIR:-/downloads}"

log() { printf '[entrypoint] %s\n' "$*" >&2; }

case "$PUID$PGID" in
    *[!0-9]*) log "PUID i PGID han de ser números (PUID=$PUID PGID=$PGID)."; exit 1 ;;
esac

umask "$UMASK"
export HOME="$DATA_DIR"

if [ "$(id -u)" != "0" ]; then
    # Started with --user: nothing to adjust.
    exec tresdl --no-gui "$@"
fi

# Make sure PGID and PUID exist in /etc/group and /etc/passwd (needed for
# --init-groups); reuse existing entries (e.g. GID 100 "users").
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd --non-unique --gid "$PGID" tresdl
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd --non-unique --uid "$PUID" --gid "$PGID" --home-dir "$DATA_DIR" \
        --no-create-home --shell /usr/sbin/nologin tresdl
fi

mkdir -p "$DATA_DIR" "$DOWNLOAD_DIR"
# The config folder is small and ours: fix its ownership. The download folder
# may be a large shared media tree: never chown it recursively.
chown -R "$PUID:$PGID" "$DATA_DIR"
if [ -z "$(ls -A "$DOWNLOAD_DIR" 2>/dev/null)" ]; then
    chown "$PUID:$PGID" "$DOWNLOAD_DIR" 2>/dev/null || true
fi
if ! setpriv --reuid="$PUID" --regid="$PGID" --init-groups test -w "$DOWNLOAD_DIR"; then
    log "ATENCIÓ: l'usuari $PUID:$PGID no pot escriure a $DOWNLOAD_DIR."
    log "Revisa els permisos de la carpeta de l'amfitrió o ajusta PUID/PGID."
fi

log "Executant com a $PUID:$PGID (umask $UMASK)"
exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups tresdl --no-gui "$@"
