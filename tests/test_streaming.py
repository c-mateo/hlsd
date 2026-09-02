import httpx

from hlsd.config import DaemonConfig
from hlsd.memory import VolatileSegmentStore
from hlsd.models import RequestTemplate, Selectors, Track
from hlsd.net import HttpClient
from hlsd.source import StreamSource, VolatileBackend
from hlsd.streaming import read_segment, render_master_playlist, render_track_playlist


class FakeLiveStream:
    def __init__(self, playlist_url: str, segment_prefix: str, extinf: float = 2.0):
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
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{first}", "#EXT-X-MAP:URI=\"init.mp4\""]
        for _seq, uri, extinf in self.segments:
            lines.append(f"#EXTINF:{extinf},")
            lines.append(uri)
        return "\n".join(lines)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == self.playlist_url:
            self.publish()
            return httpx.Response(200, text=self.playlist_text())
        if url.endswith("init.mp4"):
            return httpx.Response(200, content=b"INITDATA")
        if url.startswith(self.segment_prefix):
            self.downloads[url] = self.downloads.get(url, 0) + 1
            return httpx.Response(200, content=b"SEGDATA:" + url.encode())
        return httpx.Response(404)


def make_source_with_two_segments(tmp_path) -> StreamSource:
    stream = FakeLiveStream("https://cdn/live/media.m3u8", "https://cdn/live/seg")
    config = DaemonConfig(data_dir=tmp_path / ".hlsd")
    net = HttpClient(RequestTemplate(url=stream.playlist_url), transport=httpx.MockTransport(stream.handler), max_retries=0)
    source = StreamSource("skey", net.template, Selectors(), VolatileBackend(VolatileSegmentStore(1 << 20)), net, config)

    async def _run():
        await source.resolve()
        await source._poll_track(Track.VIDEO)

    import asyncio

    asyncio.run(_run())
    return source


def test_render_playlist_rewrites_uris(tmp_path):
    source = make_source_with_two_segments(tmp_path)
    # single track: master == direct media playlist
    assert render_master_playlist(source) == render_track_playlist(source, Track.VIDEO)
    text = render_track_playlist(source, Track.VIDEO)
    assert text is not None
    pl = source.tracks[Track.VIDEO].playlist
    assert pl is not None
    assert "#EXTM3U" in text
    assert "#EXT-X-TARGETDURATION:2" in text
    assert f"#EXT-X-MEDIA-SEQUENCE:{pl.media_sequence}" in text
    assert '#EXT-X-MAP:URI="segment/v/-1"' in text
    assert 'segment/v/0' in text and 'segment/v/1' in text
    assert "https://cdn" not in text


def test_track_playlist_window_includes_downloaded_not_in_remote_window(tmp_path):
    """Extended window: segments the remote already dropped from its playlist
    but that the daemon downloaded are still listed (a lagging player finds
    its segments instead of freezing)."""
    import asyncio

    stream = FakeLiveStream("https://cdn/live/media.m3u8", "https://cdn/live/seg")
    config = DaemonConfig(data_dir=tmp_path / ".hlsd", stream_window=3)
    net = HttpClient(RequestTemplate(url=stream.playlist_url), transport=httpx.MockTransport(stream.handler), max_retries=0)
    source = StreamSource("skey", net.template, Selectors(), VolatileBackend(VolatileSegmentStore(1 << 20)), net, config)

    async def _run():
        await source.resolve()
        for _ in range(5):
            await source._poll_track(Track.VIDEO)

    asyncio.run(_run())
    rt = source.tracks[Track.VIDEO]
    assert rt.last_seq >= 4
    text = render_track_playlist(source, Track.VIDEO, max_window=config.stream_window)
    assert text is not None
    # only the last 3 seqs, with an advanced MEDIA-SEQUENCE
    assert "segment/v/0" not in text
    assert f"segment/v/{rt.last_seq}" in text
    assert f"#EXT-X-MEDIA-SEQUENCE:{rt.last_seq - 2}" in text


def test_master_playlist_with_separate_audio(tmp_path):
    """Source with separate audio: the master declares the audio group and
    references video.m3u8 / audio.m3u8 (the player plays with sound)."""
    import asyncio

    stream = FakeLiveStream("https://cdn/live/media.m3u8", "https://cdn/live/seg")
    stream.audio_url = "https://cdn/live/audio.m3u8"
    audio_stream = FakeLiveStream("https://cdn/live/audio.m3u8", "https://cdn/live/aseg")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == stream.playlist_url:
            stream.publish()
            return httpx.Response(200, text="\n".join(
                stream.playlist_text().splitlines()[:4]
                + ['#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="a",DEFAULT=YES,URI="audio.m3u8"',
                   '#EXT-X-STREAM-INF:BANDWIDTH=1000000,AUDIO="a"',
                   "video_only.m3u8"]
            ) + "\n")
        if url.endswith("video_only.m3u8"):
            return httpx.Response(200, text=stream.playlist_text())
        if url == audio_stream.playlist_url:
            audio_stream.publish()
            return httpx.Response(200, text=audio_stream.playlist_text())
        if url.endswith("init.mp4"):
            return httpx.Response(200, content=b"INITDATA")
        if url.startswith(audio_stream.segment_prefix):
            audio_stream.downloads[url] = audio_stream.downloads.get(url, 0) + 1
            return httpx.Response(200, content=b"ASEG")
        return stream.handler(request)

    config = DaemonConfig(data_dir=tmp_path / ".hlsd")
    net = HttpClient(RequestTemplate(url=stream.playlist_url), transport=httpx.MockTransport(handler), max_retries=0)
    source = StreamSource("skey", net.template, Selectors(), VolatileBackend(VolatileSegmentStore(1 << 20)), net, config)

    async def _run():
        await source.resolve()
        for track in source.tracks:
            await source._poll_track(track)

    asyncio.run(_run())

    master = render_master_playlist(source)
    assert master is not None
    assert '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud"' in master
    assert 'URI="audio.m3u8"' in master
    assert 'AUDIO="aud"' in master
    assert master.rstrip().endswith("video.m3u8")

    audio_text = render_track_playlist(source, Track.AUDIO)
    assert audio_text is not None
    assert "segment/a/0" in audio_text
    assert "ASEG" not in audio_text


def test_read_segment_from_volatile_backend(tmp_path):
    source = make_source_with_two_segments(tmp_path)
    data = read_segment(source, "v", 0)
    assert data is not None and b"SEGDATA" in data
    assert read_segment(source, "v", 9999) is None
