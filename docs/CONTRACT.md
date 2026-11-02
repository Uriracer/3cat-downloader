# 3Cat Downloader — internal contract

Desktop app (Windows first) that downloads whole seasons or whole series from 3cat.cat.
Stack: Python 3.11, FastAPI + uvicorn (local server on 127.0.0.1), vanilla HTML/JS/CSS UI
shown inside a pywebview window, yt-dlp + ffmpeg (from `imageio-ffmpeg`) for downloads.
UI language: Catalan. Code/comments: English.

The local HTTP server is deliberately the core: in the future it will also expose a
Newznab-compatible indexer and a SABnzbd-compatible download-client API so Sonarr/Radarr
can drive it (see `tresdl/arr/README.md`). Nothing in the core may assume a GUI exists.

## Layout

```
run.py                 entry point: start server thread, open pywebview window (fallback: browser)
tresdl/models.py       shared dataclasses (DONE — do not change field names without updating all users)
tresdl/api.py          ThreeCatClient: catalogue, search, URL resolution, episode listing, media info
tresdl/naming.py       filename sanitising + Sonarr/Radarr-style paths
tresdl/config.py       Settings load/save (JSON in %APPDATA%/3CatDownloader/settings.json)
tresdl/downloader.py   DownloadManager: queue, worker threads, yt-dlp, progress, cancel, retry
tresdl/server.py       FastAPI app factory: REST API + static UI
tresdl/instance.py     single-instance lock per data folder (used by run.py)
tresdl/arr/            future Sonarr/Radarr integration (stub + design doc only)
web/index.html, web/app.js, web/style.css
tests/                 pytest; live-network tests marked @pytest.mark.live
```

## Verified 3cat API facts (probed 2026-11-02 from Spain)

All JSON responses are wrapped as `{"resposta": {"status": "OK", ...}}`. Single results are
sometimes an object instead of a list — always normalise with `as_list()`.

1. **Full programme catalogue** (1327 programmes, one request, ~fast):
   `GET https://api.3cat.cat/programestv?_format=json&version=2.0&items_pagina=2000`
   → `resposta.items.lletra[].item[]` each with `id, nombonic, titol, tipologia, entradeta, imatges, ...`.
   Use it for slug→id resolution and offline fuzzy search (cache in memory, ~24 h).
2. **Search**: `GET https://api.3cat.cat/cercador/tot?_format=json&text=<q>&tipologia=PTVC_PROGRAMA&items_pagina=20&pagina=1&sdom=img&version=2.0&https=true&master=yes`
   → `resposta.items.item` (programmes with `id, nombonic, titol`).
3. **Programme detail**: `GET https://api.3cat.cat/programestv/<id>/?_format=json&sdom=img&version=2.0&https=true&master=yes`
   → `resposta.item` with `titol, nombonic, entradeta, info_distribucio, temporades (only current!), imatges`.
4. **Episode listing** (the key one):
   `GET https://api.3cat.cat/videos?_format=json&tipus_contingut=PPD&programatv_id=<id>&items_pagina=1000&pagina=<n>&sdom=img&version=2.0&https=true&master=yes`
   optional `&temporada=PUTEMP_2` filters by season. `resposta.paginacio = {total_items, items_pagina, pagina_actual, total_pagines}`;
   max page size observed ≥1000. Items have `id, titol ("T1xC1 - Els peripatètics"), capitol (absolute), capitol_temporada,
   temporades [{id:"PUTEMP_1", desc:"1a Temporada", main}], data_emissio ("14/09/2015 21:55:08"), durada, entradeta, imatges, permatitle`.
   Merlí (54076) → 40 items, season 2 → 13. Seasons must be derived from the episode list (programme detail only shows current).
   `tipus_contingut=PPD` = full episodes; without it clips/trailers also appear. Telenotícies has 19 411 items (paginate!).
