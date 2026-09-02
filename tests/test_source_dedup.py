"""Core integration test: segment dedup between two overlapping requests
on the same Source (the key design case)."""

import httpx
import pytest

from hlsd.config import DaemonConfig
from hlsd.daemon import StreamDaemon, _spec_to_dict
from hlsd.memory import VolatileSegmentStore
from hlsd.models import DownloadMode, DownloadSpec, RequestTemplate, Selectors, Track
from hlsd.net import HttpClient
from hlsd.request import DownloadRequest
from hlsd.source import StreamSource, VolatileBackend


class FakeLiveStream:
    """Simulates a live HLS stream: each playlist fetch publishes a new segment."""

    def __init__(self, playlist_url: str, segment_prefix: str, extinf: float = 1.0):
        self.playlist_url = playlist_url
        self.segment_prefix = segment_prefix
        self.extinf = extinf
        self.segments: list[tuple[int, str, float]] = []
        self.downloads: dict[str, int] = {}

    def publish(self) -> str:
        seq = (self.segments[-1][0] + 1) if self.segments else 0
        uri = f"{self.segment_prefix}{seq}.m4s"
        self.segments.append((seq, uri, self.extinf))
        return uri

    def playlist_text(self) -> str:
        first = self.segments[0][0] if self.segments else 0
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:1", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for _seq, uri, extinf in self.segments:
            lines.append(f"#EXTINF:{extinf},")
            lines.append(uri)
        return "\n".join(lines)

    def total_unique_downloads(self) -> int:
        return sum(self.downloads.values())


def make_transport(streams: dict[str, FakeLiveStream]):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in streams:
            streams[url].publish()
            return httpx.Response(200, text=streams[url].playlist_text())
        for stream in streams.values():
            base = stream.segment_prefix
            if base in url and url.startswith(base):
                stream.downloads[url] = stream.downloads.get(url, 0) + 1
                return httpx.Response(200, content=b"SEGDATA:" + url.encode())
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def make_source(transport, tmp_path, url="https://cdn/live/media.m3u8") -> StreamSource:
    config = DaemonConfig(
        data_dir=tmp_path / ".hlsd", poll_interval_min=0.01, poll_interval_max=0.02
    )
    template = RequestTemplate(url=url)
    net = HttpClient(
        template, concurrency=4, timeout=5, max_retries=0, transport=transport
    )
    backend = VolatileBackend(VolatileSegmentStore(1 << 22))
    return StreamSource("testkey", template, Selectors(), backend, net, config)


async def test_two_overlapping_requests_dedup_segments(tmp_path):
    stream = FakeLiveStream("https://cdn/live/media.m3u8", "https://cdn/live/seg")
    source = make_source(
        make_transport({"https://cdn/live/media.m3u8": stream}), tmp_path
    )

    await source.resolve()  # bootstrap: publishes the first segment

    request_a = DownloadRequest(
        DownloadSpec(
            RequestTemplate(url="https://cdn/live/media.m3u8"),
            mode=DownloadMode.ACCUMULATED,
            duration=3.0,
        )
    )
    request_a.activate()
    source.add_request(request_a)

    request_b: DownloadRequest | None = None
    polls = 0
    while polls < 10 and not (
        request_a.is_satisfied() and request_b is not None and request_b.is_satisfied()
    ):
        await source._poll_track(Track.VIDEO)
        polls += 1
        if polls == 1:
            request_b = DownloadRequest(
                DownloadSpec(
                    RequestTemplate(url="https://cdn/live/media.m3u8"),
                    mode=DownloadMode.ACCUMULATED,
                    duration=2.0,
                )
            )
            request_b.activate()
            source.add_request(request_b)

    assert request_a.is_satisfied()
    assert request_b is not None and request_b.is_satisfied()

    a_seqs = {m.seq for m in request_a.segments[Track.VIDEO]}
    b_seqs = {m.seq for m in request_b.segments[Track.VIDEO]}
    assert request_a.stats.accumulated_duration >= 3.0
    assert request_b.stats.accumulated_duration == pytest.approx(2.0)
    assert b_seqs.issubset(a_seqs)

    assert stream.total_unique_downloads() == len(stream.segments)
    assert len(stream.segments) == polls + 2  # resolve() publishes 2 (template + media)

    await source.net.aclose()


async def test_master_with_separate_audio_track(tmp_path):
    master_text = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",DEFAULT=YES,URI="https://cdn/live/audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1.64001e"
