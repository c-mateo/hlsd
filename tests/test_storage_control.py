"""Storage control: streams default to the volatile (memory) backend, and
persisted-but-unused disk segments are pruned down to a per-source budget."""

import asyncio
from types import SimpleNamespace

from hlsd.config import DaemonConfig
from hlsd.daemon import StreamDaemon
from hlsd.memory import VolatileSegmentStore
from hlsd.models import DownloadSpec, RequestTemplate, SegmentMeta, Track
from hlsd.request import DownloadRequest
from hlsd.source import DiskBackend, VolatileBackend


def make_daemon(tmp_path, **config_overrides) -> StreamDaemon:
    config = DaemonConfig(data_dir=tmp_path / ".hlsd", **config_overrides)
    return StreamDaemon(config)


def put_segments(daemon: StreamDaemon, source_key: str, track: Track, seqs, size: int = 1024):
    backend = DiskBackend(daemon.config, daemon.store)
    for seq in seqs:
        asyncio.run(backend.put(source_key, track, seq, f"seg{seq}.m4s", 2.0, b"x" * size))


def seg_path(daemon: StreamDaemon, source_key: str, track: Track, seq: int):
    row = daemon.store.get_segment_row(source_key, track.value, seq)
    return None if row is None else row["path"]


def test_stream_volatile_defaults_to_true():
    assert DaemonConfig().stream_volatile is True


def test_env_bool_parses_daemon_style_values(monkeypatch):
    from hlsd.config import _env_bool

    monkeypatch.setenv("HLSD_X", "0")
    assert _env_bool("HLSD_X", True) is False
    monkeypatch.setenv("HLSD_X", "true")
    assert _env_bool("HLSD_X", False) is True
    monkeypatch.delenv("HLSD_X")
    assert _env_bool("HLSD_X", True) is True


def test_create_stream_defaults_to_volatile_backend(tmp_path):
    daemon = make_daemon(tmp_path)
    captured = {}

    async def fake_runtime(spec):
        captured["volatile"] = spec.volatile
        backend = daemon._volatile_backend if spec.volatile else daemon._disk_backend
        source = SimpleNamespace(key="k", requests={}, tracks={}, backend=backend, add_request=lambda request: None)
        return SimpleNamespace(source=source, task=None, net=None)  # type: ignore[return-value]

    daemon._get_source_runtime = fake_runtime  # type: ignore[method-assign]
    asyncio.run(daemon.create_stream(url="https://x/live.m3u8"))
    assert captured["volatile"] is True

    asyncio.run(daemon.create_stream(url="https://x/live.m3u8", volatile=False))
    assert captured["volatile"] is False

    asyncio.run(daemon.create_stream(url="https://x/live.m3u8", volatile=True))
    assert captured["volatile"] is True


def test_backend_for_follows_source_not_spec(tmp_path):
    """A recording request (volatile=False) sharing a stream-created source
    must merge through the source's volatile backend, where its segments live."""
    daemon = make_daemon(tmp_path)
    spec = DownloadSpec(RequestTemplate(url="https://x/live.m3u8"), volatile=False)
    request = DownloadRequest(spec)

    source_key = spec.template.resource_key(spec.selectors)
    backend = VolatileBackend(VolatileSegmentStore(1 << 20))
    daemon.sources[source_key] = SimpleNamespace(source=SimpleNamespace(backend=backend))  # type: ignore[assignment]
    assert daemon._backend_for(request) is backend


def test_prune_evicts_oldest_unreferenced_over_budget(tmp_path):
    daemon = make_daemon(tmp_path, source_disk_budget=4096)
    put_segments(daemon, "src", Track.VIDEO, range(10))
    assert daemon.store.segment_bytes_by_source()["src"] == 10 * 1024

    daemon._prune_disk_segments()

    assert daemon.store.get_segment_seqs("src", "v") == {6, 7, 8, 9}
    for seq in (6, 7, 8, 9):
        assert seg_path(daemon, "src", Track.VIDEO, seq) is not None
    for seq in range(6):
        assert seg_path(daemon, "src", Track.VIDEO, seq) is None
    assert daemon.store.segment_bytes_by_source()["src"] == 4 * 1024


def test_prune_keeps_referenced_and_init_segments(tmp_path):
    daemon = make_daemon(tmp_path, source_disk_budget=2048)
    put_segments(daemon, "src", Track.VIDEO, [-1, 0, 1, 2, 3, 4, 5], size=1024)

    spec = DownloadSpec(RequestTemplate(url="https://x/live.m3u8"), volatile=False)
    request = DownloadRequest(spec)
    for seq in (0, 1):
        request.segments[Track.VIDEO].append(
            SegmentMeta(source_key="src", track=Track.VIDEO, seq=seq, uri=f"seg{seq}.m4s", extinf=2.0, size=1024)
        )
    daemon.requests[request.id] = request

    daemon._prune_disk_segments()

    # seqs 0-1 are referenced and -1 is the init segment: everything else
    # goes until the 2KB budget fits (2 segments)
    assert daemon.store.get_segment_seqs("src", "v") == {-1, 0, 1}


def test_prune_disabled_when_budget_zero(tmp_path):
    daemon = make_daemon(tmp_path, source_disk_budget=0)
    put_segments(daemon, "src", Track.VIDEO, range(5))
    daemon._prune_disk_segments()
    assert daemon.store.get_segment_seqs("src", "v") == {0, 1, 2, 3, 4}


def test_prune_removes_known_seqs_from_live_source(tmp_path):
    daemon = make_daemon(tmp_path, source_disk_budget=2048)
    put_segments(daemon, "src", Track.VIDEO, range(6))
    source = SimpleNamespace(
        key="src",
        requests={},
        tracks={Track.VIDEO: SimpleNamespace(known_seqs={0, 1, 2, 3, 4, 5})},
        backend=DiskBackend(daemon.config, daemon.store),
    )
    daemon.sources["src"] = SimpleNamespace(source=source)  # type: ignore[assignment]

    daemon._prune_disk_segments()

    assert source.tracks[Track.VIDEO].known_seqs == {4, 5}
