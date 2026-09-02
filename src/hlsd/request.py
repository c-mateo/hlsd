"""DownloadRequest: segment window with a completion condition.

The request downloads nothing: it receives SegmentMeta from the Source as
new segments are fetched (after its activation) and evaluates its
completion condition according to the mode:

- REALTIME: strict clock since activation.
- WINDOW: clock since the first received segment (surplus is downloaded and
  trimmed at assembly).
- ACCUMULATED: sum of EXTINF of the assigned segments (the result lasts the
  requested duration even with gaps).
- SEGMENTS: fixed count of video segments (`duration` = N segments;
  any surplus arriving before completion is discarded at assembly).

duration=None => indefinite, runs until the user stops it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from .models import DownloadMode, DownloadSpec, RequestState, SegmentMeta, Track


@dataclass
class RequestStats:
    segments_video: int = 0
    segments_audio: int = 0
    accumulated_duration: float = 0.0
    bytes_downloaded: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments_video": self.segments_video,
            "segments_audio": self.segments_audio,
            "accumulated_duration": round(self.accumulated_duration, 3),
            "bytes_downloaded": self.bytes_downloaded,
        }


class DownloadRequest:
    def __init__(self, spec: DownloadSpec, request_id: str | None = None):
        self.id = request_id or uuid.uuid4().hex[:12]
        self.spec = spec
        self.state = RequestState.RESOLVING
        self.created_at = time.time()
        self.activated_mono: float | None = None
        self.activated_wall: float | None = None
        self.first_segment_mono: float | None = None
        self.segments: dict[Track, list[SegmentMeta]] = {Track.VIDEO: [], Track.AUDIO: []}
        self.stats = RequestStats()
        self.error: str | None = None
        self.result_path: str | None = None
        self.stop_requested: bool = False

    # -- lifecycle -----------------------------------------------------------
    def activate(self) -> None:
        self.activated_mono = time.monotonic()
        self.activated_wall = time.time()
        self.state = RequestState.ACTIVE

    def assign_segment(self, meta: SegmentMeta) -> bool:
        if self.state != RequestState.ACTIVE or self.activated_mono is None:
            return False
        if meta.fetched_mono < self.activated_mono:
            return False
        if any(m.seq == meta.seq for m in self.segments[meta.track]):
            return False
        self.segments[meta.track].append(meta)
        if meta.track is Track.VIDEO and self.first_segment_mono is None:
            self.first_segment_mono = meta.fetched_mono
        if meta.track is Track.VIDEO:
            self.stats.segments_video += 1
            self.stats.accumulated_duration += meta.extinf
        else:
            self.stats.segments_audio += 1
        self.stats.bytes_downloaded += meta.size
        return True

    def request_stop(self) -> None:
        self.stop_requested = True

    # -- completion condition (on the video track) ---------------------------
    def is_satisfied(self) -> bool:
        if self.state != RequestState.ACTIVE:
            return True
        if self.stop_requested:
            return True
        duration = self.spec.duration
        if duration is None:
            return False
        if self.spec.mode is DownloadMode.SEGMENTS:
            return self.stats.segments_video >= duration
        now = time.monotonic()
        if self.spec.mode is DownloadMode.REALTIME:
            return self.activated_mono is not None and (now - self.activated_mono) >= duration
        if self.spec.mode is DownloadMode.WINDOW:
            return self.first_segment_mono is not None and (now - self.first_segment_mono) >= duration
        return self.stats.accumulated_duration >= duration

    def time_remaining(self) -> float | None:
        duration = self.spec.duration
        if duration is None:
            return None
        if self.spec.mode is DownloadMode.SEGMENTS:
            return max(0.0, duration - self.stats.segments_video)
        if self.spec.mode is DownloadMode.REALTIME and self.activated_mono is not None:
            return max(0.0, duration - (time.monotonic() - self.activated_mono))
        if self.spec.mode is DownloadMode.WINDOW and self.first_segment_mono is not None:
            return max(0.0, duration - (time.monotonic() - self.first_segment_mono))
        if self.spec.mode is DownloadMode.ACCUMULATED:
            return max(0.0, duration - self.stats.accumulated_duration)
        return duration

    def target_trim(self) -> float | None:
        """Exact duration to trim the assembly to (None = no trim)."""
        if self.spec.duration is None:
            return None
        if self.spec.mode in (DownloadMode.WINDOW, DownloadMode.REALTIME):
            return self.spec.duration
        if self.spec.mode is DownloadMode.ACCUMULATED:
            if self.stats.accumulated_duration > self.spec.duration:
                return self.spec.duration
            return None
        return None

    def target_segment_count(self) -> int | None:
        """Exact count of video segments to assemble (None = no cap)."""
        if self.spec.mode is DownloadMode.SEGMENTS and self.spec.duration is not None:
            return int(self.spec.duration)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "mode": self.spec.mode.value,
            "duration": self.spec.duration,
            "indefinite": self.spec.duration is None,
            "url": self.spec.template.url,
            "volatile": self.spec.volatile,
            "time_remaining": self.time_remaining(),
            "stats": self.stats.to_dict(),
            "result_path": self.result_path,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.activated_wall or self.created_at,
        }
