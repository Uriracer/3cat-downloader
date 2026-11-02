# Sonarr / Radarr integration — design (not implemented yet)

Status: **design only**. `router.py` holds endpoint skeletons that answer
HTTP 501 and are **not mounted** in the app. Nothing here changes the current
behaviour of 3Cat Downloader.

## Goal

Let Sonarr (series) and Radarr (films) use 3Cat Downloader exactly like a
Usenet stack, without any Usenet involved:

```
Sonarr/Radarr ──(1) search──▶ /arr/newznab/api      (our Newznab-compatible indexer)
              ◀── RSS items, enclosure = /arr/nzb/<token>.nzb
              ──(2) fetch ──▶ /arr/nzb/<token>.nzb   (pseudo-NZB: metadata only)
              ──(3) send  ──▶ /sabnzbd/api?mode=addfile|addurl  (our SABnzbd-compatible client)
                              └─▶ DownloadManager.enqueue(...)  (normal yt-dlp download)
              ──(4) poll  ──▶ /sabnzbd/api?mode=queue / history
              ──(5) import the finished file from the category folder
```

The 3cat side reuses the existing core unchanged: `ThreeCatClient`
(search, catalogue, `list_episodes`) and `DownloadManager` (queue, progress,
`category` field on `DownloadJob` is already there for this purpose).

## Authentication and network exposure

* Every `/arr/*` and `/sabnzbd/*` request must carry `apikey=<settings.api_key>`
  (generated on first run, shown in the settings screen). Compare with
  `secrets.compare_digest`. Errors use each protocol's own format:
  Newznab `<error code="100" description="Incorrect user credentials"/>`,
  SABnzbd `{"status": false, "error": "API Key Incorrect"}`.
* Today the server binds **127.0.0.1 only** and `LocalGuardMiddleware` rejects
  unknown `Host` headers. Sonarr/Radarr on the same machine work as is. For
  Sonarr in Docker / another host we will add opt-in settings
  `listen_host` (e.g. `0.0.0.0`) and `allowed_hosts`; when bound to a non-loopback
  address, the UI and `/api/*` must also require the API key (or stay loopback-only).
* Headless use: `run.py --no-gui` already runs the server without a window.

## Newznab indexer — `/arr/newznab/api`

Sonarr/Radarr settings: *Indexers → Newznab*, URL `http://127.0.0.1:8765/arr/newznab`,
API path `/api`, API key = `settings.api_key`, categories `5000,5030,5040` (TV) /
`2000,2030,2040` (Movies).

### `t=caps`

```xml
<caps>
  <server version="1.0" title="3Cat Downloader"/>
  <limits max="100" default="100"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,season,ep,tvdbid"/>
    <movie-search available="yes" supportedParams="q"/>
  </searching>
  <categories>
    <category id="5000" name="TV"><subcat id="5030" name="SD"/><subcat id="5040" name="HD"/></category>
    <category id="2000" name="Movies"><subcat id="2030" name="SD"/><subcat id="2040" name="HD"/></category>
  </categories>
</caps>
```

### `t=tvsearch&q=&season=&ep=`

1. Map the query to a 3cat programme (see *Title mapping* below).
2. `client.list_episodes(program_id)` → filter by `season`/`ep`
   (`Episode.season_number` / `Episode.episode_number`; programmes without seasons
   use `absolute_number` as the episode number of season 1, same rule as `naming.py`).
3. Return one RSS `<item>` per episode and, when only `season` is given, one extra
   **season pack** item for the whole season.
4. Empty `q` (Sonarr RSS sync / connectivity test): return the latest episodes of
   programmes that have mappings (or an empty but valid feed).

### `t=movie&q=` and `t=search&q=`

Search programmes (`client.search`) whose episode listing is a single long
video (films / documentaries) and return one item each, category 2000.

### Item format

```xml
<item>
  <title>Merli.S02E03.El.Pensament.CATALAN.1080p.WEB-DL.AAC.H264-3CAT</title>
  <guid isPermaLink="false">3cat-v6293447</guid>
  <pubDate>Mon, 14 Sep 2015 21:55:08 +0200</pubDate>         <!-- Episode.air_date -->
  <enclosure url="http://127.0.0.1:8765/arr/nzb/v6293447.nzb?apikey=…" length="…" type="application/x-nzb"/>
  <newznab:attr name="category" value="5040"/>
  <newznab:attr name="size" value="…"/>                       <!-- estimate: duration × ~5 Mbit/s -->
  <newznab:attr name="season" value="2"/>
  <newznab:attr name="episode" value="3"/>
  <newznab:attr name="tvdbid" value="…"/>                     <!-- when the mapping knows it -->
</item>
```

* The release title uses the **title Sonarr searched for** (so its parser matches
  the series), `SxxEyy`, the language tag `CATALAN`, the resolution of the best
  source (`1080p` for DASH, else the MP4 label) and the group `3CAT`.
* `size` must be > 0 (Sonarr rejects 0-byte releases); estimate it from
  `duration_seconds`.
* Only items whose media is available and not geo-blocked for the user should be
  returned when that is cheap to know; otherwise the download fails with a clear
  message in the queue/history (never bypass DRM or geo-blocking).

### Tokens and `/arr/nzb/<token>.nzb`

