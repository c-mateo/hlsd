"""HTTP API of the daemon (FastAPI)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from pydantic import BaseModel, Field

from .config import DaemonConfig
from .daemon import DaemonError, StreamDaemon
from .memory import process_rss_bytes
from .models import DownloadMode
from .net import FetchError

log = logging.getLogger("hlsd.api")


class CreateRequest(BaseModel):
    url: str | None = None
    curl: str | None = None
    playlist_file: str | None = None
    base_url: str | None = None
    mode: DownloadMode = Field(default=DownloadMode.WINDOW, description="realtime | window | accumulated | segments")
    duration: float | None = Field(default=None, description="seconds (or number of segments with mode=segments); None = indefinite")
    start: str | None = Field(default=None, description="now | in 5m | 17:00 | 2026-09-02T17:00:00")
    select_order: int | None = None
    select_height: int | None = None
    select_bandwidth: int | None = None
    audio: str = Field(default="auto", description="auto | none | order:N | lang:xx | name:...")
    volatile: bool = False
    name: str = ""
    concurrency: int | None = Field(default=None, ge=1, le=3, description="segments in parallel (1-3)")


class InspectRequest(BaseModel):
    url: str | None = None
    curl: str | None = None
    playlist_file: str | None = None
    base_url: str | None = None


class StreamRequest(BaseModel):
    url: str | None = None
    curl: str | None = None
    playlist_file: str | None = None
    base_url: str | None = None
    select_order: int | None = None
    select_height: int | None = None
    select_bandwidth: int | None = None
    audio: str = "auto"
    volatile: bool | None = None
    concurrency: int | None = Field(default=None, ge=1, le=3)


def create_app(config: DaemonConfig | None = None) -> FastAPI:
    cfg = config or DaemonConfig()
    daemon = StreamDaemon(cfg)
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await daemon.start()
        yield
        await daemon.shutdown()

    app = FastAPI(title="hlsd — HLS recording daemon", version="0.2.0", lifespan=lifespan)
    app.state.daemon = daemon

    @app.exception_handler(DaemonError)
    async def daemon_error_handler(_request, exc: DaemonError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.post("/requests")
    async def create(payload: CreateRequest):
        try:
            request = await daemon.create_request(
                url=payload.url,
                curl=payload.curl,
                playlist_file=payload.playlist_file,
                base_url=payload.base_url,
                mode=payload.mode,
                duration=payload.duration,
                start=payload.start,
                select_order=payload.select_order,
                select_height=payload.select_height,
                select_bandwidth=payload.select_bandwidth,
                audio=payload.audio,
                volatile=payload.volatile,
                name=payload.name,
                concurrency=payload.concurrency,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return JSONResponse(status_code=201, content=request.to_dict())

    @app.get("/requests")
    async def list_requests():
        return daemon.list_requests()

    @app.get("/requests/{request_id}")
    async def get_request(request_id: str):
        try:
            return daemon.status(request_id)
        except DaemonError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/requests/{request_id}/stop")
    async def stop(request_id: str):
        try:
            request = await daemon.stop_request(request_id)
        except DaemonError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return request.to_dict() if hasattr(request, "to_dict") else {"id": request.id, "state": request.state.value}

    @app.delete("/requests/{request_id}")
    async def cancel(request_id: str):
        try:
            request = await daemon.stop_request(request_id)
        except DaemonError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"id": request.id, "state": request.state.value}

    @app.post("/requests/{request_id}/retry")
    async def retry(request_id: str):
        try:
            return await daemon.retry_finalize(request_id)
        except DaemonError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/requests/{request_id}/download")
    async def download(request_id: str):
        try:
            path = await daemon.merge_preview(request_id)
        except DaemonError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not Path(path).exists():
            raise HTTPException(status_code=404, detail="no segments available to assemble yet")
        return FileResponse(path, media_type="video/mp4", filename=Path(path).name)

    @app.post("/inspect")
    async def inspect(payload: InspectRequest):
        try:
            return await daemon.inspect(
                url=payload.url,
                curl=payload.curl,
                playlist_file=payload.playlist_file,
                base_url=payload.base_url,
            )
        except (DaemonError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except FetchError as exc:
            raise HTTPException(status_code=502, detail=f"Could not reach the HLS server: {exc}")

    # -- live streaming (shares the Source: a single fetch to the remote) ------
    @app.post("/streams")
    async def create_stream(payload: StreamRequest):
        try:
            session = await daemon.create_stream(
                url=payload.url,
                curl=payload.curl,
                playlist_file=payload.playlist_file,
                base_url=payload.base_url,
                select_order=payload.select_order,
                select_height=payload.select_height,
                select_bandwidth=payload.select_bandwidth,
                audio=payload.audio,
                volatile=payload.volatile,
                concurrency=payload.concurrency,
            )
        except (DaemonError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "id": session.id,
            "playlist": f"/streams/{session.id}/playlist.m3u8",
            "note": "point a player (VLC/ffplay) at the playlist URL",
        }

    @app.get("/streams")
    async def list_streams():
        return daemon.list_streams()

    @app.get("/streams/{stream_id}/playlist.m3u8")
    async def stream_playlist(stream_id: str):
        try:
            text = daemon.get_stream_playlist(stream_id)
        except DaemonError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return PlainTextResponse(text, media_type="application/vnd.apple.mpegurl")

    @app.get("/streams/{stream_id}/video.m3u8")
    async def stream_video_playlist(stream_id: str):
        try:
            text = daemon.get_stream_playlist(stream_id, track="v")
        except DaemonError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return PlainTextResponse(text, media_type="application/vnd.apple.mpegurl")

    @app.get("/streams/{stream_id}/audio.m3u8")
    async def stream_audio_playlist(stream_id: str):
        try:
            text = daemon.get_stream_playlist(stream_id, track="a")
        except DaemonError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return PlainTextResponse(text, media_type="application/vnd.apple.mpegurl")

    @app.get("/streams/{stream_id}/segment/{track}/{seq}")
    async def stream_segment(stream_id: str, track: str, seq: int):
        try:
            data, media_type = daemon.get_stream_segment(stream_id, track, seq)
        except DaemonError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return Response(content=data, media_type=media_type)

    @app.delete("/streams/{stream_id}")
    async def delete_stream(stream_id: str):
        try:
            daemon.close_stream(stream_id)
        except DaemonError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"closed": stream_id}

    @app.get("/sources")
    async def sources():
        return daemon.sources_status()

    @app.post("/shutdown")
    async def shutdown():
        callback = daemon.on_idle
        if callback is None:
            raise HTTPException(status_code=409, detail="daemon has no shutdown callback registered (was it not started via serve?)")
        daemon.on_idle = None
        callback()
        return {"shutting_down": True}

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "rss_bytes": process_rss_bytes(),
            "active_requests": len(daemon.requests),
            "active_sources": len(daemon.sources),
            "active_streams": len(daemon.streams),
            "volatile_bytes": daemon._volatile_backend.store.bytes_used,
        }

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("hlsd.api:create_app", factory=True, host="127.0.0.1", port=8000, log_level="info")
