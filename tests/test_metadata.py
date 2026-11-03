"""Jellyfin metadata (.nfo and pictures) next to the downloaded videos."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from test_downloader import final, install_fake_ytdlp, make_ep, make_manager, settings  # noqa: F401 (fixtures)
from tresdl import metadata, naming
from tresdl.config import Settings
from tresdl.models import JobStatus, Program

POSTER = b"\xff\xd8\xff poster"


def fetch(url: str) -> bytes:
    if "trencada" in url:
        raise OSError("404")
    return POSTER + url.encode()


def test_episode_nfo_numbers_match_the_file_name() -> None:
    ep = make_ep(7, 3, description=" Una classe sobre Plató. ", duration_seconds=3240)
    root = ET.fromstring(metadata.episode_nfo(ep))
    assert root.tag == "episodedetails"
    assert {c.tag: c.text for c in root} == {
        "title": "Capítol 3", "showtitle": "Merlí", "season": "1", "episode": "3", "aired": "2015-09-14",
        "plot": "Una classe sobre Plató.", "runtime": "54", "uniqueid": "7", "studio": "3Cat"}
    assert root.find("uniqueid").attrib == {"type": "3cat", "default": "true"}

    absolute = make_ep(8, season_number=None, episode_number=None, absolute_number=12)
    assert ET.fromstring(metadata.episode_nfo(absolute)).findtext("episode") == "12"   # like "S01E12"
    special = make_ep(9, season_number=None, episode_number=None, absolute_number=None)
    nfo = ET.fromstring(metadata.episode_nfo(special))
    assert nfo.findtext("season") == "0" and nfo.find("episode") is None


def test_series_dir_follows_the_template(tmp_path: Path) -> None:
    ep = make_ep(1)
    assert naming.series_dir(str(tmp_path), ep) == str(tmp_path / "Merlí")
    assert naming.series_dir(str(tmp_path), ep, "{series} - {title}") is None           # no series folder
    assert naming.series_dir(str(tmp_path), ep, "Sèries/{series}/{title}") is None
    assert naming.series_dir(str(tmp_path), make_ep(1, program_title="A: B"), "{series}/{title}") == \
        str(tmp_path / "A - B")


def test_writes_once_and_survives_missing_pictures(tmp_path: Path) -> None:
    series = tmp_path / "Merlí"
    video = series / "Season 01" / "Merlí - S01E01 - Capítol 1.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    program = Program(id=54076, slug="merli", title="Merlí", description="Un professor de filosofia.",
                      image="https://img/merli.jpg")
    ep = make_ep(1, image="https://img/trencada.jpg")
    written = metadata.write_episode_metadata(str(video), ep, series_dir=str(series), program=program,
                                              fetch_image=fetch)
    assert sorted(Path(p).name for p in written) == ["Merlí - S01E01 - Capítol 1.nfo", "poster.jpg", "tvshow.nfo"]
    assert (series / "poster.jpg").read_bytes() == POSTER + b"https://img/merli.jpg"
    show = ET.fromstring((series / "tvshow.nfo").read_bytes())
    assert show.findtext("title") == "Merlí" and show.findtext("plot") == "Un professor de filosofia."
    assert not list(tmp_path.rglob(".*.tmp"))

    (series / "tvshow.nfo").write_text("<tvshow><title>Editat</title></tvshow>", encoding="utf-8")
    ep2 = make_ep(1, image="https://img/c1.jpg")
    again = metadata.write_episode_metadata(str(video), ep2, series_dir=str(series), program=program,
                                            fetch_image=fetch)
    assert [Path(p).name for p in again] == ["Merlí - S01E01 - Capítol 1-thumb.jpg"]   # only what was missing
    assert "Editat" in (series / "tvshow.nfo").read_text(encoding="utf-8")            # never overwritten


class ProgramClient:
    def __init__(self) -> None:
        self.programs: list[int] = []

    def get_media(self, video_id: int):
        from test_downloader import make_media
        return make_media(video_id)

    def get_program(self, program_id: int) -> Program:
        self.programs.append(program_id)
        return Program(id=program_id, slug="merli", title="Merlí", image="https://img/merli.jpg")


def test_downloads_get_metadata_but_arr_releases_do_not(make_manager, settings: Settings, tmp_path: Path) -> None:
    client = ProgramClient()
    mgr = make_manager(client)
    mgr.fetch_image = fetch
    install_fake_ytdlp(mgr)
    target = tmp_path / "videos" / "tv" / "Merli.S01E03.REL" / "Merli.S01E03.REL.mp4"
    jobs = mgr.enqueue([make_ep(1, 1, image="https://img/c1.jpg"), make_ep(2, 2)])
    arr = mgr.enqueue([make_ep(3, 3)], targets={3: str(target)})
    mgr.start()
    for job in jobs + arr:
        assert final(mgr, job.id).status == JobStatus.COMPLETED
    series = Path(settings.download_dir) / "Merlí"
    season = series / "Season 01"
    assert (season / "Merlí - S01E01 - Capítol 1.nfo").is_file()
    assert (season / "Merlí - S01E01 - Capítol 1-thumb.jpg").is_file()
    assert (season / "Merlí - S01E02 - Capítol 2.nfo").is_file()
    assert (series / "tvshow.nfo").is_file() and (series / "poster.jpg").is_file()
    assert 1 <= len(client.programs) <= 2                     # not once per episode
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]   # Sonarr/Radarr: video only


def test_metadata_can_be_turned_off(make_manager, settings: Settings) -> None:
    settings.metadata_files = False
    mgr = make_manager(ProgramClient())
    install_fake_ytdlp(mgr)
    job = mgr.enqueue([make_ep(1)])[0]
    mgr.start()
    assert final(mgr, job.id).status == JobStatus.COMPLETED
    assert [p.suffix for p in Path(settings.download_dir).rglob("*") if p.is_file()] == [".mp4"]
