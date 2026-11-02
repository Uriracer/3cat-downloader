# Configuració

## Modes

| Mode | Quan | Inici de sessió | Accés |
|---|---|---|---|
| **Escriptori** | escolta a `127.0.0.1` (Windows, per defecte) | no | només el mateix ordinador |
| **Servidor** | escolta a una altra adreça, p. ex. `0.0.0.0` (Docker, Linux, Raspberry Pi) | sí | tota la xarxa de casa |

En mode servidor:

- La primera persona que obre la pàgina crea la contrasenya. Si hi ha gent a la xarxa en
  qui no confies, defineix-la abans amb `TRESDL_PASSWORD`.
- Sonarr, Radarr o scripts poden entrar amb la **clau d'API**, a la capçalera `X-Api-Key`.
  La clau és a Configuració.
- El botó «Obre la carpeta» no hi és, perquè la carpeta és al servidor.

## Variables d'entorn

Totes són opcionals. Les opcions de la línia d'ordres tenen prioritat sobre les variables, i
les variables tenen prioritat sobre la configuració desada.

| Variable | Per defecte | Què fa |
|---|---|---|
| `TRESDL_HOST` | `127.0.0.1` (Docker: `0.0.0.0`) | adreça on escolta; qualsevol que no sigui local activa el mode servidor |
| `TRESDL_PORT` | `8765` | port web. En mode servidor, si està ocupat el programa s'atura amb un error |
| `TRESDL_DATA_DIR` | `%APPDATA%\3CatDownloader`, `~/.3catdownloader` (Docker: `/config`) | configuració, cua, registres i clau de sessió |
| `TRESDL_DOWNLOAD_DIR` | Configuració (Docker: `/downloads`) | carpeta dels vídeos; si és definida, no es pot canviar des de la pàgina |
| `TRESDL_PASSWORD` | | contrasenya fixa; no es desa enlloc i no es pot canviar des de la pàgina |
| `TRESDL_API_KEY` | es genera sola | clau d'API fixa |
| `TRESDL_AUTH` | `auto` | `auto` (contrasenya només en mode servidor), `required` o `none` (sense contrasenya: no ho facis) |
| `TRESDL_ALLOWED_HOSTS` | qualsevol | noms acceptats a la capçalera `Host`, separats per comes (p. ex. `nas.local,192.168.1.20`) |
| `TRESDL_TRUST_PROXY` | `0` | `1` si hi ha un proxy invers davant (fa cas de `X-Forwarded-For` i `X-Forwarded-Proto`) |
| `TRESDL_FFMPEG` | automàtic | ruta d'ffmpeg. Si no hi és, es busca el que porta el programa i després el del sistema |
| `TRESDL_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO` o `WARNING` |
| `PUID`, `PGID`, `UMASK`, `TZ` | `1000`, `1000`, `022`, `Europe/Madrid` | només Docker: usuari, grup, permisos i zona horària |

## Línia d'ordres

```
tresdl [--no-gui] [--host ADREÇA] [--port PORT] [--data-dir CARPETA] [--debug] [--version]
```

A Windows, l'executable `3CatDownloader.exe` accepta les mateixes opcions.

## Opcions de la pàgina de Configuració

| Opció | Què fa |
|---|---|
| Carpeta de descàrrega | on es desen els vídeos |
| Plantilla de nom de fitxer | per defecte `{series}/Season {season:02d}/{series} - S{season:02d}E{episode:02d} - {title}` |
| Descàrregues simultànies | de 1 a 8 |
| Qualitat per defecte | millor disponible, 1080p, 720p, 576p, 480p o MP4 directe |
| Ometre existents | no torna a baixar un capítol si el fitxer ja hi és |
| Subtítols, incrustar-los, audiodescripció | |

## Proxy invers

Si vols un nom com `https://3cat.casa.lan`, posa-hi davant Caddy, Nginx Proxy Manager o
Traefik i defineix `TRESDL_TRUST_PROXY=1`. L'aplicació ha d'anar a l'arrel del domini; no
funciona dins d'una subcarpeta com `/3cat`. Desactiva la memòria intermèdia per a
`/api/events`, que són esdeveniments en directe. El programa ja envia `X-Accel-Buffering: no`.

## Seguretat

- No obris el port a internet. Per entrar des de fora, fes servir Tailscale o WireGuard.
- La clau d'API dona accés complet: no la comparteixis.
- Per canviar la contrasenya: Configuració → Seguretat. Es tancaran les altres sessions.
- Si oblides la contrasenya, atura el programa. Després esborra la línia `password_hash` de
  `settings.json`, dins de la carpeta de dades, i torna'l a arrencar.
