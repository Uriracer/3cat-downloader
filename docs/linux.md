# Linux natiu (servei de systemd)

Per a Debian 12/13, Ubuntu 22.04 o posterior i Raspberry Pi OS. Cal Python 3.11 o més nou,
que ja porten totes aquestes versions.

## Instal·lar

```bash
curl -fsSL https://github.com/Uriracer/3cat-downloader/releases/latest/download/install.sh | sudo bash
```

Opcions útils (van després de `bash -s --`):

```bash
curl -fsSL https://github.com/Uriracer/3cat-downloader/releases/latest/download/install.sh \
  | sudo bash -s -- --user media --download-dir /srv/media/3cat --port 8765
```

| Opció | Per defecte | Què fa |
|---|---|---|
| `--user USUARI` | `tresdl` (es crea) | usuari del servei; fes servir el de Jellyfin/Sonarr per compartir fitxers |
| `--download-dir DIR` | `/srv/media/3cat` | carpeta dels vídeos |
| `--port PORT` | `8765` | port web |
| `--version vX.Y.Z` | última | versió concreta |
| `--from-source DIR` | | instal·la des d'una còpia del codi (p. ex. `git clone`) |
| `--dry-run` | | mostra què faria sense canviar res |

## Què instal·la

| Què | On |
|---|---|
| Programa (entorn virtual de Python) | `/opt/3cat-downloader/venv` |
| Variables d'entorn del servei | `/etc/3cat-downloader/3cat-downloader.env` |
| Configuració, cua i registres | `/var/lib/3cat-downloader` |
| Servei | `/etc/systemd/system/3cat-downloader.service` |

## Fer-lo servir

```bash
sudo systemctl status 3cat-downloader
journalctl -u 3cat-downloader -f          # registres en directe
sudo systemctl restart 3cat-downloader    # després de canviar el fitxer .env
```

Obre `http://<ip>:8765/`. El primer cop crearàs la contrasenya.

## Actualitzar

```bash
curl -fsSL https://github.com/Uriracer/3cat-downloader/releases/latest/download/install.sh \
  | sudo bash -s -- --update
```

Es conserven el fitxer `.env`, la configuració i la cua.

## Desinstal·lar

```bash
curl -fsSL https://github.com/Uriracer/3cat-downloader/releases/latest/download/install.sh \
  | sudo bash -s -- --uninstall           # conserva la configuració
#                       --uninstall --purge   # també esborra configuració i usuari tresdl
```

Els vídeos no s'esborren mai.

## Des del codi font

```bash
git clone https://github.com/Uriracer/3cat-downloader.git
cd 3cat-downloader
sudo ./deploy/linux/install.sh --from-source .
```
