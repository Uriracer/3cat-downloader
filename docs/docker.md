# Docker

La imatge `ghcr.io/<usuari>/3cat-downloader` funciona a `linux/amd64` (mini-PC, NAS,
servidors) i `linux/arm64` (Raspberry Pi 4/5 amb sistema de 64 bits).

| Etiqueta | Què és |
|---|---|
| `latest` | última versió publicada |
| `0.2.0`, `0.2` | versió concreta (recomanat si vols controlar les actualitzacions) |

## Docker Compose

Fitxer d'exemple: [deploy/docker-compose.yml](../deploy/docker-compose.yml).

```yaml
services:
  3cat-downloader:
    image: ghcr.io/<usuari>/3cat-downloader:latest
    container_name: 3cat-downloader
    environment:
      PUID: "1000"
      PGID: "1000"
      TZ: Europe/Madrid
    volumes:
      - ./config:/config
      - /srv/media/3cat:/downloads
    ports:
      - "8765:8765"
    restart: unless-stopped
    stop_grace_period: 30s
```

```bash
docker compose up -d
docker compose logs -f 3cat-downloader
```

Obre `http://<ip-del-servidor>:8765/` i crea la contrasenya.

## Volums

| Camí al contenidor | Què hi va |
|---|---|
| `/config` | configuració, cua de descàrregues, registres i clau de sessió |
| `/downloads` | vídeos descarregats (es pot canviar amb `TRESDL_DOWNLOAD_DIR`) |

## Permisos: PUID i PGID

El programa s'executa amb l'usuari `PUID:PGID`. Posa-hi el mateix usuari que fan servir
Sonarr, Radarr i Jellyfin perquè tots puguin llegir i moure els fitxers. Per saber-los,
executa `id` a l'amfitrió:

```bash
$ id
uid=1000(oriol) gid=1000(oriol) ...
```

El contenidor corregeix el propietari de `/config`. A `/downloads` no canvia mai res de
manera recursiva. Si no hi pot escriure, ho diu al registre.

## Actualitzar

```bash
docker compose pull && docker compose up -d
```

Les descàrregues que estaven a mitges es reprenen soles.

## Construir la imatge tu mateix

```bash
git clone https://github.com/<usuari>/3cat-downloader.git
cd 3cat-downloader
docker build -t 3cat-downloader .
```

## Stack complet

[deploy/arr-stack](../deploy/arr-stack/README.md) té un `docker-compose.yml` amb Jellyfin,
Sonarr, Radarr, Prowlarr, Jackett, qBittorrent, Seerr i 3Cat Downloader.

## VPN i restriccions geogràfiques

Molts continguts de 3cat només es serveixen a Espanya. **No posis 3Cat Downloader darrere
d'una VPN estrangera.** Per exemple, si qBittorrent surt per gluetun, no facis servir
`network_mode: service:gluetun` per a aquest contenidor.

Totes les variables d'entorn són a [configuration.md](configuration.md).
