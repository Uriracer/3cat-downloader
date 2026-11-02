# Stack multimèdia complet

Un sol `docker-compose.yml` amb tot el necessari per demanar, descarregar, organitzar i
veure sèries i pel·lícules:

| Servei | Port | Per a què serveix |
|---|---|---|
| Jellyfin | 8096 | veure la biblioteca (TV, mòbil, navegador) |
| Seerr | 5055 | demanar sèries i pel·lícules (successor de Jellyseerr/Overseerr) |
| Sonarr | 8989 | gestiona les sèries |
| Radarr | 7878 | gestiona les pel·lícules |
| Prowlarr | 9696 | gestiona els indexadors i els passa a Sonarr/Radarr |
| Jackett | 9117 | alternativa a Prowlarr (opcional, perfil `jackett`) |
| FlareSolverr | 8191 | ajuda Prowlarr amb indexadors protegits per Cloudflare |
| qBittorrent | 8080 | client de torrents |
| 3Cat Downloader | 8765 | sèries i pel·lícules de 3cat |

## Posada en marxa

```bash
cp .env.example .env
nano .env                       # PUID, PGID, TZ, CONFIG_ROOT, DATA_ROOT, GHCR_OWNER
sudo mkdir -p /srv/data/{torrents/{tv,movies},downloads/3cat,media/{tv,movies}}
sudo chown -R "$(id -u):$(id -g)" /srv/data
docker compose up -d
```

Canvia `/srv/data` pel valor de `DATA_ROOT`. Per activar Jackett:
`docker compose --profile jackett up -d`.

## Carpetes

Tots els contenidors veuen les dades al mateix camí, `/data`:

```
/data/torrents/tv, /data/torrents/movies   ← qBittorrent (categories tv i movies)
/data/downloads/3cat                       ← 3Cat Downloader
/data/media/tv, /data/media/movies         ← biblioteques (Sonarr, Radarr, Jellyfin)
```

Com que tot és al mateix disc, Sonarr i Radarr poden fer **enllaços durs**: no dupliquen
espai i el torrent continua compartint-se.

## Configuració inicial (ordre recomanat)

1. **qBittorrent** (`:8080`): la contrasenya temporal surt a `docker compose logs qbittorrent`.
   A Opcions → Descàrregues, posa `/data/torrents` com a carpeta per defecte. Crea les
   categories `tv` i `movies`.
2. **Prowlarr** (`:9696`): afegeix-hi indexadors. A Settings → Apps, afegeix Sonarr
   (`http://sonarr:8989`) i Radarr (`http://radarr:7878`) amb les seves claus d'API. Si un
   indexador ho necessita, afegeix FlareSolverr (`http://flaresolverr:8191`) a Settings →
   Indexers.
3. **Sonarr** (`:8989`): carpeta arrel `/data/media/tv`. Client de descàrrega qBittorrent:
   host `qbittorrent`, port `8080`, categoria `tv`.
4. **Radarr** (`:7878`): el mateix, amb `/data/media/movies` i la categoria `movies`.
5. **Jellyfin** (`:8096`): biblioteques `/data/media/tv` (Sèries) i `/data/media/movies`
   (Pel·lícules).
6. **Seerr** (`:5055`): connecta'l a Jellyfin (`http://jellyfin:8096`), Sonarr i Radarr.
7. **3Cat Downloader** (`:8765`): crea la contrasenya. Les descàrregues van a
   `/data/downloads/3cat`.

Dins de la xarxa de Docker, els serveis es troben pel nom: `sonarr`, `radarr`, `jellyfin`…

## 3Cat Downloader i la biblioteca

Ara com ara, 3Cat Downloader desa les sèries a `/data/downloads/3cat/<Sèrie>/Season XX/`,
amb noms que Sonarr reconeix. Hi ha dues maneres de fer-les arribar a Jellyfin:

- **Manual**: a Sonarr, *Wanted → Manual Import* sobre `/data/downloads/3cat`.
- **Directa**: al servei `3cat-downloader` del `docker-compose.yml`, canvia el volum per
  `${DATA_ROOT}/media/tv:/data/media/tv` i posa `TRESDL_DOWNLOAD_DIR: /data/media/tv`.
  Jellyfin veurà els capítols de seguida, però Sonarr no els gestionarà.

La propera versió afegirà la integració automàtica: Sonarr i Radarr veuran 3Cat Downloader
com un indexador i un client de descàrrega més (vegeu
[tresdl/arr/README.md](../../tresdl/arr/README.md)).

## Avisos

- **VPN**: si poses qBittorrent darrere de gluetun, fes-ho només per a qBittorrent. 3Cat
  Downloader ha de sortir per la connexió de casa (molts continguts només són accessibles
  des d'Espanya).
- **Seerr** s'executa amb `PUID:PGID`: la carpeta `${CONFIG_ROOT}/seerr` ha de ser d'aquest
  usuari.
- Actualitzar-ho tot: `docker compose pull && docker compose up -d`.