| Token | Meaning |
|---|---|
| `v<video-id>` | one episode or film (`v6293447`) |
| `s<program-id>-<season-id>` | season pack (`s54076-PUTEMP_2`) |

The `.nzb` is a syntactically valid NZB (Sonarr checks for an `<nzb>` root with at
least one `<file>`) that only carries our token:

```xml
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
  <head><meta type="x-3cat-token">v6293447</meta><meta type="name">Merli.S02E03…</meta></head>
  <file poster="3cat" date="0" subject="v6293447"><groups><group>3cat</group></groups>
    <segments><segment bytes="1" number="1">v6293447@3cat</segment></segments></file>
</nzb>
```

It never references Usenet; only our own SABnzbd endpoint understands it.

## SABnzbd-compatible download client — `/sabnzbd/api`

Sonarr/Radarr settings: *Download Clients → SABnzbd*, host `127.0.0.1`, port
`8765`, URL base `sabnzbd`, API key = `settings.api_key`, category `tv` (Sonarr)
or `movies` (Radarr). All responses are JSON (`output=json`).

| `mode` | Behaviour |
|---|---|
| `version` | `{"version": "4.3.0"}` (Sonarr/Radarr require a recent SABnzbd) |
| `get_config` | `{"config": {"misc": {"complete_dir": <download_dir>, "download_dir": …}, "categories": [{"name":"*","dir":""},{"name":"tv","dir":"tv"},{"name":"movies","dir":"movies"}]}}` |
| `fullstatus` | `{"status": {"completedir": <download_dir>, …}}` (newer Sonarr versions ask for it) |
| `addfile` (POST, multipart `name`) / `addurl` (`name=<nzb url>`) | Read the token from the NZB, expand it to episodes (`list_episodes` for season packs), `manager.enqueue(episodes, category=cat)`; reply `{"status": true, "nzo_ids": ["3cat_<group-id>"]}` |
| `queue` | `{"queue": {"slots": [{nzo_id, filename, cat, status, mb, mbleft, percentage, timeleft}], "paused": false, …}}` |
| `queue&name=delete&value=<nzo_id>` | cancel and remove the jobs of that group |
| `history` | `{"history": {"slots": [{nzo_id, name, category, status, storage, fail_message, bytes, completed}]}}` |
| `history&name=delete&value=<nzo_id>[&del_files=1]` | remove finished jobs (and files if asked) |

* **Groups**: one `nzo_id` = one Sonarr release = one or more `DownloadJob`s (a
  season pack is several). The server keeps a persistent map
  `nzo_id → [job ids]` (e.g. `app_data_dir()/arr_groups.json`) and aggregates
  progress/status: Downloading while any job is active, Completed when all are
  completed/skipped, Failed if any failed (with the first error as `fail_message`).
* **Status mapping**: `queued→Queued`, `resolving/downloading→Downloading`,
  `postprocessing→Extracting`, `completed/skipped→Completed`, `failed/cancelled→Failed`.

## Categories and folders

* `tv` → `<download_dir>/tv/<release title>/…`, `movies` → `<download_dir>/movies/<release title>/…`.
* Jobs created through the *arr API get a dedicated folder per release so
  Sonarr/Radarr's *Completed Download Handling* can import (move/hardlink) and
  then remove it; `history.storage` reports that folder. Downloads started from
  the UI keep the normal library layout (`<Series>/Season 01/…`).
* If Sonarr runs in Docker, the user adds a *Remote Path Mapping* in Sonarr.

## Title mapping (TVDB / TMDB ↔ 3cat programme)

Sonarr searches with the series title from TVDB (often the Catalan title, but it
may be a Spanish/English one) and optionally `tvdbid`.

1. **User overrides** (highest priority), stored in `app_data_dir()/arr_mappings.json`
   and editable from a future settings tab:
   `{"tvdb:280961": {"program_id": 54076, "season_offset": 0, "absolute": false}}`
   and title aliases `{"title:merli": "merli"}` (normalised title → programme slug).
2. **Exact match** of the accent-insensitive, punctuation-free title against the
   cached catalogue (`client.catalogue()`, ~1300 programmes: `title` and `slug`).
3. **Search** (`client.search`) and accept the first result only if its normalised
   title equals the query or the similarity is very high; otherwise return no
   results and log the miss so the user can add an override.
4. Remember successful `tvdbid → program_id` matches in the mappings file.

Season numbering differences (3cat `PUTEMP_n` vs TVDB) are handled by
`season_offset` / `absolute` in the override (absolute numbering uses
`Episode.absolute_number`).

## Settings to add when implementing

`arr_enabled: bool = False`, `listen_host: str = "127.0.0.1"`,
`allowed_hosts: list[str]`, `arr_tv_dir: str = "tv"`, `arr_movies_dir: str = "movies"`.
The existing `api_key` is reused.

## Implementation checklist

1. Implement `router.py` (Newznab XML with `xml.etree`, SABnzbd JSON), with an
   `apikey` dependency and the group map.
2. Mount it in `tresdl/server.create_app` (`app.include_router(router)`) **before**
   the static UI mount, only when `settings.arr_enabled`.
3. Tests with recorded Sonarr/Radarr request fixtures (caps, tvsearch, addfile,
   queue, history, delete) and the fake client/manager from `tests/test_server.py`.
4. Manual test with a real Sonarr v4 / Radarr v5 instance ("Test" buttons for
   the indexer and the download client, then an interactive search).
