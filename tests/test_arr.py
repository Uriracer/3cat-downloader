"""Sonarr/Radarr integration: Newznab indexer, pseudo-NZBs, SABnzbd API."""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from test_server import FakeStore, FakeSettings  # tests/ is on sys.path
from tresdl import server
from tresdl.arr import releases as rl
from tresdl.arr.indexer import Indexer, normalize, split_year
from tresdl.models import DownloadJob, Episode, JobStatus, MediaInfo, MediaSource, Program

KEY = "clau-sonarr"
NS = {"newznab": "http://www.newznab.com/DTD/2010/feeds/attributes/"}
BASE = "http://127.0.0.1"


def ep(vid: int, season: Optional[int], number: Optional[int], title: str, *, program_id: int = 54076,
       series: str = "Merlí", date: str = "2015-09-14", absolute: Optional[int] = None) -> Episode:
    return Episode(id=vid, program_id=program_id, program_title=series, title=title,
                   raw_title=title, season_id=f"PUTEMP_{season}" if season else "",
                   season_number=season, episode_number=number, absolute_number=absolute,
                   air_date=date, duration_seconds=3240)


MERLI = [ep(1, 1, 1, "Els peripatètics"), ep(2, 1, 2, "Plató"), ep(3, 2, 1, "Maquiavel")]
GENESI = [ep(20 + n, 1, n, f"Capítol {n}", program_id=76716, series="Gènesi", date="2026-09-21")
          for n in (1, 2, 3)]
TN = [ep(10, None, None, "Telenotícies migdia", program_id=777, series="Telenotícies", date="2026-11-02")]
FILMS = [Episode(id=50, program_id=0, program_title="Monster house", title="Monster house",
                 raw_title="Monster house", air_date="2026-10-24", duration_seconds=5175)]


class Client:
    def __init__(self) -> None:
        self.programs = [Program(id=54076, slug="merli", title="Merlí"),
                         Program(id=76716, slug="genesi", title="Gènesi"),
                         Program(id=99, slug="merli-sapere-aude", title="Merlí. Sapere aude"),
                         Program(id=777, slug="telenoticies", title="Telenotícies")]
        self.episodes = {54076: MERLI, 76716: GENESI, 99: [ep(90, 1, 1, "Altres", program_id=99, series="Merlí. Sapere aude")],
                         777: TN}

    def search(self, query: str, limit: int = 30) -> list[Program]:
        return [p for p in self.programs if normalize(query).split()[0] in normalize(p.title)][:limit]

    def list_episodes(self, program_id: int, season_id=None, full_episodes_only=True) -> list[Episode]:
        return copy.deepcopy(self.episodes.get(program_id, []))

    def get_media(self, video_id: int, parse_dash: bool = False) -> MediaInfo:
        return MediaInfo(video_id=video_id, title="x", available=True,
                         sources=[MediaSource(url="https://x/stream.mpd", kind="dash", label="DASH", height=1080)])

    def films(self, force: bool = False) -> list[Episode]:
        return copy.deepcopy(FILMS)

    def recent_episodes(self, limit: int = 200) -> list[Episode]:
        return copy.deepcopy(MERLI[:1] + TN)

    def get_episode(self, video_id: int) -> Episode:
        for e in MERLI + GENESI + TN + FILMS:
            if e.id == video_id:
                return copy.deepcopy(e)
        raise LookupError(video_id)

    def get_program(self, program_id): ...
    def list_seasons(self, episodes): return []
    def resolve(self, text): return {}


@dataclass
class Manager:
    jobs_by_id: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def subscribe(self, cb): ...
    def start(self): ...
    def stop(self): ...
    def clear_finished(self): return 0

    def enqueue(self, episodes, quality=None, category="tv", targets=None):
        self.calls.append((episodes, category, targets))
        created = []
        for e in episodes:
            job = DownloadJob(id=f"job{len(self.jobs_by_id) + 1}", episode=e, category=category,
                              target=(targets or {}).get(e.id), total_bytes=100 * 1048576)
            self.jobs_by_id[job.id] = job
            created.append(job)
        return created

    def jobs(self): return list(self.jobs_by_id.values())
    def get(self, job_id): return self.jobs_by_id.get(job_id)

    def cancel(self, job_id):
        self.jobs_by_id[job_id].status = JobStatus.CANCELLED
        return True

    def retry(self, job_id):
        self.jobs_by_id[job_id].status = JobStatus.QUEUED
        return True

    def remove(self, job_id):
        return self.jobs_by_id.pop(job_id, None) is not None


