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
