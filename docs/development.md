# Desenvolupament

## Entorn

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe run.py              # finestra d'escriptori
.venv\Scripts\python.exe -m tresdl --host 0.0.0.0 --data-dir .\tmp-data   # mode servidor
```

A Linux, canvia `.venv\Scripts\python.exe` per `.venv/bin/python`.

## Proves

```powershell
.venv\Scripts\python.exe -m pytest -m "not live"   # sense xarxa (és el que fa la CI)
.venv\Scripts\python.exe -m pytest                 # inclou proves contra 3cat (cal ser a Espanya)
```

Algunes proves de desplegament fan servir `bash`, `shellcheck` i `pyyaml`:
`pip install shellcheck-py pyyaml`.

### Prova amb Sonarr i Radarr reals

El workflow [e2e.yml](../.github/workflows/e2e.yml) aixeca la imatge de Docker, Sonarr i
Radarr ([tests/e2e/docker-compose.yml](../tests/e2e/docker-compose.yml)) i els configura com
diu la guia: indexador, client de descàrrega i una correspondència manual. Després comprova
que Sonarr i Radarr troben, descarreguen i **importen** *Dirty Linen* (*Plats bruts*), *The
Simpsons* (*Els Simpson*, per correspondència manual) i *13 Minutes* (*13 minuts per matar
Hitler*).

Com que 3cat només és accessible des d'Espanya, [fake3cat.py](../tests/e2e/fake3cat.py)
executa el servidor real amb un catàleg fix i un vídeo generat amb ffmpeg. TVmaze, TheTVDB i
TMDB sí que són els reals. El workflow s'executa amb cada canvi a `tresdl/` i cada setmana,
per detectar versions noves de Sonarr i Radarr. Si falla, l'anotació de l'error porta la cua,
l'historial i el registre de Sonarr i Radarr.

## Estructura

```
tresdl/api.py          client de l'API de 3cat
tresdl/downloader.py   cua i descàrregues (yt-dlp + ffmpeg)
tresdl/naming.py       noms de fitxer
tresdl/config.py       configuració desada
tresdl/runtime.py      modes escriptori/servidor, variables TRESDL_*, ffmpeg
tresdl/auth.py         contrasenya, sessions, clau d'API
tresdl/server.py       API REST + interfície
tresdl/cli.py          línia d'ordres (tresdl / run.py)
tresdl/web/            interfície (HTML/CSS/JS, sense dependències)
tresdl/arr/            futura integració amb Sonarr/Radarr
Dockerfile, docker/    imatge Docker
deploy/                Docker Compose, stack multimèdia, instal·lador Linux i Windows
docs/dev/              contractes interns entre mòduls
```

## Paquets

```powershell
.venv\Scripts\python.exe -m build                                # dist\*.whl i dist\*.tar.gz
.venv\Scripts\python.exe -m PyInstaller --noconfirm 3CatDownloader.spec   # dist\3CatDownloader\
docker build -t 3cat-downloader .
```

L'instal·lador de Windows es fa amb [Inno Setup 6](https://jrsoftware.org/isinfo.php):

```powershell
ISCC.exe /DMyAppVersion=0.2.0 deploy\windows\3CatDownloader.iss
```

## Publicar una versió

1. Canvia `__version__` a `tresdl/__init__.py` i afegeix la secció a `CHANGELOG.md`.
2. Fes commit i crea l'etiqueta:

   ```bash
   git tag v0.2.0
   git push origin main v0.2.0
   ```

3. GitHub Actions (`.github/workflows/release.yml`) comprova que l'etiqueta coincideix amb la
   versió i passa les proves. Després publica:
   - la imatge `ghcr.io/uriracer/3cat-downloader` (amd64 + arm64), amb les etiquetes `0.2.0`,
     `0.2` i `latest`;
   - l'instal·lador i el zip de Windows, el wheel i l'sdist, i `install.sh` amb el repositori
     ja omplert, tot amb `SHA256SUMS`, a la pàgina de la versió.

La primera vegada, a GitHub, el paquet de GHCR es crea privat. Fes-lo públic a
**Packages → 3cat-downloader → Package settings → Change visibility** perquè `docker pull`
funcioni sense iniciar sessió.
