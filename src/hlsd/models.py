from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit, urlunsplit


class DownloadMode(str, Enum):
    REALTIME = "realtime"
    WINDOW = "window"
    ACCUMULATED = "accumulated"
    SEGMENTS = "segments"


class RequestState(str, Enum):
    SCHEDULED = "scheduled"
    RESOLVING = "resolving"
    ACTIVE = "active"
    FINALIZING = "finalizing"
    DONE = "done"
    STOPPED = "stopped"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    RequestState.DONE,
    RequestState.STOPPED,
    RequestState.FAILED,
    RequestState.CANCELLED,
}


class Track(str, Enum):
    VIDEO = "v"
    AUDIO = "a"


def strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))


@dataclass
class RequestTemplate:
    """How to contact the HLS server (URL + credentials extracted from cURL)."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    auth: tuple[str, str] | None = None

    def resource_key(self, selectors: Selectors) -> str:
        raw = "|".join(
            [
                strip_query(self.url).lower(),
                self.method.upper(),
                str(selectors.video_order),
                str(selectors.video_height),
                str(selectors.video_bandwidth),
                selectors.audio_selector or "",
            ]
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass
class Selectors:
    video_order: int | None = None
    video_height: int | None = None
    video_bandwidth: int | None = None
    audio_selector: str = "auto"


@dataclass
class SegmentMeta:
    source_key: str
    track: Track
    seq: int
    uri: str
    extinf: float
    size: int
    fetched_mono: float = field(default_factory=time.monotonic)
    fetched_wall: float = field(default_factory=time.time)
    pdt: float | None = None  # absolute start (EXT-X-PROGRAM-DATE-TIME)
    path: str | None = None
    is_init: bool = False

    @property
    def key(self) -> tuple[str, Track, int]:
        return (self.source_key, self.track, self.seq)


@dataclass
class DownloadSpec:
    template: RequestTemplate
    selectors: Selectors = field(default_factory=Selectors)
    mode: DownloadMode = DownloadMode.WINDOW
    duration: float | None = None
    start_at: float | None = None
    volatile: bool = False
    name: str = ""
    viewer: bool = False
    concurrency: int | None = None