5. **Media info**: `GET https://api-media.3cat.cat/pvideo/media.jsp?media=video&versio=vast&idint=<id>&profile=pc_3cat&format=dm`
   → `informacio {titol, estat.actiu, durada, programa, capitol}`, `media {geo, format, url: [{file, label, active}]}`,
   `subtitols [{text, iso, url, format:"vtt"}]`, `variants [{id:"AUD", media:{url:[...]}}]` (audio description), `imatges`.
   `media.url` labels seen: `"DASH"` (…/stream.mpd, **no DRM**, up to 1920x1080 avc1 + aac) and `"720p"`, `"576p"`, `"480p"` direct MP4.
   MP4 entries may have `active:false` but still download fine (HTTP 200). Prefer DASH for best quality.
   Old CDN hosts must be rewritten (from reference project getTV3Videos):
   `mp4-high-es.ccma.cat.s3.eu-west-1.amazonaws.com/MP4_ALTA_IPTV_ES` → `mp4-down-high-es.3catvideos.cat`, and the same for
   `-int`/`MON`, `medium-es`/`MITJA_WEB_ES`, `medium-int`/`MITJA_WEB_MON`.
   `geo` = `ESPANYA`/`CATALUNYA` means geo-blocked outside that area: report a clear error, never try to bypass.
6. Accepted user inputs (`resolve(text)`):
   - `https://www.3cat.cat/3cat/<slug>/` (also `/tv3/<slug>/`, trailing segments like `/capitols/`) → programme
   - `https://www.3cat.cat/3cat/<slug>/video/<id>/` → single video (also offers its programme)
   - legacy `ccma.cat/tv3/alacarta/<slug>/...` and `.../video/<id>/` forms
   - bare numeric id → try as video id
   - anything else → search text

Never bypass DRM or geo-blocking. If a stream has `ContentProtection`, refuse with a clear message.

## Module APIs (must match exactly)

### tresdl/api.py
```python
class ThreeCatError(Exception): ...
class ThreeCatClient:
    def __init__(self, http: httpx.Client | None = None, timeout: float = 20.0): ...
    def catalogue(self, force: bool = False) -> list[Program]
    def search(self, query: str, limit: int = 30) -> list[Program]      # cercador/tot, fallback: fuzzy over catalogue (accent-insensitive)
    def get_program(self, program_id: int) -> Program
    def program_by_slug(self, slug: str) -> Program | None
    def list_episodes(self, program_id: int, season_id: str | None = None, full_episodes_only: bool = True,
                      *, languages: tuple[str, ...] | None = ("PU_CATALA",)) -> list[Episode]  # all pages, sorted by (season_number, episode_number, air_date)
    def list_seasons(self, episodes: list[Episode]) -> list[Season]     # derived, sorted
    def get_media(self, video_id: int) -> MediaInfo
    def get_episode(self, video_id: int) -> Episode                      # built from media.jsp informacio (for single-video URLs)
    def resolve(self, text: str) -> dict  # {"type": "program", "program": Program} | {"type": "video", "episode": Episode, "program": Program|None} | {"type": "search", "results": list[Program]}
```
Title cleaning: strip a leading `T<s>xC<e> - ` / `Capítol <n> - ` prefix to get `Episode.title`.
Season number: parse digits from `PUTEMP_<n>` or desc `"<n>a Temporada"`. Episodes without season → season_number None.
See "Review fixes" below for the series title, season ids, languages, numbering and season 0 rules.

### tresdl/naming.py
```python
def sanitize(name: str) -> str        # Windows-safe: strip <>:"/\|?*, control chars, trailing dots/spaces, reserved names (CON, NUL...), max 150 chars
def episode_path(root: str, ep: Episode, ext: str = "mp4", template: str | None = None) -> str
```
Default TV layout (Sonarr-importable):
`<root>/<Series>/Season 01/<Series> - S01E01 - <Title>.mp4`;
no season (programme without any season) → `Season 01` with absolute number; season 0 (extra of a programme
with seasons) → Specials; no number at all → `<Series> - <YYYY-MM-DD> - <Title>.mp4` in `<Series>/Specials`… keep it simple and deterministic.
Subtitles next to the video as `<same stem>.ca.vtt` (also convert to `.srt` if setting enabled).

### tresdl/config.py
```python
@dataclass class Settings:
    download_dir: str = str(Path.home() / "Videos" / "3Cat")
    max_parallel: int = 2
    quality: str = "best"            # best|1080|720|576|480|mp4
    subtitles: bool = True
    embed_subtitles: bool = False
    audio_description: bool = False
    skip_existing: bool = True
    filename_template: str = "{series}/Season {season:02d}/{series} - S{season:02d}E{episode:02d} - {title}"
    server_port: int = 8765
    api_key: str = <random hex, generated on first run>   # for future *arr integration
def load_settings() -> Settings
def save_settings(s: Settings) -> None
def app_data_dir() -> Path
```

