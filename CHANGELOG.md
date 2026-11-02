# Registre de canvis

Format basat en [Keep a Changelog](https://keepachangelog.com/ca/1.1.0/); versions [SemVer](https://semver.org/lang/ca/).

## [0.2.0] - 2026-11-02

### Afegit
- **Mode servidor** per a la xarxa local (Docker, Raspberry Pi, Proxmox): s'activa en escoltar a una
  adreça que no és local (`--host 0.0.0.0`).
  - Inici de sessió amb contrasenya (es crea des del navegador el primer cop, o amb `TRESDL_PASSWORD`).
  - Clau d'API (`X-Api-Key`) per a Sonarr/Radarr i scripts.
  - Protecció CSRF, límit d'intents d'inici de sessió i galetes de sessió signades.
  - Variables d'entorn `TRESDL_*` (carpeta de descàrregues, port, ffmpeg, registres…).
- **Imatge Docker** multiarquitectura (`linux/amd64`, `linux/arm64`) a GitHub Container Registry, amb
  `PUID`/`PGID`, comprovació de salut i aturada neta.
- Exemple d'**stack complet** amb Jellyfin, Sonarr, Radarr, Prowlarr, Jackett, qBittorrent, Seerr i
  3Cat Downloader (`deploy/arr-stack`).
- **Instal·lador natiu per a Linux** (Debian, Ubuntu, Raspberry Pi OS) com a servei de systemd.
- **Instal·lador de Windows** (Inno Setup) i versió portable en zip.
- Paquet Python instal·lable (`pip install`), amb les ordres `tresdl` i `3cat-downloader`.
- Publicació automàtica amb GitHub Actions en etiquetar una versió.

### Canviat
- La interfície web es troba ara dins del paquet (`tresdl/web`).
- ffmpeg es busca a `TRESDL_FFMPEG`, després el que porta `imageio-ffmpeg` i finalment el del sistema.

## [0.1.0] - 2026-11-02

### Afegit
- Aplicació d'escriptori per a Windows per descarregar temporades i sèries senceres de 3cat.
- Cerca per nom o enllaç, selecció de temporades i capítols, cua de descàrregues en directe.
- Millor qualitat disponible (fins a 1080p), subtítols en català i audiodescripció opcional.
- Noms de fitxer compatibles amb Sonarr/Jellyfin (`Sèrie/Season 01/Sèrie - S01E01 - Títol.mp4`).