https://cdn/live/video.m3u8
"""
    video_stream = FakeLiveStream(
        "https://cdn/live/video.m3u8", "https://cdn/live/vseg"
    )
    audio_stream = FakeLiveStream(
        "https://cdn/live/audio.m3u8", "https://cdn/live/aseg"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://cdn/live/master.m3u8":
            return httpx.Response(200, text=master_text)
        if url == video_stream.playlist_url:
            video_stream.publish()
            return httpx.Response(200, text=video_stream.playlist_text())
        if url == audio_stream.playlist_url:
            audio_stream.publish()
            return httpx.Response(200, text=audio_stream.playlist_text())
        for stream in (video_stream, audio_stream):
            if url.startswith(stream.segment_prefix):
                stream.downloads[url] = stream.downloads.get(url, 0) + 1
                return httpx.Response(200, content=b"SEG")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    source = make_source(transport, tmp_path, url="https://cdn/live/master.m3u8")
    await source.resolve()

    assert source.separate_audio is True
    assert set(source.tracks) == {Track.VIDEO, Track.AUDIO}

    await source._poll_track(Track.VIDEO)
    await source._poll_track(Track.AUDIO)

    assert video_stream.total_unique_downloads() >= 1
    assert audio_stream.total_unique_downloads() >= 1
    assert source.tracks[Track.VIDEO].last_seq >= 0
    assert source.tracks[Track.AUDIO].last_seq >= 0

    await source.net.aclose()


async def test_endlist_stops_track(tmp_path):
    stream = FakeLiveStream("https://cdn/live/media.m3u8", "https://cdn/live/seg")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == stream.playlist_url:
            stream.publish()
            text = stream.playlist_text() + "\n#EXT-X-ENDLIST"
            return httpx.Response(200, text=text)
        if url.startswith(stream.segment_prefix):
            stream.downloads[url] = stream.downloads.get(url, 0) + 1
            return httpx.Response(200, content=b"SEG")
        return httpx.Response(404)

    source = make_source(httpx.MockTransport(handler), tmp_path)
    await source.resolve()
    await source._poll_track(Track.VIDEO)
    assert source.tracks[Track.VIDEO].ended is True

    await source.net.aclose()


async def test_indefinite_request_finalizes_on_endlist(tmp_path, monkeypatch):
    """VOD with ENDLIST: an indefinite request finalizes by itself (done) with
    all the segments, without needing a stop."""
    import asyncio as aio

    from hlsd.config import DaemonConfig
    from hlsd.models import DownloadMode, DownloadSpec, RequestTemplate

    stream = FakeLiveStream("https://cdn/live/media.m3u8", "https://cdn/live/seg")

    # real fMP4 segment so the final merge works
    import subprocess

    from hlsd.merger import _ffmpeg_path, _has_ffmpeg

    if not _has_ffmpeg():
        pytest.skip("ffmpeg not available")
    await aio.to_thread(
        subprocess.run,
        [
            _ffmpeg_path(),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=0.5:size=64x64:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.5",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-movflags",
            "+frag_keyframe+empty_moov",
            "-shortest",
            str(tmp_path / "seg.mp4"),
        ],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    )
    seg_bytes = (tmp_path / "seg.mp4").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == stream.playlist_url:
            stream.publish()
            text = stream.playlist_text() + "\n#EXT-X-ENDLIST"
            return httpx.Response(200, text=text)
        if url.startswith(stream.segment_prefix):
            stream.downloads[url] = stream.downloads.get(url, 0) + 1
            return httpx.Response(200, content=seg_bytes)
        return httpx.Response(404)

    config = DaemonConfig(data_dir=tmp_path / ".hlsd")
    daemon = StreamDaemon(config)

    # inject the mock transport into all httpx.AsyncClient instances of the daemon
    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    await daemon.start()
    try:
        spec = DownloadSpec(
            RequestTemplate(url="https://cdn/live/media.m3u8"),
            mode=DownloadMode.ACCUMULATED,
            duration=None,  # indefinite
        )
        request = DownloadRequest(spec)
        daemon.requests[request.id] = request
        daemon.store.save_request(request.id, _spec_to_dict(spec), request.state.value)
        await daemon._activate(request)
        runtime = daemon.sources[spec.template.resource_key(spec.selectors)]
        await runtime.task  # the source runs until ENDLIST and finishes
        await aio.sleep(0.1)

        assert request.state.value == "done", request.error
        assert request.result_path is not None
        assert request.stats.segments_video >= 1
    finally:
        await daemon.shutdown()


async def test_vod_without_endlist_finalizes_via_playlist_type(tmp_path, monkeypatch):
    """PLAYLIST-TYPE:VOD without ENDLIST: the track ends anyway and the
    indefinite request finalizes as done."""
    import asyncio as aio

    from hlsd.config import DaemonConfig
    from hlsd.daemon import _spec_to_dict
    from hlsd.models import DownloadMode, DownloadSpec, RequestTemplate

    stream = FakeLiveStream("https://cdn/live/media.m3u8", "https://cdn/live/seg")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == stream.playlist_url:
            stream.publish()
            text = stream.playlist_text().replace(
                "#EXTM3U", "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD", 1
            )
            return httpx.Response(200, text=text)
        if url.startswith(stream.segment_prefix):
            stream.downloads[url] = stream.downloads.get(url, 0) + 1
            return httpx.Response(200, content=b"SEG")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    daemon = StreamDaemon(DaemonConfig(data_dir=tmp_path / ".hlsd"))
    await daemon.start()
    try:
        spec = DownloadSpec(
            RequestTemplate(url="https://cdn/live/media.m3u8"),
            mode=DownloadMode.ACCUMULATED,
            duration=None,
        )
        request = DownloadRequest(spec)
        daemon.requests[request.id] = request
        daemon.store.save_request(request.id, _spec_to_dict(spec), request.state.value)
        await daemon._activate(request)
        runtime = daemon.sources[spec.template.resource_key(spec.selectors)]
        await runtime.task
        await aio.sleep(0.1)
        # no ENDLIST but PLAYLIST-TYPE:VOD => it ended (the merge fails because
        # the segments are garbage, but the state proves the detection)
        assert request.state.value in ("failed", "done")
        assert request.state.value != "active"
    finally:
        await daemon.shutdown()


async def test_event_playlist_keeps_polling(tmp_path):
    """PLAYLIST-TYPE:EVENT: the list grows without ENDLIST; the source does NOT
    end and keeps polling waiting for more segments."""

    from hlsd.config import DaemonConfig
    from hlsd.memory import VolatileSegmentStore
    from hlsd.models import Track
    from hlsd.net import HttpClient
    from hlsd.source import StreamSource, VolatileBackend

    stream = FakeLiveStream("https://cdn/live/media.m3u8", "https://cdn/live/seg")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == stream.playlist_url:
            stream.publish()
            text = stream.playlist_text().replace(
                "#EXTM3U", "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT", 1
            )
            return httpx.Response(200, text=text)
        if url.startswith(stream.segment_prefix):
            stream.downloads[url] = stream.downloads.get(url, 0) + 1
            return httpx.Response(200, content=b"SEG")
        return httpx.Response(404)

    config = DaemonConfig(
        data_dir=tmp_path / ".hlsd", poll_interval_min=0.01, poll_interval_max=0.02
    )
    net = HttpClient(
        RequestTemplate(url=stream.playlist_url),
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )
    source = StreamSource(
        "evt",
        net.template,
        Selectors(),
        VolatileBackend(VolatileSegmentStore(1 << 20)),
        net,
        config,
    )
    await source.resolve()
    for _ in range(3):
        await source._poll_track(Track.VIDEO)
    assert source.tracks[Track.VIDEO].ended is False
    assert source.tracks[Track.VIDEO].last_seq >= 0
    await source.net.aclose()


async def test_master_with_audio_group_selects_separate_audio(tmp_path):
    """Master with AUDIO="<group>" + CODECS listing mp4a: the video chunklist is
    video-only and the audio must be downloaded separately."""
    import httpx as hx

    master = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio_aac",NAME="Audio",DEFAULT=YES,AUTOSELECT=YES,CHANNELS="2",URI="/audio.m3u8"\n\n#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="audio_aac"\n/video.m3u8'
    video_pl = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nseg0.m4s\n#EXTINF:2.0,\nseg1.m4s"
    audio_pl = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\na0.m4s"

    def handler(request: hx.Request) -> hx.Response:
        url = str(request.url)
        if url.endswith("master.m3u8"):
            return hx.Response(200, text=master)
        if url.endswith("video.m3u8"):
            return hx.Response(200, text=video_pl)
        if url.endswith("audio.m3u8"):
            return hx.Response(200, text=audio_pl)
        return hx.Response(200, content=b"SEG")

    source = make_source(hx.MockTransport(handler), tmp_path, url="https://cdn/live/master.m3u8")
    await source.resolve()
    assert source.separate_audio is True
    assert Track.AUDIO in source.tracks
    assert source.tracks[Track.VIDEO].media_url.endswith("video.m3u8")
    assert source.tracks[Track.AUDIO].media_url.endswith("audio.m3u8")
    await source.net.aclose()


async def test_live_media_sequence_offset(tmp_path):
    """Live whose MEDIA-SEQUENCE starts high (sliding window, e.g. 907):
    last_seq must anchor to the real start of the window — if it anchors at 0
    it never advances and render_playlist exposes no segment."""
    stream = FakeLiveStream("https://cdn/live/media.m3u8", "https://cdn/live/seg")
    stream.segments.append((906, "https://cdn/live/seg906.m4s", 1.0))  # already out of the window

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == stream.playlist_url:
            stream.publish()  # first publishes 907
            return httpx.Response(200, text=stream.playlist_text())
        if url.startswith(stream.segment_prefix):
            stream.downloads[url] = stream.downloads.get(url, 0) + 1
            return httpx.Response(200, content=b"SEG")
        return httpx.Response(404)

    source = make_source(httpx.MockTransport(handler), tmp_path)
    await source.resolve()
    await source._poll_track(Track.VIDEO)
    await source._poll_track(Track.VIDEO)
    rt = source.tracks[Track.VIDEO]
    assert rt.last_seq >= 907

    from hlsd.streaming import render_track_playlist

    text = render_track_playlist(source, Track.VIDEO)
    assert text is not None
    assert "#EXTINF" in text
    assert "segment/v/907" in text
    await source.net.aclose()
