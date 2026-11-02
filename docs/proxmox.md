# Proxmox (mini-PC)

Objectiu: un mini-PC amb Proxmox VE que fa de centre multimèdia: Jellyfin, Sonarr, Radarr,
Seerr, Prowlarr, Jackett, qBittorrent i 3Cat Downloader, tot automatitzat.

## Arquitectura recomanada

```
Proxmox VE (mini-PC)
├── Emmagatzematge de dades (disc o ZFS)  →  /tank/data   (torrents, 3cat, biblioteques)
└── LXC "media" (Debian 13, sense privilegis, nesting=1)
    ├── Docker + Docker Compose
    └── deploy/arr-stack: jellyfin, sonarr, radarr, prowlarr, (jackett), qbittorrent,
        seerr, 3cat-downloader   — tots veuen /data
```

Un únic contenidor LXC amb Docker és el més senzill de mantenir. Totes les aplicacions
comparteixen la mateixa carpeta `/data`, de manera que Sonarr i Radarr poden importar al
moment, amb enllaços durs, el que baixen qBittorrent o 3Cat Downloader. Si prefereixes més
aïllament, fes servir una VM Debian en lloc del LXC: els passos dins són els mateixos.

## 1. Preparar l'emmagatzematge a l'amfitrió

```bash
# A la consola de Proxmox (exemple amb un dataset ZFS; amb un disc ext4 seria un directori)
zfs create tank/data
mkdir -p /tank/data/{torrents/{tv,movies},downloads/3cat,media/{tv,movies}}
```

## 2. Crear el contenidor LXC

- Plantilla: Debian 13 (Trixie).
- Sense privilegis («Unprivileged container»), amb **Opcions → Features: nesting=1, keyctl=1**
  (necessari per a Docker).
- 2-4 nuclis, 4 GB de RAM, 16 GB de disc per al sistema i les configuracions.

Afegeix-hi la carpeta de dades, a `/etc/pve/lxc/<ID>.conf`:

```
mp0: /tank/data,mp=/data
```

### Permisos en un LXC sense privilegis

Dins d'un LXC sense privilegis, l'usuari 1000 correspon a l'usuari **101000** de l'amfitrió
(el desplaçament és 100000). Per tant, a l'amfitrió:

```bash
chown -R 101000:101000 /tank/data
```

Dins del LXC i a `.env` fes servir `PUID=1000` i `PGID=1000`.

## 3. Docker i l'stack

Dins del LXC:

```bash
apt update && apt install -y curl git
curl -fsSL https://get.docker.com | sh
git clone https://github.com/Uriracer/3cat-downloader.git /opt/3cat-downloader
cd /opt/3cat-downloader/deploy/arr-stack
cp .env.example .env
nano .env        # DATA_ROOT=/data, CONFIG_ROOT=/opt/media/config, GHCR_OWNER=uriracer
docker compose up -d
```

Ports: Jellyfin 8096, Sonarr 8989, Radarr 7878, Prowlarr 9696, qBittorrent 8080,
Seerr 5055, 3Cat Downloader 8765 (Jackett 9117 i FlareSolverr 8191 si els actives).
La configuració inicial de cada aplicació és a [deploy/arr-stack](../deploy/arr-stack/README.md).

## Transcodificació per maquinari (Jellyfin)

Si el mini-PC té gràfica Intel o AMD, passa `/dev/dri` al LXC:

```
# /etc/pve/lxc/<ID>.conf  (Proxmox VE 8.2 o posterior)
dev0: /dev/dri/renderD128,gid=<GID>
```

`<GID>` és el número del grup `render` dins del LXC. Per saber-lo, executa
`getent group render` dins del LXC. Després descomenta `devices` al servei `jellyfin` del
`docker-compose.yml`.

## Alternativa sense Docker

Pots crear un LXC Debian per a cada aplicació, per exemple amb els scripts de la comunitat
de Proxmox (community-scripts). En aquest cas, crea un altre LXC Debian per a 3Cat
Downloader i instal·la'l amb [install.sh](linux.md):

```bash
curl -fsSL https://github.com/Uriracer/3cat-downloader/releases/latest/download/install.sh \
  | bash -s -- --download-dir /data/downloads/3cat
```

Munta-hi la mateixa carpeta de dades (`mp0`), igual que als altres LXC.

## Còpies de seguretat

Programa una còpia del LXC a **Datacenter → Backup** (vzdump, mode «snapshot»). Exclou-ne
la carpeta de dades (`mp0` sense «Backup»): els vídeos es poden tornar a descarregar, la
configuració no.

## VPN

Si qBittorrent surt per una VPN, fes-ho només per a qBittorrent, per exemple amb gluetun.
3Cat Downloader ha de sortir per la connexió de casa, perquè molts continguts només són
accessibles des d'Espanya.

## Accés des de fora de casa

No obris els ports al router. Fes servir [Tailscale](https://tailscale.com/) o WireGuard per
connectar-te a la xarxa de casa.
