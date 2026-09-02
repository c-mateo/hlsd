import subprocess
from pathlib import Path

import pytest

from hlsd.merger import _av_shift, _ffmpeg_path, _has_ffmpeg, _write_list, merge_request
from hlsd.models import DownloadMode, DownloadSpec, RequestTemplate, SegmentMeta, Track
from hlsd.request import DownloadRequest
from hlsd.source import SegmentBackend


def test_av_shift_positive_delays_audio():
    # audio starts later in absolute time: delay the audio input
    v_shift, a_shift = _av_shift(1.5)
    assert v_shift == []
    assert a_shift == ["-itsoffset", "1.500"]


def test_av_shift_negative_delays_video():
    # audio starts earlier: delay the video input (a negative audio shift
    # produces negative timestamps that the muxer clamps with -c copy)
    v_shift, a_shift = _av_shift(-1.5)
    assert v_shift == ["-itsoffset", "1.500"]
    assert a_shift == []


def test_av_shift_zero_is_noop():
    assert _av_shift(0.0) == ([], [])


def test_write_list_format(tmp_path):
    list_file = tmp_path / "list.txt"
    files = [Path("/a/b/seg0.m4s"), Path("/a/b/seg1.m4s")]
    _write_list(list_file, files)
    content = list_file.read_text()
    assert "file '/a/b/seg0.m4s'" in content
    assert "file '/a/b/seg1.m4s'" in content


def make_request_with_segments(tmp_path, mode=DownloadMode.WINDOW, duration=10.0):
    request = DownloadRequest(DownloadSpec(RequestTemplate(url="https://x/live.m3u8"), mode=mode, duration=duration))
    request.activate()
    for seq in range(3):
        path = tmp_path / f"seg{seq}.m4s"
        path.write_bytes(b"\x00" * 16)
        meta = SegmentMeta(
            source_key="k",
            track=Track.VIDEO,
            seq=seq,
            uri=f"https://x/seg{seq}.m4s",
            extinf=4.0,
            size=16,
            path=str(path),
        )
        request.segments[Track.VIDEO].append(meta)
    return request


class NullBackend(SegmentBackend):
    def read(self, meta):
        return Path(meta.path).read_bytes()

    async def put(self, *args, **kwargs):
        raise NotImplementedError

    def release(self, source_key):
        pass


def test_merge_without_ffmpeg_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr("hlsd.merger._has_ffmpeg", lambda: False)
    request = make_request_with_segments(tmp_path)
    with pytest.raises(Exception, match="ffmpeg"):
        import asyncio

        asyncio.run(merge_request(request, NullBackend(), tmp_path / "out.mp4"))


def test_merge_with_ffmpeg_concat(tmp_path):
    if not _has_ffmpeg():
        pytest.skip("ffmpeg not available in this environment")
    import asyncio

    ffmpeg = _ffmpeg_path()
    assert ffmpeg is not None
    seg_dir = tmp_path / "segs"
    seg_dir.mkdir()
    files = []
    for seq in range(3):
        seg = seg_dir / f"seg{seq}.mp4"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=duration=0.5:size=64x64:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
             "-movflags", "+frag_keyframe+empty_moov", "-shortest", str(seg)],
            check=True, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        )
        files.append(seg)

    request = DownloadRequest(DownloadSpec(RequestTemplate(url="https://x/live.m3u8"), mode=DownloadMode.ACCUMULATED, duration=1.0))
    request.activate()
    for seq, seg in enumerate(files):
        request.segments[Track.VIDEO].append(
            SegmentMeta(source_key="k", track=Track.VIDEO, seq=seq, uri=f"https://x/seg{seq}.m4s", extinf=0.5, size=seg.stat().st_size, path=str(seg))
        )

    class LocalBackend(SegmentBackend):
        def read(self, meta):
            return Path(meta.path).read_bytes()

        async def put(self, *args, **kwargs):
            raise NotImplementedError

        def release(self, source_key):
            pass

    out = asyncio.run(merge_request(request, LocalBackend(), tmp_path / "out.mp4"))
    assert out.exists()
    assert out.stat().st_size > 0


def _ffmpeg_duration(path: Path) -> float:
    """Container duration in seconds, parsed from `ffmpeg -i` stderr."""
    ffmpeg = _ffmpeg_path()
    assert ffmpeg is not None
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for line in proc.stderr.decode(errors="ignore").splitlines():
        if "Duration:" in line:
            stamp = line.split("Duration:")[1].split(",")[0].strip()
            hh, mm, ss = stamp.split(":")
            return int(hh) * 3600 + int(mm) * 60 + float(ss)
    raise AssertionError(f"no Duration line for {path}")


def test_merge_applies_av_offset_from_pdt(tmp_path):
    """Audio starting 1s after the video in absolute time (PROGRAM-DATE-TIME)
    must be delayed by the detected offset: the muxed output then lasts
    video + offset (audio shifted positively; a negative shift would be
    clamped by the muxer with -c copy)."""
    if not _has_ffmpeg():
        pytest.skip("ffmpeg not available in this environment")
    import asyncio

    ffmpeg = _ffmpeg_path()
    assert ffmpeg is not None
    seg_dir = tmp_path / "segs"
    seg_dir.mkdir()
    v_file = seg_dir / "v.mp4"
    a_file = seg_dir / "a.mp4"
    common = ["-movflags", "+frag_keyframe+empty_moov"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=2:size=64x64:rate=10",
         "-c:v", "libx264", "-preset", "ultrafast", *common, str(v_file)],
        check=True, capture_output=True, creationflags=flags,
    )
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:a", "aac", *common, str(a_file)],
        check=True, capture_output=True, creationflags=flags,
    )

    request = DownloadRequest(DownloadSpec(RequestTemplate(url="https://x/live.m3u8"), mode=DownloadMode.ACCUMULATED, duration=10.0))
    request.activate()
    for track, path, pdt in ((Track.VIDEO, v_file, 1000.0), (Track.AUDIO, a_file, 1001.0)):
        request.segments[track].append(
            SegmentMeta(source_key="k", track=track, seq=0, uri=f"https://x/{track.value}0.m4s", extinf=2.0, size=path.stat().st_size, path=str(path), pdt=pdt)
        )

    class LocalBackend(SegmentBackend):
        def read(self, meta):
            return Path(meta.path).read_bytes()

        async def put(self, *args, **kwargs):
            raise NotImplementedError

        def release(self, source_key):
            pass

    out = asyncio.run(merge_request(request, LocalBackend(), tmp_path / "out.mp4"))
    duration = _ffmpeg_duration(out)
    # video 2s from t=0; audio detected 1s late via tfdt and delayed 1s -> 3s
    assert 2.7 <= duration <= 3.4, f"expected ~3s (audio delayed by tfdt offset), got {duration:.2f}s"