### tresdl/downloader.py
```python
class DownloadManager:
    def __init__(self, client: ThreeCatClient, settings_getter: Callable[[], Settings]): ...
    def start(self) -> None; def stop(self) -> None
    def enqueue(self, episodes: list[Episode], quality: str | None = None, category: str = "tv") -> list[DownloadJob]  # dedupe: same video id already queued/downloading → skip
    def jobs(self) -> list[DownloadJob]
    def get(self, job_id: str) -> DownloadJob | None
    def cancel(self, job_id: str) -> bool
    def retry(self, job_id: str) -> bool
    def remove(self, job_id: str) -> bool          # only finished/failed/cancelled
    def clear_finished(self) -> int
    def subscribe(self, cb: Callable[[DownloadJob], None]) -> None   # called from worker threads on every change
```
Persist the queue to `app_data_dir()/queue.json` so unfinished jobs survive restarts.
Use yt-dlp as a library (`yt_dlp.YoutubeDL`) on the chosen MediaSource URL, with
`ffmpeg_location=imageio_ffmpeg.get_ffmpeg_exe()`, format selection by height, `merge_output_format=mp4`,
`progress_hooks` for progress, cancellation by raising from the hook. Download to `<path>.part` dir/temporary name, move to final path on success.

### REST API (tresdl/server.py) — all JSON, prefix `/api`
| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/health` | | `{ok, version, ffmpeg}` |
| GET | `/api/search?q=` | | `{results: [Program]}` |
| POST | `/api/resolve` | `{text}` | resolve() dict, serialised |
| GET | `/api/programs/{id}` | | `{program, seasons: [Season], episodes: [Episode]}` |
| GET | `/api/videos/{id}/media` | | `MediaInfo` |
| POST | `/api/downloads` | `{episode_ids?: [int], program_id?: int, season_ids?: [str], quality?: str}` | `{jobs: [DownloadJob]}` (program_id without episode_ids = whole series; with season_ids = those seasons) |
| GET | `/api/downloads` | | `{jobs: [DownloadJob]}` |
| POST | `/api/downloads/{job_id}/cancel` \| `/retry` | | `{ok}` |
| DELETE | `/api/downloads/{job_id}` | | `{ok}` |
| POST | `/api/downloads/clear` | | `{removed}` |
| GET/PUT | `/api/settings` | Settings | Settings |
| POST | `/api/open-folder` | `{path?, job_id?}` | `{ok}` (os.startfile; default download_dir; `job_id` opens that job's own output) |
| POST | `/api/naming/preview` | `{filename_template, download_dir?}` | `{ok, path}` or `{ok: false, error}` (sample episode rendered by `naming`) |
| GET | `/api/events` | | Server-Sent Events: `event: job` data DownloadJob JSON on every change |
Static UI served from `/` (web/). Server binds 127.0.0.1 only.

## Integration notes (verified end-to-end 2026-11-02)

Additions agreed after the modules were built; they extend, not change, the tables above.

- **Errors**: `api.ThreeCatError(message, status=None)` carries the HTTP status in `.status`;
  `api.ThreeCatNotFound` (subclass) is raised for unknown programmes/videos. The server maps
  `status == 404` / `ThreeCatNotFound` to HTTP 404 and every other `ThreeCatError` to 502,
  always as `{"detail": <Catalan message>}`. Both server and downloader recognise these
  classes by name through the MRO (subclasses included), never by importing `api`.
- `get_media(video_id, *, parse_dash=False)`: `True` fills the DASH height from the manifest
  and drops DRM-protected DASH streams.
- `GET /api/health` → `{ok, version, ffmpeg: bool, ffmpeg_path, app: "3cat-downloader"}`.
- `POST /api/downloads` also accepts `category` (`tv`|`movies`) and returns `requested`
  (episodes asked for) next to `jobs` (only the newly created ones; duplicates of pending
  jobs are skipped).
- `DownloadManager` notifications are serialised and always carry the job's latest state,
  so the last `job` SSE event for a job is its final status.
- Cancel/failure removes temp files and the now-empty folders the manager created for that
  job (never pre-existing folders or folders still used by another job). Shutdown keeps both
  so yt-dlp can resume.
- Videos without season in a programme that has **no seasons at all** (news, kids programmes)
  are named per the default rule `Season 01` + absolute number, e.g.
  `Telenotícies - S01E4596 - <title>.mp4`. (Season-less videos of a programme *with*
  seasons are specials: see below.)

## Review fixes (2026-11-02, adversarial review)

Behaviour changes on top of the tables above; all covered by tests.

**api.py**
- Series title (`Episode.program_title`) = the programme's own title (`get_program`, cached
  1 h; catalogue as fallback), identical for every episode of `list_episodes` and for
  `get_episode` when `programa_id` is known. The per-item `programa` field (often a sub-show
  or "Pel·lícula") is only a fallback; `programes_tv` entry with the same id is preferred.
- `get_episode` without `programa_id`: an exact (accent-insensitive) catalogue match of
  `informacio.programa` gives `program_id`/`program_title`; otherwise `programa` is used as the
  series when the video has a season/`T<s>xC<e>` marker and is not a generic label; only then
  the film rule (`program_id = 0`, title = video title) applies.
- An episode whose season is only known from its `T<s>xC<e>` title gets `season_id =
  "PUTEMP_<s>"`, so it joins the real season. `list_seasons` therefore never shows two entries
  with the same number.
- `list_episodes(season_id=...)` filters locally (no `temporada` API param), so such episodes
  are included.
- Language: items whose main `idiomes` id is not in `languages` (default `PU_CATALA`) are
  dropped (dubbed `PU_ANGLES`, `PU_ARANES`, `PU_VOS` copies with the same numbers). Items without
  `idiomes` are kept; if nothing is left the unfiltered list is returned. `None` = no filter.
- In a programme where any episode has a season, season-less episodes get `season_number = 0`
  (`season_id` stays `""`); `list_seasons` names that group "Especials i extres" and sorts it
  last. `get_episode` applies the same rule when the programme is known to have seasons
  (from an earlier listing, or the programme detail's `temporades`).
- Episode number: an explicit `T<s>xC<e>` title prefix wins over `capitol_temporada` (which is
  sometimes the absolute number or off by one); shared by `list_episodes` and `get_episode`.

**naming.py**
- `season_number == 0` → Specials layout (`<Series>/Specials/<Series> - <date> - <title>`).
- `disambiguate(path, video_id)` → `<stem> [<video_id>].<ext>`.
- `validate_template` also requires one of `{episode}`, `{absolute}`, `{title}`, `{id}`.

**downloader.py**
- Two different videos never write the same path: at enqueue, a job whose rendered path is
  already used by an earlier job (queue order) of another video is flagged and saved as
  `... [<id>].mp4`; at run time a path claimed by a running job, or produced by a
  completed/skipped job of another video, gets the same treatment.
- `queue.json` items may carry a private `"_extra"` object (`unique`, `temps`); writes are
  debounced (~0.5 s, compact JSON) and flushed by `enqueue`, `stop` and the start-up reset.
- `refresh_workers()` applies a changed `max_parallel` at once (called by `PUT /api/settings`).
- A user cancel survives `stop()`; cancelling a QUEUED job deletes partial files a shutdown kept.
- Terminal status, control removal and path-claim release are atomic (`retry` + `cancel` races).
- yt-dlp: `socket_timeout` 10 s and `retry_sleep_functions` that honour cancel, so a cancel
  during a network stall takes at most ~one socket timeout.
- Subtitle sidecars are renamed to their final names only after the video is in place.
- An unusable download folder fails the job with a Catalan message.

**server.py**
- `POST /api/downloads` with `episode_ids: []` → 400 "No s'ha seleccionat cap capítol."
- Allowed `Host` names are only `127.0.0.1`, `localhost`, `::1` (no `testserver`).
- Every 422 (request validation or settings) is `{"detail": <Catalan string>}`.
- `download_dir` that goes through an existing file → 422; a missing drive is accepted.

**run.py / instance.py**
- One instance per data folder: an OS lock on `<data_dir>/instance.lock`, real port published
  in `<data_dir>/instance.json`. A second launch shows the running instance (whatever port it
  is on) and never builds a `DownloadManager`. The port fallback is therefore safe.
