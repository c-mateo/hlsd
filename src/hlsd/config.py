from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class DaemonConfig:
    data_dir: Path = field(default_factory=lambda: (
        Path(os.environ["HLSD_DATA_DIR"]) if os.environ.get("HLSD_DATA_DIR") else Path(os.getcwd()) / ".hlsd"
    ))

    host: str = os.environ.get("HLSD_HOST", "127.0.0.1")
    port: int = _env_int("HLSD_PORT", 8000)

    poll_interval_min: float = _env_float("HLSD_POLL_MIN", 1.0)
    poll_interval_max: float = _env_float("HLSD_POLL_MAX", 6.0)
    poll_divisor: float = _env_float("HLSD_POLL_DIVISOR", 2.0)

    # segment download concurrency:
    # - live: 2 per track (video/audio) — gentle on the origin server
    # - VOD/pre-recorded: higher to fetch the whole file quickly,
    #   without hammering rate limiters
    # - HLSD_SEGMENT_CONCURRENCY pins a single value that overrides both
    live_concurrency: int = _env_int("HLSD_LIVE_CONCURRENCY", 2)
    vod_concurrency: int = _env_int("HLSD_VOD_CONCURRENCY", 6)
    segment_concurrency: int | None = (
        _env_int("HLSD_SEGMENT_CONCURRENCY", 0) or None
    )
    global_concurrency: int = _env_int("HLSD_GLOBAL_CONCURRENCY", 6)
    request_timeout: float = _env_float("HLSD_REQUEST_TIMEOUT", 20.0)
    max_retries: int = _env_int("HLSD_MAX_RETRIES", 3)
    retry_backoff_base: float = _env_float("HLSD_RETRY_BACKOFF", 1.0)
    error_backoff_max: float = _env_float("HLSD_ERROR_BACKOFF_MAX", 30.0)
    max_consecutive_errors: int = _env_int("HLSD_MAX_CONSECUTIVE_ERRORS", 20)

    ll_hls_poll_interval: float = _env_float("HLSD_LL_POLL", 0.5)
    ll_block_timeout: float = _env_float("HLSD_LL_BLOCK_TIMEOUT", 15.0)

    volatile_max_bytes: int = _env_int("HLSD_VOLATILE_MAX_BYTES", 1 << 30)
    drain_checks: int = _env_int("HLSD_DRAIN_CHECKS", 2)
    shutdown_timeout: float = _env_float("HLSD_SHUTDOWN_TIMEOUT", 30.0)
    idle_exit_timeout: float = _env_float("HLSD_IDLE_EXIT", 0.0)
    stream_access_ttl: float = _env_float("HLSD_STREAM_TTL", 120.0)
    # Window of segments exposed in streaming playlists (longer than the
    # remote's own window: a lagging tab can catch up instead of freezing).
    # None -> no cap (VOD lists everything)
    stream_window: int = _env_int("HLSD_STREAM_WINDOW", 60)
    # Request-less streams: segments kept in memory only (never touch disk).
    # HLSD_STREAM_VOLATILE=0 restores disk-backed streaming.
    stream_volatile: bool = _env_bool("HLSD_STREAM_VOLATILE", True)
    # Budget of "unused" persisted segments per source: disk segments that no
    # active request references are evicted FIFO (oldest first) once this cap
    # is exceeded. 0 = no cap.
    source_disk_budget: int = _env_int("HLSD_SOURCE_DISK_BUDGET", 20 * 1024 * 1024)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.live_concurrency = max(1, self.live_concurrency)
        self.vod_concurrency = max(1, self.vod_concurrency)
        if self.segment_concurrency is not None:
            self.segment_concurrency = max(1, self.segment_concurrency)
        self.global_concurrency = max(1, self.global_concurrency)
        self.stream_window = max(1, self.stream_window)
        self.source_disk_budget = max(0, self.source_disk_budget)

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def segments_dir(self) -> Path:
        return self.data_dir / "segments"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hlsd.db"

    def ensure_dirs(self) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        self.segments_dir.mkdir(parents=True, exist_ok=True)
