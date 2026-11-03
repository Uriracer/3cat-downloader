"""End-to-end test: real Sonarr and Radarr search, grab and import from 3Cat Downloader.

Expects 3Cat Downloader (``fake3cat.py``), Sonarr and Radarr running (see
``docker-compose.yml`` next to this file, or ``.github/workflows/e2e.yml``)
and configures them exactly as docs/sonarr-radarr.md says:

1. a manual correspondence in 3Cat Downloader ("Els Simpson" = TheTVDB 71663);
2. 3Cat Downloader as indexer (Newznab) and download client (SABnzbd) in
   Sonarr and Radarr: adding them runs Sonarr's/Radarr's own connection tests;
3. Sonarr: "Dirty Linen" (3cat "Plats bruts", found automatically) and
   "The Simpsons" (manual correspondence) S01E01; Radarr: "13 Minutes" (3cat
   "13 minuts per matar Hitler");
4. each search must end with the file imported into the library.

Only the standard library is used. On failure, Sonarr's/Radarr's queue,
history and log are printed, and a summary is reported as a GitHub
annotation.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

TRESDL = os.environ.get("TRESDL_URL", "http://127.0.0.1:8765")
TRESDL_KEY = os.environ.get("TRESDL_API_KEY", "e2e0000000000000000000000000tres")
TRESDL_HOST_FOR_ARR = os.environ.get("E2E_TRESDL_HOST", "tresdl")   # how Sonarr/Radarr reach it
TRESDL_PORT_FOR_ARR = int(os.environ.get("E2E_TRESDL_PORT", "8765"))
SONARR = os.environ.get("SONARR_URL", "http://127.0.0.1:8989")
SONARR_KEY = os.environ.get("SONARR_API_KEY", "e2e00000000000000000000000sonarr")
RADARR = os.environ.get("RADARR_URL", "http://127.0.0.1:7878")
RADARR_KEY = os.environ.get("RADARR_API_KEY", "e2e00000000000000000000000radarr")
TV_ROOT = os.environ.get("E2E_TV_ROOT", "/data/media/tv")
MOVIE_ROOT = os.environ.get("E2E_MOVIE_ROOT", "/data/media/movies")
TIMEOUT = float(os.environ.get("E2E_TIMEOUT", "600"))

SERIES = {84710: "Dirty Linen (Plats bruts)", 71663: "The Simpsons (Els Simpson, manual)"}
MOVIE = 319337   # 13 Minutes (13 minuts per matar Hitler)


class Fail(Exception):
    pass


# --------------------------------------------------------------------------- http

def call(base: str, key: str, method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"X-Api-Key": key, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise Fail(f"{method} {base}{path} -> {exc.code}: {detail[:1500]}") from exc
    return json.loads(text) if text.strip() else None


def wait_until(what: str, check: Callable[[], Any], timeout: float, every: float = 3.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except (Fail, OSError, ValueError) as exc:
            last = exc
        time.sleep(every)
    raise Fail(f"Temps esgotat esperant: {what}" + (f" (últim error: {last})" if last else ""))


def log(message: str) -> None:
    print(f"[e2e {time.strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- *arr setup

class Arr:
    def __init__(self, name: str, base: str, key: str) -> None:
        self.name, self.base, self.key = name, base, key

    def get(self, path: str) -> Any:
        return call(self.base, self.key, "GET", path)

    def post(self, path: str, body: Any) -> Any:
        return call(self.base, self.key, "POST", path, body, timeout=120)

    def put(self, path: str, body: Any) -> Any:
        return call(self.base, self.key, "PUT", path, body)

    def wait_ready(self) -> None:
        wait_until(f"{self.name} en marxa", lambda: self.get("/api/v3/system/status"), 180)
        log(f"{self.name} {self.get('/api/v3/system/status')['version']} en marxa")

    def schema(self, kind: str, implementation: str, values: dict) -> dict:
        item = next(s for s in self.get(f"/api/v3/{kind}/schema") if s["implementation"] == implementation)
        for field in item["fields"]:
            if field["name"] in values:
                field["value"] = values[field["name"]]
        return item

    def setup(self, root: str, category: str, categories: list[int]) -> None:
        if not any(r["path"].rstrip("/") == root for r in self.get("/api/v3/rootfolder")):
            self.post("/api/v3/rootfolder", {"path": root})
        client = self.schema("downloadclient", "Sabnzbd", {
            "host": TRESDL_HOST_FOR_ARR, "port": TRESDL_PORT_FOR_ARR, "useSsl": False, "urlBase": "sabnzbd",
            "apiKey": TRESDL_KEY, "tvCategory": category, "movieCategory": category})
        client.update(name="3Cat Downloader", enable=True, priority=1)
        self.post("/api/v3/downloadclient", client)       # runs the connection test
        log(f"{self.name}: client de descàrrega afegit (prova de connexió superada)")
        indexer = self.schema("indexer", "Newznab", {
            "baseUrl": f"http://{TRESDL_HOST_FOR_ARR}:{TRESDL_PORT_FOR_ARR}/arr/newznab", "apiPath": "/api",
            "apiKey": TRESDL_KEY, "categories": categories})
        indexer.update(name="3Cat Downloader", enableRss=True, enableAutomaticSearch=True,
                       enableInteractiveSearch=True, priority=25)
        self.post("/api/v3/indexer", indexer)             # runs the indexer test (caps + RSS)
        log(f"{self.name}: indexador afegit (prova de l'indexador superada)")

    def quality_profile(self) -> int:
        profiles = self.get("/api/v3/qualityprofile")
        profile = next((p for p in profiles if p["name"] == "Any"), profiles[0])
        if "language" in profile:   # Radarr: "Original" by default, 3cat films are dubbed (docs/sonarr-radarr.md)
            profile["language"] = {"id": -1, "name": "Any"}
            self.put(f"/api/v3/qualityprofile/{profile['id']}", profile)
        return profile["id"]

    def releases(self, query: str) -> str:
        """The releases an interactive search finds, with the reasons they are rejected."""
        try:
            found = call(self.base, self.key, "GET", f"/api/v3/release?{query}", timeout=180)
        except Exception as exc:
            return f"(cerca interactiva: {exc})"
        return "; ".join(f"{r.get('title')} [{r.get('quality', {}).get('quality', {}).get('name')}, "
                         f"{r.get('size')} B] -> {r.get('rejections') or 'acceptat'}" for r in found) or "cap resultat"

    def diagnostics(self) -> str:
        lines = [f"===== {self.name} ====="]
        for title, path in (("Cua", "/api/v3/queue?pageSize=20&includeUnknownSeriesItems=true&includeUnknownMovieItems=true"),
                            ("Historial", "/api/v3/history?pageSize=25&sortKey=date&sortDirection=descending"),
                            ("Registre", "/api/v3/log?pageSize=60&sortKey=time&sortDirection=descending")):
            try:
                records = self.get(path).get("records", [])
            except Exception as exc:   # diagnostics must never hide the real failure
                lines.append(f"{title}: {exc}")
                continue
            lines.append(f"--- {title}")
            for r in records:
                if title == "Cua":
                    lines.append(f"{r.get('title')} | {r.get('status')} | {r.get('trackedDownloadStatus')} | "
                                 f"{r.get('trackedDownloadState')} | {r.get('statusMessages')} | {r.get('errorMessage')}")
                elif title == "Historial":
                    lines.append(f"{r.get('date')} {r.get('eventType')} {r.get('sourceTitle')} {r.get('data', {}).get('message', '')}")
                elif r.get("level") in ("warn", "error", "fatal") or "3Cat" in str(r.get("message")):
                    lines.append(f"{r.get('time')} {r.get('level')} {r.get('logger')}: {r.get('message')} "
                                 f"{(r.get('exception') or '')[:400]}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- steps

def check_tresdl() -> None:
    wait_until("3Cat Downloader en marxa", lambda: call(TRESDL, TRESDL_KEY, "GET", "/api/health"), 120)
    mapping = call(TRESDL, TRESDL_KEY, "POST", "/api/arr/mappings",
                   {"kind": "series", "title": "els simpson", "remote_id": 71663})
    if mapping.get("remote_title") != "The Simpsons":
        raise Fail(f"Correspondència inesperada: {mapping}")
    log(f"3Cat Downloader: correspondència «{mapping['title']}» = «{mapping['remote_title']}»")


def add_series(sonarr: Arr, tvdb_id: int, profile: int) -> int:
    found = sonarr.get(f"/api/v3/series/lookup?term=tvdb:{tvdb_id}")
    if not found:
        raise Fail(f"Sonarr no troba TheTVDB {tvdb_id}")
    series = dict(found[0], qualityProfileId=profile, rootFolderPath=TV_ROOT, monitored=True, seasonFolder=True,
                  addOptions={"monitor": "none", "searchForMissingEpisodes": False,
                              "searchForCutoffUnmetEpisodes": False})
    added = sonarr.post("/api/v3/series", series)
    episode = wait_until(f"capítols de {added['title']}", lambda: next(
        (e for e in sonarr.get(f"/api/v3/episode?seriesId={added['id']}")
         if e["seasonNumber"] == 1 and e["episodeNumber"] == 1), None), 180)
    sonarr.put("/api/v3/episode/monitor", {"episodeIds": [episode["id"]], "monitored": True})
    sonarr.post("/api/v3/command", {"name": "EpisodeSearch", "episodeIds": [episode["id"]]})
    log(f"Sonarr: {added['title']} S01E01 afegit i buscant")
    return episode["id"]


def add_movie(radarr: Arr, profile: int) -> int:
    movie = radarr.get(f"/api/v3/movie/lookup/tmdb?tmdbId={MOVIE}")
    movie.update(qualityProfileId=profile, rootFolderPath=MOVIE_ROOT, monitored=True,
                 minimumAvailability="released", addOptions={"searchForMovie": False})
    added = radarr.post("/api/v3/movie", movie)
    radarr.post("/api/v3/command", {"name": "MoviesSearch", "movieIds": [added["id"]]})
    log(f"Radarr: {added['title']} ({added['year']}) afegida i buscant")
    return added["id"]


def main() -> int:
    sonarr, radarr = Arr("Sonarr", SONARR, SONARR_KEY), Arr("Radarr", RADARR, RADARR_KEY)
    try:
        check_tresdl()
        sonarr.wait_ready()
        radarr.wait_ready()
        sonarr.setup(TV_ROOT, "tv", [5030, 5040])
        radarr.setup(MOVIE_ROOT, "movies", [2030, 2040])
        tv_profile, movie_profile = sonarr.quality_profile(), radarr.quality_profile()
        # label -> (has the file been imported?, what the interactive search says about the releases)
        pending: dict[str, tuple[Callable[[], bool], Callable[[], str]]] = {}
        for tvdb_id, label in SERIES.items():
            episode_id = add_series(sonarr, tvdb_id, tv_profile)
            pending[label] = (lambda eid=episode_id: sonarr.get(f"/api/v3/episode/{eid}")["hasFile"],
                              lambda eid=episode_id: sonarr.releases(f"episodeId={eid}"))
        movie_id = add_movie(radarr, movie_profile)
        pending["13 Minutes (13 minuts per matar Hitler)"] = (
            lambda: radarr.get(f"/api/v3/movie/{movie_id}")["hasFile"],
            lambda: radarr.releases(f"movieId={movie_id}"))

        deadline = time.monotonic() + TIMEOUT
        while pending and time.monotonic() < deadline:
            for label, (done, _releases) in list(pending.items()):
                try:
                    if done():
                        log(f"IMPORTAT: {label}")
                        del pending[label]
                except Fail as exc:
                    log(f"{label}: {exc}")
            time.sleep(5)
        if pending:
            why = "\n".join(f"{label}: {releases()}" for label, (_done, releases) in pending.items())
            raise Fail("No s'han importat: " + ", ".join(pending) + "\n" + why)
    except Fail as exc:
        report = "\n\n".join(arr.diagnostics() for arr in (sonarr, radarr))
        print(report, flush=True)
        if os.environ.get("GITHUB_ACTIONS"):
            summary = f"{exc}\n{report}"[:6000].replace("%", "%25").replace("\r", "").replace("\n", "%0A")
            print(f"::error title=Prova amb Sonarr i Radarr::{summary}", flush=True)
        log(f"ERROR: {exc}")
        return 1
    log("Tot correcte: Sonarr i Radarr han trobat, descarregat i importat els tres llançaments.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