TVMAZE = {   # what TVmaze / SkyHook answer, keyed by URL
    "https://api.tvmaze.com/lookup/shows?thetvdb=480821": {
        "id": 94234, "name": "Gènesi", "officialSite": "https://www.3cat.cat/3cat/genesi/",
        "externals": {"thetvdb": 480821}},
    "https://api.tvmaze.com/shows/94234": {
        "id": 94234, "name": "Gènesi", "officialSite": "https://www.3cat.cat/3cat/genesi/",
        "externals": {"thetvdb": 480821}},
    "https://api.tvmaze.com/shows/94234/akas": [],
    "https://skyhook.sonarr.tv/v1/tvdb/shows/en/480821": {"title": "Genesis (2026)"},
    "https://api.tvmaze.com/lookup/shows?thetvdb=82712": {"id": 1, "name": "Genesis", "officialSite": ""},
    "https://api.tvmaze.com/shows/1/akas": [],
    "https://skyhook.sonarr.tv/v1/tvdb/shows/en/82712": {"title": "Genesis (2005)"},
    "https://skyhook.sonarr.tv/v1/tvdb/search/en/?term=Genesis 2026": [
        {"tvdbId": 393542, "title": "Genesis", "firstAired": "2021-01-19"},
        {"tvdbId": 480821, "title": "Genesis (2026)", "firstAired": "2026-09-21"}],
    "https://skyhook.sonarr.tv/v1/tvdb/search/en/?term=Genesis": [
        {"tvdbId": 480821, "title": "Genesis (2026)", "firstAired": "2026-09-21"},
        {"tvdbId": 82712, "title": "Genesis (2005)", "firstAired": "2005-01-01"}],
}


def fake_fetch(url: str, params: dict):
    full = url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")
    if full not in TVMAZE:
        raise AssertionError(f"unexpected request {full}")
    return copy.deepcopy(TVMAZE[full])


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr("tresdl.arr.lookup._default_fetch", fake_fetch)


@pytest.fixture()
def arr(tmp_path: Path):
    client, manager = Client(), Manager()
    store = FakeStore(FakeSettings(download_dir=str(tmp_path / "dl"), api_key=KEY))
    web = tmp_path / "web"
    web.mkdir()
    app = server.create_app(client, manager, store, web_dir=web, arr_data_dir=tmp_path / "data")
    with TestClient(app, base_url=BASE) as http:
        yield {"http": http, "manager": manager, "tmp": tmp_path, "app": app}


