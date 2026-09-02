"""StreamSource: per-resource HLS poller.

Downloads each new segment EXACTLY once (dedup by media sequence) and
distributes it to all active requests of the resource, no matter how many
there are or when they were activated. Handles master/media playlists,
video variants, separate audio renditions (EXT-X-MEDIA) and LL-HLS (full
segments, with optional blocking reload).

The Source lives as long as there are requests with unmet demand (+ drain).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import DaemonConfig
from .hls import (
    MediaPlaylist,
    blocking_params,
    extract_m3u8_from_page,
    looks_like_m3u8,
    parse_master,
    parse_media,
    select_audio,
    select_variant,
)
from .memory import VolatileSegmentStore
from .models import RequestTemplate, SegmentMeta, Selectors, Track
from .net import FetchError, HttpClient
from .request import DownloadRequest
from .store import Store

log = logging.getLogger("hlsd.source")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


class SourceError(RuntimeError):
    pass


@dataclass
class TrackRuntime:
    media_url: str
    playlist: MediaPlaylist | None = None
    last_seq: int = -1
    ended: bool = False
    map_uri: str | None = None
    consecutive_errors: int = 0
    known_seqs: set[int] = field(default_factory=set)


class SegmentBackend:
    """Segment write/read: disk (persistent) or memory (volatile)."""

    async def put(self, source_key: str, track: Track, seq: int, uri: str, extinf: float, data: bytes, *, pdt: float | None = None) -> SegmentMeta:
        raise NotImplementedError

    def read(self, meta: SegmentMeta) -> bytes:
        raise NotImplementedError

    def get_init_meta(self, source_key: str, track: Track) -> SegmentMeta | None:
        """Meta of the init segment (EXT-X-MAP, seq=-1) if it exists in the backend."""
        return None

    def release(self, source_key: str) -> None:
        pass


class DiskBackend(SegmentBackend):
    def __init__(self, config: DaemonConfig, store: Store):
        self.config = config
        self.store = store
        self.config.ensure_dirs()

    async def put(self, source_key, track, seq, uri, extinf, data, *, pdt: float | None = None) -> SegmentMeta:
        directory = self.config.segments_dir / source_key / track.value
        directory.mkdir(parents=True, exist_ok=True)
        name = _sanitize(uri) or f"seg_{seq:08d}"
        final = directory / name
        tmp = directory / f".{name}.tmp"
        tmp.write_bytes(data)
        tmp.replace(final)  # atomic: a partial segment never remains
        meta = SegmentMeta(
            source_key=source_key,
            track=track,
            seq=seq,
            uri=uri,
            extinf=extinf,
            size=len(data),
            pdt=pdt,
            path=str(final),
            is_init=seq < 0,
        )
        self.store.save_segment(source_key, track.value, seq, uri, extinf, str(final), len(data), meta.fetched_wall)
        return meta

    def read(self, meta: SegmentMeta) -> bytes:
        assert meta.path is not None
        return Path(meta.path).read_bytes()

    def get_init_meta(self, source_key: str, track: Track) -> SegmentMeta | None:
        for row in self.store.get_segments(source_key, track.value):
            if row["seq"] < 0:
                return SegmentMeta(
                    source_key=source_key,
                    track=track,
                    seq=-1,
                    uri=row["uri"],
                    extinf=0.0,
                    size=row["size"],
                    path=row["path"],
                    is_init=True,
                )
        return None

    def release(self, source_key: str) -> None:
        pass


class VolatileBackend(SegmentBackend):
    def __init__(self, store: VolatileSegmentStore):
        self.store = store

    async def put(self, source_key, track, seq, uri, extinf, data, *, pdt: float | None = None) -> SegmentMeta:
        key = (source_key, track.value, seq)
        await self.store.put(key, data)  # backpressure if the cap is reached
        return SegmentMeta(
            source_key=source_key,
            track=track,
            seq=seq,
            uri=uri,
            extinf=extinf,
            size=len(data),
            pdt=pdt,
            path=None,
            is_init=seq < 0,
        )

    def read(self, meta: SegmentMeta) -> bytes:
        data = self.store.get((meta.source_key, meta.track.value, meta.seq))
        if data is None:
            raise KeyError(f"missing volatile segment: {meta.key}")
        return data

    def get_init_meta(self, source_key: str, track: Track) -> SegmentMeta | None:
        data = self.store.get((source_key, track.value, -1))
        if data is None:
            return None
        return SegmentMeta(
            source_key=source_key,
            track=track,
            seq=-1,
            uri="init",
            extinf=0.0,
            size=len(data),
            is_init=True,
        )

    def release(self, source_key: str) -> None:
        self.store.delete_prefix((source_key,))


def _sanitize(uri: str) -> str:
    from urllib.parse import unquote, urlparse

    raw = Path(urlparse(uri).path).name
    raw = unquote(raw).split("?")[0].split("#")[0]
    name = _SAFE_NAME.sub("_", raw)[:180]
    return name


def make_backend(config: DaemonConfig, store: Store, volatile: bool) -> SegmentBackend:
    if volatile:
        return VolatileBackend(VolatileSegmentStore(config.volatile_max_bytes))
    return DiskBackend(config, store)


class StreamSource:
    def __init__(
        self,
        key: str,
        template: RequestTemplate,
        selectors: Selectors,
        backend: SegmentBackend,
        net: HttpClient,
        config: DaemonConfig,
        fixed_concurrency: int | None = None,
    ):
        self.key = key
        self.template = template
        self.selectors = selectors
        self.backend = backend
        self.net = net
        self.config = config
        # if the user fixed concurrency (-c / HLSD_SEGMENT_CONCURRENCY) it is
        # not adjusted based on content type; otherwise resolve() chooses
        # between live_concurrency and vod_concurrency
        self.fixed_concurrency = fixed_concurrency
        self.requests: dict[str, DownloadRequest] = {}
        self.tracks: dict[Track, TrackRuntime] = {}
        self.separate_audio = False
        self.stop_event = asyncio.Event()
        self.resolved = False
        self._drain_remaining = config.drain_checks

    # -- subscriptions -------------------------------------------------------
    def add_request(self, request: DownloadRequest) -> None:
        self.requests[request.id] = request
        self._drain_remaining = self.config.drain_checks

    def remove_request(self, request_id: str) -> None:
        self.requests.pop(request_id, None)

    def has_demand(self) -> bool:
        return any(not r.is_satisfied() for r in self.requests.values())

    def request_stop(self) -> None:
        self.stop_event.set()

    # -- resolution ----------------------------------------------------------
    async def resolve(self) -> None:
        text = await self.net.fetch_text(self.template.url)
        playlist_url = self.template.url
        if not looks_like_m3u8(text):
            extracted = extract_m3u8_from_page(text, playlist_url)
            if not extracted:
                raise SourceError("The URL is not an m3u8 playlist and one could not be extracted from the page")
            playlist_url = extracted
            text = await self.net.fetch_text(playlist_url)

        if "#EXT-X-STREAM-INF" in text:
            master = parse_master(text, playlist_url)
            variant = select_variant(
                master,
                order=self.selectors.video_order,
                height=self.selectors.video_height,
                bandwidth=self.selectors.video_bandwidth,
            )
            video_url = variant.uri
            audio_rendition = None
            # AUDIO="<group>" on the variant => separate audio, even if CODECS
            # lists mp4a (the video chunklist is video-only and the master
            # declares the renditions per group)
            if variant.audio_group or not variant.has_audio_codec:
                audio_rendition = select_audio(master, variant, self.selectors.audio_selector)
            if audio_rendition and audio_rendition.uri:
                self.separate_audio = True
                audio_url = audio_rendition.uri
            else:
                audio_url = None
        else:
            video_url = playlist_url
            audio_url = None
            if self.selectors.audio_selector not in ("auto", "none"):
                log.warning("audio_selector ignored: the playlist is not a master")

        self.tracks = {Track.VIDEO: TrackRuntime(media_url=video_url)}
        if audio_url:
            self.tracks[Track.AUDIO] = TrackRuntime(media_url=audio_url)

        for track, rt in self.tracks.items():
            text = await self.net.fetch_text(rt.media_url)
            await self._bootstrap_track(track, rt, text)
        self.resolved = True
        self._apply_concurrency()

    def _apply_concurrency(self) -> None:
        """Lives: low concurrency (2 segments per channel at a time, gentle on
        the origin). Prerecorded (VOD with ENDLIST/PLAYLIST-TYPE:VOD): higher
        concurrency to fetch the full file fast, without hammering rate
        limiters."""
        if self.fixed_concurrency is not None:
            return
        is_vod = all(
            rt.playlist is not None and rt.playlist.is_complete
            for rt in self.tracks.values()
        )
        self.net.set_concurrency(
            self.config.vod_concurrency if is_vod else self.config.live_concurrency
        )

    async def _bootstrap_track(self, track: Track, rt: TrackRuntime, text: str) -> None:
        rt.playlist = parse_media(text, rt.media_url)
        if rt.playlist.map_uri:
            data = await self.net.fetch(rt.playlist.map_uri)
            meta = await self.backend.put(self.key, track, -1, rt.playlist.map_uri, 0.0, data)
            rt.map_uri = rt.playlist.map_uri
            self._emit(meta)
        # dedup across source restarts (disk mode)

        if isinstance(self.backend, DiskBackend):
            rt.known_seqs = self.backend.store.get_segment_seqs(self.key, track.value)

    # -- main loop -----------------------------------------------------------
    async def run(self) -> None:
        if not self.resolved:
            await self.resolve()
        while not self.stop_event.is_set():
            active_tracks = [t for t, rt in self.tracks.items() if not rt.ended]
            if not active_tracks:
                break
            try:
                for track in active_tracks:
                    await self._poll_track(track)
            except FetchError as exc:
                log.warning("Source %s: network error: %s", self.key, exc)
                await self._handle_error()
                continue
            if not self.has_demand():
                if self._drain_remaining <= 0:
                    break
                self._drain_remaining -= 1
            else:
                self._drain_remaining = self.config.drain_checks
            await self._sleep_interval_async()

    async def _poll_track(self, track: Track) -> None:
        rt = self.tracks[track]
        playlist = rt.playlist
        assert playlist is not None
        params = blocking_params(playlist, rt.last_seq + 1) if rt.last_seq >= 0 else None
        timeout = self.config.ll_block_timeout if params else None
        text = await self.net.fetch_text(rt.media_url, params=params, timeout=timeout)
        parsed = parse_media(text, rt.media_url)
        rt.playlist = parsed

        if parsed.map_uri and parsed.map_uri != rt.map_uri:
            data = await self.net.fetch(parsed.map_uri)
            meta = await self.backend.put(self.key, track, -1, parsed.map_uri, 0.0, data)
            rt.map_uri = parsed.map_uri
            self._emit(meta)

        base_seq = parsed.media_sequence
        if rt.last_seq < 0:
            # anchor contiguous progress to the real start of the window:
            # lives start with MEDIA-SEQUENCE > 0 and last_seq=-1 would never
            # catch up to the real seqs
            rt.last_seq = base_seq - 1
        if rt.last_seq >= 0 and any(s not in rt.known_seqs for s in range(rt.last_seq + 1, base_seq)):
            missing = [s for s in range(rt.last_seq + 1, base_seq) if s not in rt.known_seqs]
            log.warning(
                "Source %s track %s: gap in the window, missing seqs %s",
                self.key, track.value, missing[:10],
            )

        # pending: not downloaded yet (dedup by seq), in order
        pending = [
            (base_seq + offset, uri, extinf, pdt)
            for offset, (uri, extinf, pdt) in enumerate(parsed.segments)
            if base_seq + offset > rt.last_seq and base_seq + offset not in rt.known_seqs
        ]
        if pending:
            await self._download_batch(track, rt, pending)
        rt.consecutive_errors = 0

        # VOD: ENDLIST or PLAYLIST-TYPE:VOD => no more segments.
        # EVENT: the list grows without removing old ones => keep polling
        # (dedup by seq already ignores known ones). Pure live: sliding
        # window, missing ones are retried while they remain in it.
        if parsed.is_complete:
            rt.ended = True

    async def _download_batch(self, track: Track, rt: TrackRuntime, pending: list[tuple[int, str, float, float | None]]) -> None:
        """Downloads the pending segments in parallel (bounded by the client's
        semaphore, shared across tracks) and stores them in sequence order.
        A failing segment does not open a gap: last_seq only advances
        contiguously and the missing one is retried on the next poll while
        it remains in the window."""
        results = await asyncio.gather(
            *(self.net.fetch(uri) for _seq, uri, _extinf, _pdt in pending),
            return_exceptions=True,
        )
        fetched: list[tuple[int, str, float, float | None, bytes]] = []
        failed = 0
        for (seq, uri, extinf, pdt), res in zip(pending, results):
            if isinstance(res, BaseException):
                failed += 1
                log.warning("Source %s track %s: failed seq %s (%r)", self.key, track.value, seq, res)
                continue
            fetched.append((seq, uri, extinf, pdt, res))
        for seq, uri, extinf, pdt, data in sorted(fetched, key=lambda item: item[0]):
            meta = await self.backend.put(self.key, track, seq, uri, extinf, data, pdt=pdt)
            rt.known_seqs.add(seq)
            self._emit(meta)
        if failed == len(pending):
            raise FetchError(f"all segments in the batch failed ({len(pending)})")
        nxt = rt.last_seq + 1
        while nxt in rt.known_seqs:
            rt.last_seq = nxt
            nxt += 1

    def _emit(self, meta: SegmentMeta) -> None:
        for request in list(self.requests.values()):
            request.assign_segment(meta)

    async def _handle_error(self) -> None:
        for rt in self.tracks.values():
            rt.consecutive_errors += 1
        total = max(rt.consecutive_errors for rt in self.tracks.values())
        if total > self.config.max_consecutive_errors and not self.has_demand():
            raise SourceError(f"too many consecutive errors ({total}) with no pending demand")
        if total > self.config.max_consecutive_errors and self._no_segments_ever():
            raise SourceError(f"too many consecutive errors ({total}) without obtaining segments")
        delay = min(self.config.retry_backoff_base * (2 ** min(total, 10)), self.config.error_backoff_max)
        await asyncio.sleep(delay)

    def _no_segments_ever(self) -> bool:
        return all(rt.last_seq < 0 for rt in self.tracks.values())

    async def _sleep_interval_async(self) -> None:
        interval = self._current_interval()
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

    async def sleep_interval(self) -> None:
        await self._sleep_interval_async()

    def _current_interval(self) -> float:
        cfg = self.config
        candidates = []
        for rt in self.tracks.values():
            if rt.playlist is None:
                continue
            if rt.playlist.is_ll:
                candidates.append(cfg.ll_hls_poll_interval)
                continue
            target = rt.playlist.target_duration or rt.playlist.part_hold_back or 4.0
            candidates.append(min(max(target / cfg.poll_divisor, cfg.poll_interval_min), cfg.poll_interval_max))
        if not candidates:
            return cfg.poll_interval_min
        return min(candidates)
