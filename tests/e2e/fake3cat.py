"""3Cat Downloader with a fake 3cat catalogue, for the end-to-end test with Sonarr and Radarr.

The real 3cat is geo-restricted (and changes every day), so this runs the real
server, download manager, indexer and SABnzbd API over a fixed catalogue whose
videos are one small generated MP4 served from this process. The metadata
services (TVmaze, SkyHook, Radarr's API) are the real ones, like Sonarr's.

    python tests/e2e/fake3cat.py          # then: python tests/e2e/run_e2e.py

Configuration comes from the usual TRESDL_* environment variables (data and
download folders, API key, password, host). ``E2E_VIDEO_SECONDS`` sets the
length of the video: Sonarr and Radarr reject files much shorter than the
episode or film ("sample"), so the default is 12 minutes (a few hundred KB).
"""
from __future__ import annotations

import copy
import logging
import os
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_parents = Path(__file__).resolve().parents
if len(_parents) > 2 and (_parents[2] / "tresdl").is_dir():   # from a checkout; the image has it installed
    sys.path.insert(0, str(_parents[2]))

from tresdl import api, cli  # noqa: E402
from tresdl.arr.releases import normalize  # noqa: E402
from tresdl.models import Episode, MediaInfo, MediaSource, Program  # noqa: E402
from tresdl.runtime import find_ffmpeg  # noqa: E402

log = logging.getLogger("e2e")

VIDEO_PORT = int(os.environ.get("E2E_VIDEO_PORT", "8766"))
VIDEO_SECONDS = int(os.environ.get("E2E_VIDEO_SECONDS", "720"))


def _ep(vid: int, program_id: int, series: str, season: int, number: int, title: str, date: str,
        minutes: int) -> Episode:
    return Episode(id=vid, program_id=program_id, program_title=series, title=title,
                   raw_title=f"T{season}xC{number} - {title}", season_id=f"PUTEMP_{season}",
                   season_number=season, episode_number=number, absolute_number=number,
                   air_date=date, duration_seconds=minutes * 60, description=f"{series}: {title}.")


PROGRAMS = [
    # Found automatically: TVmaze knows "Plats Bruts" (TheTVDB 84710, "Dirty Linen").
    Program(id=126706100, slug="plats-bruts", title="Plats bruts", description="Comèdia de TV3."),
    # Needs a manual correspondence: TheTVDB 71663 is "The Simpsons", TVmaze has no Catalan name.
    Program(id=900001, slug="els-simpson", title="Els Simpson", description="Dibuixos animats."),
]
EPISODES = {
    126706100: [_ep(9101, 126706100, "Plats bruts", 1, 1, "Tinc una idea", "1999-04-19", 27),
                _ep(9102, 126706100, "Plats bruts", 1, 2, "El pis", "1999-04-26", 27)],
    900001: [_ep(9201, 900001, "Els Simpson", 1, 1, "Rostit a la brasa", "1989-12-17", 22),
             _ep(9202, 900001, "Els Simpson", 1, 2, "El geni", "1990-01-14", 22)],
}
# Radarr: TMDB 319337 is "13 Minutes"; its Catalan title is on TMDB, found automatically.
FILMS = [Episode(id=9301, program_id=0, program_title="13 minuts per matar Hitler",
                 title="13 minuts per matar Hitler", raw_title="13 minuts per matar Hitler",
                 air_date="2026-10-20", duration_seconds=114 * 60, description="Georg Elser, 1939.")]


class FakeCatalogue:
    """The parts of ThreeCatClient the server, the indexer and the downloader use."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.video_url = f"http://127.0.0.1:{VIDEO_PORT}/video.mp4"

    def search(self, query: str, limit: int = 30) -> list[Program]:
        wanted = normalize(query)
        return [p for p in PROGRAMS if wanted and wanted in normalize(p.title)][:limit]

    def get_program(self, program_id: int) -> Program | None:
        return next((p for p in PROGRAMS if p.id == program_id), None)

    def list_episodes(self, program_id: int, season_id=None, full_episodes_only=True) -> list[Episode]:
        return copy.deepcopy(EPISODES.get(program_id, []))

    def list_seasons(self, episodes):
        return []

    def get_episode(self, video_id: int) -> Episode:
        for ep in [e for eps in EPISODES.values() for e in eps] + FILMS:
            if ep.id == video_id:
                return copy.deepcopy(ep)
        raise LookupError(video_id)

    def get_media(self, video_id: int, parse_dash: bool = False) -> MediaInfo:
        return MediaInfo(video_id=video_id, title=str(video_id), available=True, geo="TOTS",
                         sources=[MediaSource(url=self.video_url, kind="mp4", label="720p", height=720)])

    def films(self, force: bool = False) -> list[Episode]:
        return copy.deepcopy(FILMS)

    def recent_episodes(self, limit: int = 200) -> list[Episode]:
        return copy.deepcopy([e for eps in EPISODES.values() for e in eps])[:limit]

    def resolve(self, text: str) -> dict:
        return {"kind": "search", "results": self.search(text)}


def make_video(folder: Path) -> Path:
    """A 720p video with sound, ``VIDEO_SECONDS`` long and tiny (1 frame per second)."""
    video = folder / "video.mp4"
    if video.is_file():
        return video
    ffmpeg, error = find_ffmpeg()
    if not ffmpeg:
        raise SystemExit(f"ffmpeg: {error}")
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg, "-nostdin", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=1",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050",
                    "-t", str(VIDEO_SECONDS), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "45",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "16k", "-shortest",
                    "-movflags", "+faststart", str(video)], check=True)
    return video


def serve_video(folder: Path) -> None:
    handler = partial(SimpleHTTPRequestHandler, directory=str(folder))
    handler.log_message = lambda *_args: None   # type: ignore[attr-defined]
    httpd = ThreadingHTTPServer(("127.0.0.1", VIDEO_PORT), handler)
    threading.Thread(target=httpd.serve_forever, name="e2e-video", daemon=True).start()


def main() -> int:
    data = Path(os.environ.get("TRESDL_DATA_DIR") or Path.cwd() / "e2e-data")
    serve_video(make_video(data / "e2e-video").parent)
    api.ThreeCatClient = FakeCatalogue   # type: ignore[misc]  # cli builds the client from here
    return cli.main(["--no-gui"] + sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