def items(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        attrs = {a.get("name"): a.get("value") for a in item.findall("newznab:attr", NS)}
        out.append({**attrs, "title": item.findtext("title"), "link": item.findtext("link"),
                    "size": int(item.find("enclosure").get("length"))})
    return out


def newznab(http: TestClient, **params) -> str:
    r = http.get("/arr/newznab/api", params={"apikey": KEY, **params})
    assert r.status_code == 200
    return r.text


# --------------------------------------------------------------------------- unit

def test_names_and_helpers() -> None:
    assert rl.episode_name("Merlí", season=1, episode=2, title="Plató", height=1080) == \
        "Merli.S01E02.Plato.CATALAN.1080p.WEB-DL.AAC.H264-3CAT"
    assert rl.episode_name("Telenotícies", season=None, episode=None, title="Capítol 3",
                           air_date="2026-11-02", height=720) == "Telenoticies.2026.11.02.CATALAN.720p.WEB-DL.AAC.H264-3CAT"
    assert rl.movie_name("L'avi i la nena", 2020, 576) == "Lavi.i.la.nena.2020.CATALAN.576p.WEB-DL.AAC.H264-3CAT"
    assert rl.season_pack_name("Polseres vermelles", 2, 1080) == "Polseres.vermelles.S02.CATALAN.1080p.WEB-DL.AAC.H264-3CAT"
    assert split_year("Monster house 2006") == ("Monster house", 2006)
    assert split_year("1714") == ("1714", None)
    assert normalize("Merlí: Sapere aude!") == "merli sapere aude"
    assert rl.estimate_size(3600, 1080) > rl.estimate_size(3600, 480) > 0


def test_token_and_nzb_roundtrip() -> None:
    tok = rl.Token(kind="e", title="Merli.S01", videos=[1, 2], names={"1": "a", "2": "b"})
    assert rl.Token.decode(tok.encode()) == tok
    data = rl.build_nzb(tok)
    root = ET.fromstring(data)
    assert root.tag.endswith("nzb") and root.findall("{%s}file" % rl.NZB_NS)  # what Sonarr validates
    assert rl.token_from_nzb(data) == tok
    with pytest.raises(ValueError):
        rl.Token.decode("not-a-token")
    with pytest.raises(ValueError):
        rl.token_from_nzb(b"<nzb><file/></nzb>")


# --------------------------------------------------------------------------- newznab

def test_caps_and_api_key(arr) -> None:
    http = arr["http"]
    caps = ET.fromstring(newznab(http, t="caps"))
    assert caps.find("searching/tv-search").get("supportedParams") == "q,season,ep,tvdbid,tvmazeid"
    assert {c.get("id") for c in caps.iter("category")} == {"2000", "5000"}
    bad = http.get("/arr/newznab/api", params={"t": "caps", "apikey": "dolenta"})
    assert ET.fromstring(bad.text).get("code") == "100"


def test_tvsearch_episode(arr) -> None:
    found = items(newznab(arr["http"], t="tvsearch", q="Merlí", season="1", ep="2", cat="5030,5040"))
    assert [f["title"] for f in found] == ["Merli.S01E02.Plato.CATALAN.1080p.WEB-DL.AAC.H264-3CAT"]
    assert found[0]["season"] == "1" and found[0]["episode"] == "2" and found[0]["category"] == "5040"
    assert found[0]["size"] > 1_000_000 and f"apikey={KEY}" in found[0]["link"]


def test_tvsearch_is_strict_and_season_has_pack(arr) -> None:
    http = arr["http"]
    titles = [f["title"] for f in items(newznab(http, t="tvsearch", q="Merli", season="1"))]
    assert titles[0] == "Merli.S01.CATALAN.1080p.WEB-DL.AAC.H264-3CAT"     # season pack first
    assert len(titles) == 3 and all(t.startswith("Merli.S01") for t in titles)
    assert not any("Altres" in t for t in titles)                          # "Merlí. Sapere aude" not leaked
    assert items(newznab(http, t="tvsearch", q="Merl")) == []               # partial title: nothing
    assert len(items(newznab(http, t="tvsearch", q="Merlí (2015)"))) == 3   # year suffix ignored


def test_tvsearch_by_tvdbid_uses_original_title(arr) -> None:
    """Sonarr calls it "Genesis (2026)" (TheTVDB), 3cat "Gènesi": found through TVmaze."""
    http = arr["http"]
    # Sonarr's title search: TheTVDB "Genesis (2026)" -> TVmaze "Gènesi" -> 3cat.
    by_title = items(newznab(http, t="tvsearch", q="Genesis 2026", season="1", ep="3"))
    assert [(f["title"].split(".CATALAN")[0], f["tvdbid"]) for f in by_title] == [("Genesis.2026.S01E03", "480821")]
    # A plain "Genesis" is another series: never Gènesi, although TheTVDB lists "Genesis (2026)".
    assert items(newznab(http, t="tvsearch", q="Genesis", season="1", ep="1")) == []
    found = items(newznab(http, t="tvsearch", tvdbid="480821", season="1", cat="5030,5040"))
    assert [f["title"] for f in found] == [
        "Genesis.2026.S01.CATALAN.1080p.WEB-DL.AAC.H264-3CAT",
        *[f"Genesis.2026.S01E0{n}.CATALAN.1080p.WEB-DL.AAC.H264-3CAT" for n in (1, 2, 3)]]
    assert {f["tvdbid"] for f in found} == {"480821"}
    one = items(newznab(http, t="tvsearch", tvmazeid="94234", season="1", ep="2"))
    assert [(f["title"].split(".CATALAN")[0], f["tvdbid"]) for f in one] == [("Genesis.2026.S01E02", "480821")]
    assert items(newznab(http, t="tvsearch", tvdbid="82712", season="1", ep="1")) == []   # not on 3cat
    # Merlí by title still works and carries no id.
    assert "tvdbid" not in items(newznab(http, t="tvsearch", q="Merlí", season="1", ep="1"))[0]


def test_series_lookup_caches_and_survives_failures() -> None:
    from tresdl.arr.lookup import SeriesLookup
    calls: list[str] = []

    def fetch(url, params):
        calls.append(url)
        return fake_fetch(url, params)

    lookup = SeriesLookup(fetch)
    info = lookup.find(tvdb_id=480821)
    assert info.names == ["Gènesi", "genesi"] and info.sonarr_title == "Genesis (2026)"
    assert info.tvmaze_id == 94234 and lookup.find(tvdb_id=480821) is info and len(calls) == 3
    assert lookup.find() is None

    def broken(url, params):
        raise OSError("offline")

    assert SeriesLookup(broken).find(tvdb_id=480821) is None


def test_daily_and_rss(arr) -> None:
    http = arr["http"]
    daily = items(newznab(http, t="tvsearch", q="Telenotícies", season="2026", ep="11/02"))
    assert [d["title"] for d in daily] == ["Telenoticies.2026.11.02.Telenoticies.migdia.CATALAN.1080p.WEB-DL.AAC.H264-3CAT"]
    assert len(items(newznab(http, t="tvsearch", cat="5030,5040"))) == 2    # RSS never empty
    assert len(items(newznab(http, t="movie", cat="2000"))) == 1


def test_movie_search(arr) -> None:
    http = arr["http"]
    found = items(newznab(http, t="search", q="Monster House 2006", cat="2000,2040"))
    assert [f["title"] for f in found] == ["Monster.House.2006.CATALAN.1080p.WEB-DL.AAC.H264-3CAT"]
    assert found[0]["category"] == "2040"
    assert items(newznab(http, t="search", q="Monster 2006", cat="2000")) == []


def test_nzb_endpoint(arr) -> None:
    http = arr["http"]
    link = items(newznab(http, t="tvsearch", q="Merlí", season="1", ep="1"))[0]["link"]
    path = link.replace(BASE, "")
    r = http.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-nzb")
    assert rl.token_from_nzb(r.content).videos == [1]
    assert http.get(path.split("?")[0]).status_code == 401


# --------------------------------------------------------------------------- sabnzbd

def sab(http: TestClient, mode: str, **params):
    r = http.get("/sabnzbd/api", params={"mode": mode, "apikey": KEY, "output": "json", **params})
    assert r.status_code == 200
    return r.json()


def add(http: TestClient, token: rl.Token, cat: str = "tv") -> str:
    r = http.post("/sabnzbd/api", params={"mode": "addfile", "cat": cat, "priority": "-100",
                                          "apikey": KEY, "output": "json"},
                  files={"name": (f"{token.title}.nzb", rl.build_nzb(token), "application/x-nzb")})
    data = r.json()
    assert data["status"] is True, data
    return data["nzo_ids"][0]


def test_sab_version_config_and_keys(arr) -> None:
    http = arr["http"]
    assert sab(http, "version")["version"].startswith("4.")
    config = sab(http, "get_config")["config"]
    assert config["sorters"] == [] and config["misc"]["enable_tv_sorting"] is False
    assert {"tv", "movies"} <= {c["name"] for c in config["categories"]}
    assert config["misc"]["complete_dir"] == str(arr["tmp"] / "dl")
    assert sab(http, "fullstatus")["status"]["completedir"] == str(arr["tmp"] / "dl")
    assert http.get("/sabnzbd/api", params={"mode": "get_config", "apikey": "x"}).json()["error"] == "API Key Incorrect"
    assert http.get("/sabnzbd/api", params={"mode": "queue"}).json()["error"] == "API Key Required"
    assert http.get("/api", params={"mode": "version"}).json()["version"]   # URL base empty in Sonarr


def test_sab_full_cycle(arr) -> None:
    http, manager, dl = arr["http"], arr["manager"], arr["tmp"] / "dl"
    title = "Merli.S01E01.Els.Peripatetics.CATALAN.1080p.WEB-DL.AAC.H264-3CAT"
    nzo = add(http, rl.Token(kind="e", title=title, videos=[1]))
    episodes, category, targets = manager.calls[-1]
    folder = dl / "tv" / title
    assert category == "tv" and targets == {1: str(folder / f"{title}.mp4")}

    job = manager.jobs()[0]
    job.status, job.progress, job.eta = JobStatus.DOWNLOADING, 40.0, 75
    slot = sab(http, "queue", category="tv")["queue"]["slots"][0]
    assert slot["nzo_id"] == nzo and slot["status"] == "Downloading" and slot["filename"] == title
    assert slot["percentage"] == "40" and slot["timeleft"] == "0:01:15" and float(slot["mb"]) == 100
    assert sab(http, "history", category="tv")["history"]["slots"] == []

    folder.mkdir(parents=True)
    (folder / f"{title}.mp4").write_bytes(b"x" * 2048)
    job.status = JobStatus.COMPLETED
    assert sab(http, "queue")["queue"]["slots"] == []
    hist = sab(http, "history", category="tv")["history"]["slots"][0]
    assert hist["status"] == "Completed" and hist["storage"] == str(folder) and hist["name"] == title
    assert hist["bytes"] == 2048 and hist["fail_message"] == ""

    assert sab(http, "history", name="delete", value=nzo, del_files="1")["status"] is True
    assert not folder.exists() and sab(http, "history")["history"]["slots"] == []
    assert manager.jobs() == []


def test_sab_failure_pack_and_queue_delete(arr) -> None:
    http, manager, dl = arr["http"], arr["manager"], arr["tmp"] / "dl"
    tok = rl.Token(kind="e", title="Merli.S01.CATALAN.1080p.WEB-DL.AAC.H264-3CAT", videos=[1, 2],
                   names={"1": "Merli.S01E01.A", "2": "Merli.S01E02.B"})
    nzo = add(http, tok, cat="sonarr")
    _eps, category, targets = manager.calls[-1]
    assert category == "sonarr"
    assert targets[2] == str(dl / "sonarr" / tok.title / "Merli.S01E02.B.mp4")
    assert sab(http, "queue")["queue"]["slots"][0]["status"] == "Queued"
    assert sab(http, "queue", name="delete", value=nzo, del_files="1")["status"] is True
    assert sab(http, "queue")["queue"]["slots"] == [] and manager.jobs() == []

    nzo = add(http, rl.Token(kind="m", title="Monster.House.2006.CATALAN.1080p.WEB-DL.AAC.H264-3CAT",
                             videos=[50], category="movies"), cat="movies")
    job = manager.jobs()[0]
    job.status, job.error = JobStatus.FAILED, "Aquest vídeo només es pot veure des d'Espanya."
    hist = sab(http, "history", category="movies")["history"]["slots"][0]
    assert hist["status"] == "Failed" and "Espanya" in hist["fail_message"] and hist["storage"] == ""
    assert sab(http, "retry", value=nzo)["status"] is True and job.status == JobStatus.QUEUED


def test_sab_rejects_foreign_nzb_and_persists(arr, tmp_path: Path) -> None:
    http = arr["http"]
    r = http.post("/sabnzbd/api", params={"mode": "addfile", "apikey": KEY},
                  files={"name": ("x.nzb", b'<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb"><file/></nzb>')})
    assert r.json()["status"] is False and "3Cat" in r.json()["error"]
    nzo = add(http, rl.Token(kind="e", title="Merli.S02E01", videos=[3]))
    saved = (tmp_path / "data" / "arr_jobs.json").read_text(encoding="utf-8")
    assert nzo in saved and "Merli.S02E01" in saved


def test_addurl(arr) -> None:
    http, manager = arr["http"], arr["manager"]
    link = items(newznab(http, t="tvsearch", q="Merlí", season="2", ep="1"))[0]["link"]
    data = sab(http, "addurl", name=link, cat="tv")
    assert data["status"] is True and manager.calls[-1][0][0].id == 3
