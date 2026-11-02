# 3Cat Downloader

Aplicació d'escriptori per descarregar **temporades senceres o sèries completes** de
[3cat](https://www.3cat.cat/), no només capítol a capítol.

- Enganxa un enllaç de 3cat (programa o vídeo), un número de vídeo o simplement cerca pel nom.
- Tria la sèrie sencera, unes temporades concretes o capítols solts.
- Cua de descàrregues amb progrés en temps real, cancel·lació i reintents. Les
  descàrregues pendents es recuperen si tanques el programa.
- Qualitat màxima disponible (fins a 1080p) o una resolució concreta, amb subtítols en català.
- Fitxers organitzats a l'estil de Sonarr/Radarr:
  `Sèrie/Season 01/Sèrie - S01E01 - Títol.mp4`.
- Preparat per integrar-se en el futur amb **Sonarr i Radarr** (vegeu
  [`tresdl/arr/README.md`](tresdl/arr/README.md)).

## Com funciona

El programa arrenca un petit servidor local (només accessible des del mateix ordinador,
a `http://127.0.0.1:8765`) i mostra la interfície en una finestra pròpia. Les descàrregues
es fan amb [yt-dlp](https://github.com/yt-dlp/yt-dlp) i [ffmpeg](https://ffmpeg.org/)
(inclòs automàticament amb `imageio-ffmpeg`).

## Requisits

- Windows 10/11 (també hauria de funcionar a Linux i macOS des del codi font).
- [Python 3.11](https://www.python.org/downloads/) o posterior, si l'executes des del codi font.
- Microsoft Edge WebView2 Runtime (ja ve instal·lat a Windows 11). Si no hi és, el programa
  s'obre al navegador.

## Instal·lació (des del codi font)

```powershell
cd 3cat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Execució

```powershell
.venv\Scripts\python.exe run.py
```

Opcions:

| Opció | Què fa |
|---|---|
| `--no-gui` | Només el servidor, sense finestra (útil per a un ús sense pantalla, p. ex. amb Sonarr/Radarr en el futur). La interfície continua disponible a `http://127.0.0.1:8765` des del navegador. Atura'l amb Ctrl+C. |
| `--port 9000` | Usa un altre port. Si està ocupat, se'n tria un de lliure automàticament. |
| `--debug` | Registre detallat i eines de desenvolupament a la finestra. |

Si el programa ja s'està executant, una segona execució simplement obre la mateixa
finestra (no es creen dues cues de descàrregues).

La configuració, la cua i els registres es desen a `%APPDATA%\3CatDownloader\`
(`settings.json`, `queue.json`, `logs\`). Per defecte els vídeos es desen a
`Vídeos\3Cat` (es pot canviar a la pantalla de configuració).

## Proves

```powershell
.venv\Scripts\python.exe -m pytest -m "not live"   # sense xarxa
.venv\Scripts\python.exe -m pytest                 # inclou proves contra els servidors de 3cat
```

## Crear l'executable (.exe)

```powershell
.venv\Scripts\pyinstaller.exe 3CatDownloader.spec --noconfirm --clean
```

El resultat és la carpeta `dist\3CatDownloader\`; executa `3CatDownloader.exe`. Per
distribuir-lo, comprimeix tota la carpeta (inclou la interfície i ffmpeg).

## Estructura

```
run.py                 punt d'entrada (servidor + finestra)
tresdl/api.py          client de l'API de 3cat
tresdl/downloader.py   cua i descàrregues (yt-dlp + ffmpeg)
tresdl/naming.py       noms de fitxer i carpetes
tresdl/config.py       configuració
tresdl/server.py       API REST local + interfície
tresdl/arr/            futura integració amb Sonarr/Radarr (disseny)
web/                   interfície (HTML/CSS/JS)
```

## Avís legal

- Aquest programa només descarrega **contingut que 3cat ofereix lliurement i de forma
  gratuïta** a la seva web.
- **No elude cap protecció**: no trenca DRM ni restriccions geogràfiques. Si un vídeo
  està protegit o només es pot veure des de Catalunya/Espanya i no hi ets, el programa
  mostra un error i no el descarrega.
- És per a **ús personal i privat**. Els continguts són propietat de la Corporació
  Catalana de Mitjans Audiovisuals (CCMA) i dels seus titulars de drets; no els
  redistribueixis.
- Projecte independent, sense cap relació amb 3cat ni amb la CCMA.
