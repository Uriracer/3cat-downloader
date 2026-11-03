# Sonarr / Radarr integration — developer notes

User guide: [docs/sonarr-radarr.md](../../docs/sonarr-radarr.md).

```
Sonarr/Radarr ──(1) search ─▶ /arr/newznab/api          indexer.py + releases.py
              ◀── RSS items, enclosure = /arr/nzb/<token>.nzb
              ──(2) fetch  ─▶ /arr/nzb/<token>.nzb      pseudo-NZB carrying the token
              ──(3) addfile▶ /sabnzbd/api (or /api)     sabnzbd.py → DownloadManager.enqueue(targets=…)
              ──(4) poll   ─▶ mode=queue / history
              ──(5) import the folder reported as history.storage, then history delete (del_files=1)
```

Protocol details were checked against Sonarr's `SabnzbdProxy.cs`, `Sabnzbd.cs`,
`NzbValidationService.cs`, `HttpIndexerBase.cs` and Radarr's `NewznabRequestGenerator.cs`.

## Authentication

Every endpoint requires the API key (`apikey` query parameter, as Sonarr/Radarr send it, or
`X-Api-Key`), also in desktop mode. Errors use each protocol's format: Newznab
`<error code="100" …/>`, SABnzbd `{"status": false, "error": "API Key Incorrect"}`
(Sonarr's test matches these strings). `mode=version` answers without a key, like SABnzbd.

## Indexer (`indexer.py`, `releases.py`)

* `t=caps`: `search` (q), `tv-search` (q, season, ep), `movie-search` (q); categories
  2000/2030/2040 and 5000/5030/5040.
* `t=tvsearch&tvdbid=…|tvmazeid=…` (Sonarr tries ids first): `lookup.py` asks TVmaze for the
  original name, official-site slug and akas (`/lookup/shows?thetvdb=`), and SkyHook for
  Sonarr's title; the programme must **equal** one of those names. Releases are named with
  Sonarr's title (`Genesis.2026.S01E01…`) and carry `<newznab:attr name="tvdbid">`, which
  Sonarr's `ParsingService.FindSeries` accepts when the title differs. Cached 24 h
  (failures 15 min); without TVmaze Sonarr falls back to the title search.
* Title search fallback (Sonarr's title tier, or a Sonarr that cached old caps): when no 3cat
  programme equals `q`, SkyHook's `search/en/?term=q` gives TheTVDB series whose title
  **equals** `q` (year included), then the id path above. "Dirty Linen" -> 84710 -> TVmaze
  "Plats Bruts" -> 3cat `plats-bruts`.
* Numbering (`indexer.remap_numbers`): with a TheTVDB episode list (SkyHook), 3cat
  numbers are kept if all exist on TheTVDB; else matched in order when both sides have the
  same number of regular episodes; else an episode missing on TheTVDB takes the numbers of
  the only TheTVDB episode aired the same day.
* RSS: `programme_series_cached` maps a 3cat programme name to TheTVDB (TVmaze search: same
  name and Catalan language / Catalan network / 3cat official site). Unknown names are
  resolved by a background thread; the feed never waits. Identified episodes get Sonarr's
  title, TheTVDB numbering and `tvdbid`.
* Films: `t=movie&tmdbid=…|imdbid=…` (digits, no `tt`, as Radarr sends it) -> Radarr's
  metadata API (`api.radarr.video/v1/movie/{tmdb}` or `/movie/imdb/tt…`): original title +
  `ca`/`es` translations and alternative titles must equal a 3cat film. Title fallback:
  `/v1/search?q=&year=` with the exact title and year. Releases carry `tmdbid` and `imdb`
  (digits), which Radarr's `ParsingService.FindMovie` matches first.
* Cache: `arr_metadata.json` in the data directory (`[expires, value]` per key, wall clock);
  failures only in memory for 15 min. TVmaze calls are spaced 0.55 s (20 per 10 s limit) and
  429 answers are retried after `Retry-After`.
* Manual correspondences (`mappings.py`, `arr_mappings.json`): 3cat programme/film name ->
  TheTVDB/TMDB id, edited from the settings page (`/api/arr/mappings`, `/api/arr/lookup`,
  session-protected like the rest of `/api`). Their names are tried before the automatic ones;
  a mapped series that TVmaze doesn't know is still described by SkyHook.
* End-to-end test with real Sonarr/Radarr: `tests/e2e/` and `.github/workflows/e2e.yml`.
* Every search is logged at INFO: `Newznab t=… q=… tvdbid=… -> N resultats`.
* `t=tvsearch&q=…[&season=…][&ep=…]`: the programme whose title **equals** `q`
  (accent/punctuation-insensitive, trailing year ignored) — never a fuzzy match, because the
  release is named after `q`. Daily series: `season=YYYY&ep=MM/DD` matches the air date.
  A season search adds a **season pack** first.
* `t=search&q=Title Year` with movie categories (Radarr) and `t=movie`: the film catalogue
  (`ThreeCatClient.films()`, `format_contingut=PUF_PEL`).
* No `q` (RSS sync and the **Test** button, which fails on an empty feed): latest full
  episodes (`recent_episodes()`) or latest films.
* Names: `Series.S01E02.Title.CATALAN.1080p.WEB-DL.AAC.H264-3CAT`; resolution probed once per
  programme (DASH height), size estimated from the duration.
* Token = base64url JSON `{k, t, v, n?, c}`: kind (`e`/`m`), release title, video ids,
  per-video file names (season packs), category.

## Download client (`sabnzbd.py`)

* `version` 4.5.0; `get_config` with `categories` (`*`, `tv`, `movies`, `sonarr`, `radarr`),
  `sorters: []` (Sonarr calls `.Any()` on it), sorting disabled, `history_retention_option: all`.
* `addfile`/`addurl`: decode the token, `get_episode` each video, enqueue with explicit
  targets `<download_dir>/<category>/<release>/<file>.mp4` → `nzo_id` `SABnzbd_nzo_3cat…`.
  Groups (`nzo_id` → job ids) persist in `<data dir>/arr_jobs.json`.
* `queue`: groups with active jobs (Queued / Downloading / Extracting), `mb`, `mbleft`,
  `percentage`, `timeleft` `H:MM:SS`.
* `history`: finished groups; `Completed` if at least one video finished (storage = the
  release folder, whose name equals `name` as Sonarr expects), else `Failed` with the first
  job error as `fail_message`.
* `queue|history&name=delete&value=…&del_files=1`: cancel/remove the jobs and delete the
  folder (only inside the download directory). `retry`: retry failed jobs.
