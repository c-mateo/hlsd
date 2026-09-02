import time

from hlsd.models import DownloadMode, DownloadSpec, RequestTemplate, SegmentMeta, Track
from hlsd.request import DownloadRequest


def make_spec(mode: DownloadMode, duration) -> DownloadSpec:
    return DownloadSpec(template=RequestTemplate(url="https://x/live.m3u8?t=1"), mode=mode, duration=duration)


def make_seg(seq: int, extinf: float = 4.0, fetched_mono: float | None = None) -> SegmentMeta:
    return SegmentMeta(
        source_key="k",
        track=Track.VIDEO,
        seq=seq,
        uri=f"https://x/seg{seq}.m4s",
        extinf=extinf,
        size=100,
        fetched_mono=fetched_mono if fetched_mono is not None else time.monotonic(),
    )


def test_accumulated_counts_extinf_and_finishes():
    request = DownloadRequest(make_spec(DownloadMode.ACCUMULATED, 12.0))
    request.activate()
    request.assign_segment(make_seg(0))
    request.assign_segment(make_seg(1))
    assert not request.is_satisfied()
    request.assign_segment(make_seg(2))
    assert request.is_satisfied()
    assert request.stats.accumulated_duration == 12.0


def test_window_starts_at_first_segment():
    request = DownloadRequest(make_spec(DownloadMode.WINDOW, 6.0))
    request.activate()
    t0 = time.monotonic()
    request.assign_segment(make_seg(0, fetched_mono=t0))
    assert not request.is_satisfied()
    request.first_segment_mono = t0 - 10
    assert request.is_satisfied()
    assert request.target_trim() == 6.0


def test_realtime_counts_from_activation_not_first_segment():
    request = DownloadRequest(make_spec(DownloadMode.REALTIME, 5.0))
    request.activate()
    request.activated_mono = time.monotonic() - 10
    request.assign_segment(make_seg(0))
    assert request.is_satisfied()


def test_indefinite_never_satisfied_until_stop():
    request = DownloadRequest(make_spec(DownloadMode.ACCUMULATED, None))
    request.activate()
    for seq in range(10):
        request.assign_segment(make_seg(seq))
    assert not request.is_satisfied()
    request.request_stop()
    assert request.is_satisfied()
    assert request.target_trim() is None


def test_segments_before_activation_are_ignored():
    request = DownloadRequest(make_spec(DownloadMode.ACCUMULATED, 8.0))
    request.activate()
    request.assign_segment(make_seg(0, fetched_mono=-100.0))
    assert request.stats.segments_video == 0
    assert not request.is_satisfied()


def test_no_duplicate_assignment():
    request = DownloadRequest(make_spec(DownloadMode.ACCUMULATED, 4.0))
    request.activate()
    seg = make_seg(0)
    assert request.assign_segment(seg) is True
    assert request.assign_segment(seg) is False
    assert request.stats.segments_video == 1


def test_segments_mode_finishes_after_n_video_segments():
    request = DownloadRequest(make_spec(DownloadMode.SEGMENTS, 3))
    request.activate()
    for seq in range(2):
        request.assign_segment(make_seg(seq))
    assert not request.is_satisfied()
    request.assign_segment(make_seg(2))
    assert request.is_satisfied()
    assert request.target_segment_count() == 3
    assert request.target_trim() is None
    assert request.time_remaining() == 0.0


def test_segments_mode_ignores_audio_count():
    request = DownloadRequest(make_spec(DownloadMode.SEGMENTS, 1))
    request.activate()
    audio = SegmentMeta(
        source_key="k", track=Track.AUDIO, seq=0,
        uri="https://x/a0.m4s", extinf=2.0, size=50,
        fetched_mono=time.monotonic(),
    )
    request.assign_segment(audio)
    assert not request.is_satisfied()
    request.assign_segment(make_seg(0))
    assert request.is_satisfied()


def test_segments_mode_indefinite():
    request = DownloadRequest(make_spec(DownloadMode.SEGMENTS, None))
    request.activate()
    for seq in range(10):
        request.assign_segment(make_seg(seq))
    assert not request.is_satisfied()
