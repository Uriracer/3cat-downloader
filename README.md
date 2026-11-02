# 3Cat Downloader

Descarrega **temporades senceres o sèries completes** de [3cat](https://www.3cat.cat/),
no només capítol a capítol. Funciona com a aplicació d'escriptori a Windows i com a servei
a casa (Docker, Raspberry Pi, Proxmox) al costat de Jellyfin, Sonarr i Radarr.

- Enganxa un enllaç de 3cat o cerca pel nom de la sèrie.
- Tria la sèrie sencera, unes temporades o capítols solts.
- Cua de descàrregues en directe, amb cancel·lació, reintents i represa després d'un reinici.
- Millor qualitat disponible (fins a 1080p), subtítols en català i audiodescripció opcional.
- Fitxers a punt per a Jellyfin i Sonarr: `Sèrie/Season 01/Sèrie - S01E01 - Títol.mp4`.
- Mode servidor amb contrasenya i clau d'API, accessible des del mòbil o l'ordinador de casa.

## Instal·lació

> A les ordres i enllaços, substitueix `<usuari>` pel nom d'usuari de GitHub on hi ha
> publicat aquest repositori.

| On | Com | Guia |
|---|---|---|
| Windows | Instal·lador `3CatDownloader-x.y.z-setup.exe` o zip portable de [Releases](../../releases) | [docs/windows.md](docs/windows.md) |
| Docker (qualsevol servidor) | `ghcr.io/<usuari>/3cat-downloader` per a amd64 i arm64 | [docs/docker.md](docs/docker.md) |
| Raspberry Pi 4/5 | Docker o instal·lació nativa (Raspberry Pi OS de 64 bits) | [docs/raspberry-pi.md](docs/raspberry-pi.md) |
| Proxmox (mini-PC) | Contenidor LXC amb Docker i tot l'stack multimèdia | [docs/proxmox.md](docs/proxmox.md) |
| Linux natiu | `install.sh` crea un servei de systemd | [docs/linux.md](docs/linux.md) |
| Python | `pip install 3cat_downloader-x.y.z-py3-none-any.whl` | [docs/development.md](docs/development.md) |

### Inici ràpid amb Docker

```bash
mkdir -p ~/3cat-downloader && cd ~/3cat-downloader
curl -fsSLO https://raw.githubusercontent.com/<usuari>/3cat-downloader/main/deploy/docker-compose.yml
# edita PUID/PGID, la carpeta de vídeos i <usuari>
docker compose up -d
```

Obre `http://<ip-del-servidor>:8765/`. El primer cop et demanarà que creïs una contrasenya.

Per a l'stack complet (Jellyfin, Sonarr, Radarr, Prowlarr, Jackett, qBittorrent, Seerr i
3Cat Downloader) mira [deploy/arr-stack](deploy/arr-stack/README.md).

## Configuració

Totes les opcions i variables d'entorn (`TRESDL_*`) són a
[docs/configuration.md](docs/configuration.md).

## Full de ruta

- **Integració amb Sonarr i Radarr**: 3Cat Downloader es presentarà com un indexador
  (Newznab) i un client de descàrrega (compatible amb SABnzbd). Així, quan demanis una sèrie
  a Seerr o a Sonarr, si és a 3cat es descarregarà automàticament. El disseny és a
  [tresdl/arr/README.md](tresdl/arr/README.md).

## Avís legal

- Només descarrega **contingut que 3cat ofereix lliurement i gratuïtament** a la seva web.
- **No elude cap protecció**: no trenca DRM ni restriccions geogràfiques. Molts continguts
  només es poden veure des d'Espanya. Si el servidor surt a internet per una VPN estrangera,
  aquests vídeos fallaran.
- És per a **ús personal i privat**. Els continguts són propietat de la CCMA i dels seus
  titulars de drets: no els redistribueixis.
- Projecte independent, sense cap relació amb 3cat ni amb la CCMA. Llicència [MIT](LICENSE).
