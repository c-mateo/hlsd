"""StreamDaemon: single orchestrator.

Manages Sources (one per resource, with dedup), Requests, the scheduler and
finalization (ffmpeg assembly). A single process; graceful shutdown
without losing already-downloaded segments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import streaming
from .config import DaemonConfig
from .curl_parser import parse_source_command
from .hls import MasterInfo, extract_m3u8_from_page, looks_like_m3u8, parse_master
from .merger import MergeError, merge_request
from .models import (
    TERMINAL_STATES,
    DownloadMode,
    DownloadSpec,
    RequestState,
    RequestTemplate,
    Selectors,
    Track,
)
from .net import FetchError, HttpClient
from .request import DownloadRequest
from .scheduler import Scheduler, parse_start
from .source import (
    DiskBackend,
    SegmentBackend,
    SourceError,
    StreamSource,
    VolatileBackend,
)
from .store import Store

log = logging.getLogger("hlsd.daemon")


class DaemonError(RuntimeError):
    pass


@dataclass
class StreamSession:
    """Live streaming session: indefinite demand on a Source."""

    id: str
    source_key: str
    request_id: str
    last_access: float = field(default_factory=time.monotonic)
    template: RequestTemplate | None = None


class SourceRuntime:
    def __init__(self, source: StreamSource, task: asyncio.Task, net: HttpClient):
        self.source = source
        self.task = task
        self.net = net


def _spec_to_dict(spec: DownloadSpec) -> dict[str, Any]:
    return {
        "url": spec.template.url,
        "method": spec.template.method,
        "headers": spec.template.headers,
        "cookies": spec.template.cookies,
        "body": spec.template.body.decode("utf-8", "replace") if spec.template.body else None,
        "auth": list(spec.template.auth) if spec.template.auth else None,
        "selectors": asdict(spec.selectors),
        "mode": spec.mode.value,
        "duration": spec.duration,
        "start_at": spec.start_at,
        "volatile": spec.volatile,
        "name": spec.name,
        "concurrency": spec.concurrency,
    }


def _spec_from_dict(data: dict[str, Any]) -> DownloadSpec:
    template = RequestTemplate(
        url=data["url"],
        method=data.get("method", "GET"),
        headers=data.get("headers") or {},
        cookies=data.get("cookies") or {},
        body=data["body"].encode() if data.get("body") else None,
        auth=tuple(data["auth"]) if data.get("auth") else None,
    )
    sel = data.get("selectors") or {}
    return DownloadSpec(
        template=template,
        selectors=Selectors(**sel),
        mode=DownloadMode(data.get("mode", "window")),
        duration=data.get("duration"),
        start_at=data.get("start_at"),
        volatile=bool(data.get("volatile", False)),
        name=data.get("name", ""),
        concurrency=data.get("concurrency"),
    )


class StreamDaemon:
    def __init__(self, config: DaemonConfig | None = None, store: Store | None = None):
        self.config = config or DaemonConfig()
        self.store = store or Store(self.config.db_path)
        self.config.ensure_dirs()
        self.requests: dict[str, DownloadRequest] = {}
        self.sources: dict[str, SourceRuntime] = {}
        self.streams: dict[str, StreamSession] = {}
        self._lock = asyncio.Lock()
        self._monitor_task: asyncio.Task | None = None
        self._stopping = False
        self.idle_exit_after: float = float(self.config.idle_exit_timeout or 0)
        self.on_idle: Callable[[], None] | None = None
        self._idle_since: float | None = None
        self._last_prune = 0.0
        self._disk_backend = DiskBackend(self.config, self.store)
        from .memory import VolatileSegmentStore

        self._volatile_backend = VolatileBackend(VolatileSegmentStore(self.config.volatile_max_bytes))
        self.scheduler = Scheduler(self._on_job_due)

    # -- lifecycle -----------------------------------------------------------
    async def start(self) -> None:
        from .net import global_limiter

        global_limiter.configure(self.config.global_concurrency)
        self._stopping = False
        self._recover()
        self.scheduler.start()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    def _recover(self) -> None:
        for request_id, due_at in self.store.list_jobs():
            row = self.store.get_request(request_id)
            if not row:
                self.store.delete_job(request_id)
                continue
            spec = _spec_from_dict(json.loads(row["spec"]))
            request = DownloadRequest(spec, request_id)
            request.state = RequestState.SCHEDULED
            self.requests[request_id] = request
            self.scheduler.add(request_id, due_at)
        for row in self.store.list_requests():
            state = RequestState(row["state"])
            if state in (RequestState.ACTIVE, RequestState.RESOLVING, RequestState.FINALIZING):
                # after a restart: assemble whatever is on disk and mark stopped
                request = DownloadRequest(_spec_from_dict(json.loads(row["spec"])), row["id"])
                request.state = RequestState.ACTIVE
                request.activated_mono = 0.0
                if row["activated_at"]:
                    request.created_at = row["activated_at"]
                self.requests[request.id] = request
                self._rehydrate_segments(request)
                request.request_stop()
                asyncio.get_running_loop().create_task(self._finalize(request, stopped=True, note="daemon restarted"))

    def _rehydrate_segments(self, request: DownloadRequest) -> None:
        from .models import SegmentMeta, Track

        spec = request.spec
        key = spec.template.resource_key(spec.selectors)
        for track in (Track.VIDEO, Track.AUDIO):
            rows = self.store.get_segments(key, track.value)
            for row in rows:
                meta = SegmentMeta(
                    source_key=key,
                    track=track,
                    seq=row["seq"],
                    uri=row["uri"],
                    extinf=row["extinf"],
                    size=row["size"],
                    fetched_wall=row["fetched_wall"],
                    path=row["path"],
                    fetched_mono=-1.0 if row["seq"] < 0 else 0.0,
                )
                if row["seq"] >= 0:
                    request.segments[track].append(meta)
        for track in (Track.VIDEO, Track.AUDIO):
            request.segments[track] = sorted(request.segments[track], key=lambda m: m.seq)
        request.stats.segments_video = len(request.segments[Track.VIDEO])
        request.stats.segments_audio = len(request.segments[Track.AUDIO])
        request.stats.accumulated_duration = sum(m.extinf for m in request.segments[Track.VIDEO])
        request.stats.bytes_downloaded = sum(m.size for m in request.segments[Track.VIDEO] + request.segments[Track.AUDIO])

    async def shutdown(self) -> None:
        self._stopping = True
        await self.scheduler.stop()
        if self._monitor_task:
            self._monitor_task.cancel()
        for session in list(self.streams.values()):
            self._drop_stream(session)
        for runtime in list(self.sources.values()):
            runtime.source.request_stop()
        if self.sources:
            await asyncio.wait(
                [rt.task for rt in self.sources.values() if not rt.task.done()],
                timeout=self.config.shutdown_timeout,
            )
        for runtime in list(self.sources.values()):
            if not runtime.task.done():
                runtime.task.cancel()
            await runtime.net.aclose()
        self.sources.clear()
        for request in list(self.requests.values()):
            if request.state in (RequestState.ACTIVE, RequestState.RESOLVING):
                self._rehydrate_segments(request)
                request.request_stop()
                await self._finalize(request, stopped=True, note="shutdown")
        if not any(r.spec.volatile for r in self.requests.values()):
            from .memory import malloc_trim

            malloc_trim()
        self.store.close()

    # -- creation / activation ------------------------------------------------
    async def create_request(
        self,
        *,
        url: str | None = None,
        curl: str | None = None,
        playlist_file: str | None = None,
        base_url: str | None = None,
        mode: str = "window",
        duration: float | None = None,
        start: str | None = None,
        select_order: int | None = None,
        select_height: int | None = None,
        select_bandwidth: int | None = None,
        audio: str = "auto",
        volatile: bool = False,
        name: str = "",
        concurrency: int | None = None,
    ) -> DownloadRequest:
        template = self._build_template(url=url, curl=curl, playlist_file=playlist_file, base_url=base_url)
        spec = DownloadSpec(
            template=template,
            selectors=Selectors(
                video_order=select_order,
                video_height=select_height,
                video_bandwidth=select_bandwidth,
                audio_selector=audio,
            ),
            mode=DownloadMode(mode),
            duration=duration,
            volatile=volatile,
            name=name,
            concurrency=self._clamp_concurrency(concurrency),
        )
        request = DownloadRequest(spec)
        start_at = parse_start(start)
        if duration is not None and duration <= 0:
            raise DaemonError("duration must be positive or None (indefinite)")
        if spec.mode is DownloadMode.SEGMENTS and duration is not None:
            if duration != int(duration):
                raise DaemonError("with mode=segments, duration must be an integer (number of segments)")
            spec.duration = int(duration)
        self.requests[request.id] = request
        self.store.save_request(request.id, _spec_to_dict(spec), request.state.value)
        if start_at is not None:
            request.state = RequestState.SCHEDULED
            self.store.update_request(request.id, state=RequestState.SCHEDULED.value)
            self.store.save_job(request.id, start_at)
            self.scheduler.add(request.id, start_at)
            log.info("request %s scheduled for %s", request.id, start)
        else:
            await self._activate(request)
        return request

    @staticmethod
    def _clamp_concurrency(value: int | None) -> int | None:
        if value is None:
            return None
        return max(1, min(3, value))

    def _build_template(
        self,
        *,
        url: str | None,
        curl: str | None,
        playlist_file: str | None,
        base_url: str | None,
    ) -> RequestTemplate:
        provided = [p for p in (url, curl, playlist_file) if p]
        if len(provided) != 1:
            raise DaemonError("Specify exactly one of: url, curl, playlist_file")
        if curl:
            return parse_source_command(curl)
        if url:
            return RequestTemplate(url=url)
        assert playlist_file
        path = Path(playlist_file)
        if not path.is_file():
            raise DaemonError(f"playlist file does not exist: {playlist_file}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if not looks_like_m3u8(text):
            raise DaemonError("the file does not look like an m3u8 playlist")
        if not base_url:
            raise DaemonError("playlist_file requires base_url to resolve segments")
        return RequestTemplate(url=base_url)

    async def _on_job_due(self, request_id: str) -> None:
        request = self.requests.get(request_id)
        if request is None:
            return
        self.store.delete_job(request_id)
        await self._activate(request)

    async def _activate(self, request: DownloadRequest) -> None:
        request.activate()
        self.store.update_request(request.id, state=RequestState.ACTIVE.value, activated_at=request.created_at)
        runtime = await self._get_source_runtime(request.spec)
        runtime.source.add_request(request)
        log.info("request %s active on source %s", request.id, runtime.source.key)

    async def _get_source_runtime(self, spec: DownloadSpec) -> SourceRuntime:
        key = spec.template.resource_key(spec.selectors)
        async with self._lock:
            runtime = self.sources.get(key)
            if runtime is not None:
                return runtime
            concurrency = self._clamp_concurrency(spec.concurrency) or self.config.segment_concurrency
            net = HttpClient(
                spec.template,
                concurrency=concurrency or self.config.live_concurrency,
                timeout=self.config.request_timeout,
                max_retries=self.config.max_retries,
                backoff_base=self.config.retry_backoff_base,
            )
            backend = self._volatile_backend if spec.volatile else self._disk_backend
            source = StreamSource(
                key, spec.template, spec.selectors, backend, net, self.config,
                fixed_concurrency=concurrency,
            )
            task = asyncio.create_task(self._run_source(source, net))
            runtime = SourceRuntime(source, task, net)
            self.sources[key] = runtime
            self.store.save_source(key, _spec_to_dict(_template_only(spec)), asdict(spec.selectors), isinstance(source.backend, VolatileBackend))
            return runtime

    async def _run_source(self, source: StreamSource, net: HttpClient) -> None:
        ended_normally = False
        try:
            try:
                await source.run()
                ended_normally = not self._stopping and not source.stop_event.is_set()
            except (SourceError, FetchError) as exc:
                log.error("source %s ended with error: %s", source.key, exc)
                for request in list(source.requests.values()):
                    request.error = str(exc)
                await asyncio.gather(
                    *(self._finalize(r, failed=True) for r in list(source.requests.values())),
                    return_exceptions=True,
                )
            if ended_normally:
                # VOD with ENDLIST or PLAYLIST-TYPE:VOD (or the full window
                # already downloaded): active indefinite requests will no
                # longer receive segments.
                for request in list(source.requests.values()):
                    if request.state is RequestState.ACTIVE:
                        await self._finalize(request, stopped=False)
        except asyncio.CancelledError:
            pass
        finally:
            self.sources.pop(source.key, None)
            await net.aclose()
            source.backend.release(source.key)
            from .memory import malloc_trim

            malloc_trim()
            self.store.update_source_state(source.key, "ended")
            log.info("source %s finished", source.key)

    # -- monitoring / finalization --------------------------------------------
    async def _monitor_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(0.5)
            now = time.monotonic()
            for request in list(self.requests.values()):
                if request.state is RequestState.ACTIVE and request.is_satisfied():
                    try:
                        await self._finalize(request, stopped=request.stop_requested)
                    except Exception:
                        log.exception("failed to finalize request %s", request.id)
            if now - self._last_prune >= 30.0:
                self._last_prune = now
                try:
                    self._prune_disk_segments()
                except Exception:
                    log.exception("failed to prune disk segments")
            self._expire_streams(now)
            self._check_idle(now)

    def _prune_disk_segments(self) -> None:
        """Disk storage control: per source, evict the oldest persisted
        segments that no active request/viewer references until usage fits
        the configured budget. Best-effort: retry-after-restart only sees
        what survived pruning."""
        budget = self.config.source_disk_budget
        if budget <= 0:
            return
        referenced: set[tuple[str, str, int]] = set()
        for request in self.requests.values():
            for track, metas in request.segments.items():
                for meta in metas:
                    referenced.add((meta.source_key, track.value, meta.seq))
        for runtime in self.sources.values():
            for request in runtime.source.requests.values():
                for track, metas in request.segments.items():
                    for meta in metas:
                        referenced.add((meta.source_key, track.value, meta.seq))
        for key, total in self.store.segment_bytes_by_source().items():
            if total <= budget:
                continue
            for track_value in (Track.VIDEO.value, Track.AUDIO.value):
                for row in self.store.get_segments(key, track_value):
                    if total <= budget:
                        break
                    if row["seq"] < 0 or (key, track_value, row["seq"]) in referenced:
                        continue  # init segment and in-use segments are never pruned
                    deleted = self.store.delete_segment(key, track_value, row["seq"])
                    if deleted is None:
                        continue
                    try:
                        Path(deleted["path"]).unlink(missing_ok=True)
                    except OSError:
                        log.warning("could not delete pruned segment: %s", deleted["path"])
                    runtime = self.sources.get(key)
                    if runtime is not None:
                        rt = runtime.source.tracks.get(Track(track_value))
                        if rt is not None:
                            rt.known_seqs.discard(row["seq"])
                    total -= deleted["size"]
            if total > budget:
                log.info("source %s: %dMB of persisted segments remain over budget", key, total // (1 << 20))

    def _expire_streams(self, now: float) -> None:
        for session in list(self.streams.values()):
            if now - session.last_access > self.config.stream_access_ttl:
                log.info("stream %s expired due to inactivity", session.id)
                self._drop_stream(session)

    def _check_idle(self, now: float) -> None:
        idle = not self.requests and not self.sources and not self.streams and self.scheduler.next_due() is None
        if not idle:
            self._idle_since = None
            return
        if self._idle_since is None:
            self._idle_since = now
            return
        if self.idle_exit_after > 0 and self.on_idle is not None and now - self._idle_since >= self.idle_exit_after:
            log.info("daemon idle %.0fs — auto-shutdown", now - self._idle_since)
            self._idle_since = None
            callback = self.on_idle
            self.on_idle = None
            callback()

    # -- live streaming -------------------------------------------------------
    async def create_stream(self, **kwargs: Any) -> StreamSession:
        template = self._build_template(
            url=kwargs.get("url"),
            curl=kwargs.get("curl"),
            playlist_file=kwargs.get("playlist_file"),
            base_url=kwargs.get("base_url"),
        )
        # Request-less streams default to the volatile (memory) backend so a
        # casual playback session never writes to disk; HLSD_STREAM_VOLATILE=0
        # (or explicit volatile=false) restores disk-backed streaming.
        volatile = kwargs.get("volatile")
        if volatile is None:
            volatile = self.config.stream_volatile
        spec = DownloadSpec(
            template=template,
            selectors=Selectors(
                video_order=kwargs.get("select_order"),
                video_height=kwargs.get("select_height"),
                video_bandwidth=kwargs.get("select_bandwidth"),
                audio_selector=kwargs.get("audio", "auto"),
            ),
            mode=DownloadMode.WINDOW,
            duration=None,
            volatile=bool(volatile),
            viewer=True,
            concurrency=self._clamp_concurrency(kwargs.get("concurrency")),
        )
        viewer = DownloadRequest(spec)
        viewer.activate()
        runtime = await self._get_source_runtime(spec)
        runtime.source.add_request(viewer)
        session = StreamSession(id=uuid.uuid4().hex[:12], source_key=runtime.source.key, request_id=viewer.id, template=template)
        self.streams[session.id] = session
        log.info("stream %s opened on source %s", session.id, runtime.source.key)
        return session

    def get_stream(self, stream_id: str) -> tuple[StreamSession, StreamSource]:
        session = self.streams.get(stream_id)
        if session is None:
            raise DaemonError(f"stream not found or expired: {stream_id}")
        session.last_access = time.monotonic()
        runtime = self.sources.get(session.source_key)
        if runtime is None:
            self._drop_stream(session)
            raise DaemonError(f"the stream {stream_id}'s source is no longer active")
        return session, runtime.source

    def get_stream_playlist(self, stream_id: str, track: str | None = None) -> str:
        _session, source = self.get_stream(stream_id)
        if track is None:
            text = streaming.render_master_playlist(source)
        else:
            try:
                track_enum = Track(track)
            except ValueError:
                raise DaemonError(f"invalid track: {track}")
            rt = source.tracks.get(track_enum)
            max_window = (
                None
                if rt is not None and rt.playlist is not None and rt.playlist.is_complete
                else self.config.stream_window
            )
            text = streaming.render_track_playlist(source, track_enum, max_window=max_window)
        if text is None:
            raise DaemonError("the stream has no segments yet; retry in a few seconds")
        return text

    def get_stream_segment(self, stream_id: str, track_value: str, seq: int) -> tuple[bytes, str]:
        _session, source = self.get_stream(stream_id)
        data = streaming.read_segment(source, track_value, seq)
        if data is None:
            raise DaemonError(f"segment not available: {track_value}/{seq}")
        uri = _stream_segment_uri(source, track_value, seq)
        return data, streaming.guess_segment_media_type(uri)

    def _drop_stream(self, session: StreamSession) -> None:
        self.streams.pop(session.id, None)
        runtime = self.sources.get(session.source_key)
        if runtime is not None:
            runtime.source.remove_request(session.request_id)

    def close_stream(self, stream_id: str) -> None:
        session = self.streams.get(stream_id)
        if session is None:
            raise DaemonError(f"stream not found: {stream_id}")
        self._drop_stream(session)

    def list_streams(self) -> list[dict[str, Any]]:
        return [
            {
                "id": s.id,
                "url": s.template.url if s.template else None,
                "source_key": s.source_key,
                "age_seconds": round(time.monotonic() - s.last_access, 1),
            }
            for s in self.streams.values()
        ]

    def _backend_for(self, request: DownloadRequest) -> SegmentBackend:
        """Backend actually holding this request's segments: the source's own
        backend (a recording request sharing a stream-created source inherits
        its storage), falling back to the spec's volatile flag once the
        source is gone."""
        key = request.spec.template.resource_key(request.spec.selectors)
        runtime = self.sources.get(key)
        if runtime is not None:
            return runtime.source.backend
        return self._volatile_backend if request.spec.volatile else self._disk_backend

    async def _finalize(
        self,
        request: DownloadRequest,
        *,
        stopped: bool = False,
        failed: bool = False,
        note: str | None = None,
    ) -> None:
        if request.state in TERMINAL_STATES or request.state is RequestState.FINALIZING:
            return
        for runtime in self.sources.values():
            runtime.source.remove_request(request.id)
        request.state = RequestState.FINALIZING
        self.store.update_request(request.id, state=RequestState.FINALIZING.value)
        try:
            out = self.config.outputs_dir / f"{request.id}.mp4"
            backend = self._backend_for(request)
            await merge_request(request, backend, out)
            request.result_path = str(out)
            if failed or note:
                request.error = note
            request.state = RequestState.STOPPED if stopped else (RequestState.FAILED if failed else RequestState.DONE)
            self.store.update_request(
                request.id,
                state=request.state.value,
                finished_at=request.created_at,
                result_path=str(out),
                error=request.error,
                clear_error=request.error is None,
                stats=request.stats.to_dict(),
            )
            log.info("request %s -> %s", request.id, request.state.value)
        except (MergeError, KeyError, OSError, sqlite3.Error) as exc:
            request.error = str(exc)
            request.state = RequestState.FAILED
            self.store.update_request(request.id, state=RequestState.FAILED.value, error=str(exc))
            log.error("request %s failed to assemble: %s", request.id, exc)
        self.requests.pop(request.id, None)

    # -- operations -------------------------------------------------------------
    async def retry_finalize(self, request_id: str) -> dict[str, Any]:
        """Retries assembly of a request that failed to finalize.

        Segments remain on disk (persistent backend), so the retry works
        even after restarting the daemon: the spec and metadata are loaded
        from the store and re-assembled with the current code. With a
        volatile backend the segments are lost on restart."""
        request = self.requests.get(request_id)
        if request is None:
            row = self.store.get_request(request_id)
            if not row:
                raise DaemonError(f"request not found: {request_id}")
            request = DownloadRequest(_spec_from_dict(json.loads(row["spec"])), request_id)
            self._rehydrate_segments(request)
        if request.state in (RequestState.DONE, RequestState.STOPPED):
            raise DaemonError(f"request already finalized ({request.state.value})")
        if not request.segments[Track.VIDEO]:
            self._rehydrate_segments(request)
            if not request.segments[Track.VIDEO]:
                raise DaemonError("no persisted segments to assemble")
        request.error = None
        request.state = RequestState.ACTIVE
        self.requests[request_id] = request
        await self._finalize(request)
        return self.status(request_id)

    async def stop_request(self, request_id: str) -> DownloadRequest:
        request = self.requests.get(request_id)
        if request is None:
            raise DaemonError(f"request not found: {request_id}")
        if request.state is RequestState.SCHEDULED:
            self.scheduler.cancel(request_id)
            self.store.delete_job(request_id)
            request.state = RequestState.CANCELLED
            self.store.update_request(request_id, state=RequestState.CANCELLED.value)
            self.requests.pop(request_id, None)
            return request
        if request.state is RequestState.ACTIVE:
            request.request_stop()
            await self._finalize(request, stopped=True)
            return request
        raise DaemonError(f"request in state {request.state.value} cannot be stopped")

    async def merge_preview(self, request_id: str) -> Path:
        request = self.requests.get(request_id)
        if request is None:
            row = self.store.get_request(request_id)
            if row and row.get("result_path"):
                return Path(row["result_path"])
            raise DaemonError(f"request not found: {request_id}")
        if request.state is RequestState.SCHEDULED:
            raise DaemonError("the request has not started yet")
        out = self.config.outputs_dir / f"{request_id}_preview.mp4"
        await merge_request(request, self._backend_for(request), out)
        return out

    def status(self, request_id: str) -> dict[str, Any]:
        request = self.requests.get(request_id)
        if request is None:
            row = self.store.get_request(request_id)
            if not row:
                raise DaemonError(f"request not found: {request_id}")
            return {
                "id": row["id"],
                "state": row["state"],
                "result_path": row.get("result_path"),
                "error": row.get("error"),
                "stats": json.loads(row["stats"]) if row.get("stats") else None,
                "url": json.loads(row["spec"]).get("url"),
                "created_at": row.get("created_at"),
                "started_at": row.get("activated_at") or row.get("created_at"),
            }
        return request.to_dict()

    def list_requests(self) -> list[dict[str, Any]]:
        rows = self.store.list_requests()
        result = []
        for row in rows:
            entry = {
                "id": row["id"],
                "state": row["state"],
                "url": json.loads(row["spec"]).get("url"),
                "result_path": row.get("result_path"),
            }
            live = self.requests.get(row["id"])
            if live:
                entry.update(live.to_dict())
            result.append(entry)
        return result

    def sources_status(self) -> list[dict[str, Any]]:
        return [
            {
                "key": key,
                "requests": len(rt.source.requests),
                "tracks": {t.value: {"last_seq": r.last_seq, "ended": r.ended} for t, r in rt.source.tracks.items()},
                "ll_hls": any(r.playlist and r.playlist.is_ll for r in rt.source.tracks.values()),
            }
            for key, rt in self.sources.items()
        ]

    async def inspect(
        self,
        *,
        url: str | None = None,
        curl: str | None = None,
        playlist_file: str | None = None,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        template = self._build_template(url=url, curl=curl, playlist_file=playlist_file, base_url=base_url)
        net = HttpClient(template, timeout=self.config.request_timeout, max_retries=1)
        try:
            text = await net.fetch_text(template.url)
            playlist_url = template.url
            if not looks_like_m3u8(text):
                extracted = extract_m3u8_from_page(text, playlist_url)
                if not extracted:
                    raise DaemonError("could not extract an m3u8 from the page")
                playlist_url = extracted
                text = await net.fetch_text(playlist_url)
            if "#EXT-X-STREAM-INF" not in text:
                return {"type": "media", "url": playlist_url, "note": "direct media playlist (no variants)"}
            master = parse_master(text, playlist_url)
            return _master_to_dict(master, playlist_url)
        finally:
            await net.aclose()


def _master_to_dict(master: MasterInfo, playlist_url: str) -> dict[str, Any]:
    variants = []
    for i, v in enumerate(sorted(master.variants, key=lambda v: v.bandwidth)):
        variants.append(
            {
                "index": i,
                "bandwidth": v.bandwidth,
                "resolution": f"{v.width}x{v.height}" if v.width and v.height else None,
                "codecs": v.codecs,
                "muxed_audio": v.has_audio_codec,
                "audio_group": v.audio_group,
                "uri": v.uri,
            }
        )
    renditions = [
        {
            "group": r.group_id,
            "name": r.name,
            "language": r.language,
            "channels": r.channels,
            "default": r.default,
            "uri": r.uri,
        }
        for r in master.renditions
    ]
    return {"type": "master", "url": playlist_url, "variants": variants, "audio_renditions": renditions}


def _template_only(spec: DownloadSpec) -> DownloadSpec:
    return DownloadSpec(template=spec.template, selectors=spec.selectors)


def _stream_segment_uri(source: StreamSource, track_value: str, seq: int) -> str:
    try:
        track = Track(track_value)
    except ValueError:
        return f"{track_value}/{seq}"
    rt = source.tracks.get(track)
    if rt and rt.playlist:
        base = rt.playlist.media_sequence
        for offset, (uri, _extinf, _pdt) in enumerate(rt.playlist.segments):
            if base + offset == seq:
                return uri
    return f"{track_value}/{seq}"
