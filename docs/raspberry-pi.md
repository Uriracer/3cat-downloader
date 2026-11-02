# Raspberry Pi

Funciona a **Raspberry Pi 4 i 5** amb **Raspberry Pi OS de 64 bits** (Bookworm o Trixie).
La versió de 32 bits també funciona amb la instal·lació nativa, però més lenta i sense
imatge Docker.

## Abans de començar

- Fes servir un **SSD per USB 3** per als vídeos, no la targeta SD. Els vídeos ocupen molt
  (un capítol en 1080p fa entre 0,5 i 2 GB) i la targeta es desgasta.
- Munta el disc sempre al mateix lloc, per exemple `/srv/media`. Afegeix-lo a `/etc/fstab`
  amb `nofail` perquè la Raspberry arrenqui encara que el disc no hi sigui.
- Comprova que el sistema és de 64 bits: `uname -m` ha de dir `aarch64`.

## Opció A: Docker (recomanat)

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # tanca la sessió i torna a entrar
mkdir -p ~/3cat-downloader && cd ~/3cat-downloader
curl -fsSLO https://raw.githubusercontent.com/<usuari>/3cat-downloader/main/deploy/docker-compose.yml
nano docker-compose.yml           # PUID/PGID (ordre «id»), carpeta de vídeos i <usuari>
docker compose up -d
```

Docker baixa sola la imatge `arm64`. Pots fer servir també l'stack complet de
[deploy/arr-stack](../deploy/arr-stack/README.md). Una Raspberry Pi 5 ho aguanta bé, però la
transcodificació de Jellyfin va justa: millor reproduir sense transcodificar.

## Opció B: instal·lació nativa (sense Docker)

```bash
curl -fsSL https://github.com/<usuari>/3cat-downloader/releases/latest/download/install.sh \
  | sudo bash -s -- --download-dir /srv/media/3cat
```

L'script instal·la Python i ffmpeg, crea el servei `3cat-downloader` i en mostra l'adreça.
Si Jellyfin o Sonarr s'executen amb un altre usuari, fes que 3Cat Downloader faci servir el
mateix amb `--user nom-usuari`. Detalls a [linux.md](linux.md).

## Accedir-hi

Obre `http://<ip-de-la-raspberry>:8765/` des de qualsevol dispositiu de casa. Per trobar
l'adreça, executa `hostname -I`.
