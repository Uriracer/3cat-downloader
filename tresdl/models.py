"""Shared data models for 3Cat Downloader.

These dataclasses are the contract between the 3cat API client (api.py),
the download manager (downloader.py) and the HTTP server (server.py).
Every model has a ``to_dict()`` that returns JSON-serialisable data.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


@dataclass
class Program:
    """A 3cat programme (series, show, film collection...)."""
    id: int
    slug: str                      # "nombonic", e.g. "merli"
    title: str
    description: str = ""
    image: Optional[str] = None    # best poster/landscape image URL
    info: str = ""                 # e.g. "Tres temporades disponibles"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Season:
    """A season of a programme as exposed by the episode listing."""
    id: str                        # e.g. "PUTEMP_1"; "" when the programme has no seasons
    number: Optional[int]          # 1, 2, ... parsed from id/desc; None if unknown
    title: str                     # e.g. "1a Temporada"
    episode_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Episode:
    """One downloadable video (episode, film, clip)."""
    id: int                        # video id ("idint" for media.jsp)
    program_id: int
    program_title: str
    title: str                     # clean episode title, e.g. "Els peripatètics"
    raw_title: str                 # title as returned by the API, e.g. "T1xC1 - Els peripatètics"
    season_id: str = ""
    season_number: Optional[int] = None
    episode_number: Optional[int] = None   # number inside the season (capitol_temporada)
    absolute_number: Optional[int] = None  # global number (capitol)
    air_date: Optional[str] = None         # ISO date "YYYY-MM-DD"
    duration_seconds: Optional[int] = None
    description: str = ""
    image: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MediaSource:
    """One concrete stream for a video."""
    url: str
    kind: str                      # "dash" | "hls" | "mp4"
    label: str                     # API label, e.g. "DASH", "720p", "576p"
    height: Optional[int] = None   # known/estimated vertical resolution

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Subtitle:
    url: str
    lang: str                      # ISO code, e.g. "ca"
    label: str
    format: str = "vtt"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MediaInfo:
    """Result of media.jsp for one video."""
    video_id: int
    title: str
    available: bool
    geo: str = ""                  # "TOTS", "ESPANYA", "CATALUNYA"...
    sources: list[MediaSource] = field(default_factory=list)
    subtitles: list[Subtitle] = field(default_factory=list)
    audio_description: list[MediaSource] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RESOLVING = "resolving"        # fetching media info
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"            # file already existed


@dataclass
class DownloadJob:
    id: str                        # uuid4 hex
    episode: Episode
    quality: str = "best"          # "best" | "1080" | "720" | "576" | "480" | "mp4"
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0          # 0..100
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed: Optional[float] = None  # bytes/s
    eta: Optional[int] = None      # seconds
    output_path: Optional[str] = None
    error: Optional[str] = None
    created_at: float = 0.0        # unix time
    finished_at: Optional[float] = None
    category: str = "tv"           # "tv" | "movies" (future Sonarr/Radarr categories)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d
