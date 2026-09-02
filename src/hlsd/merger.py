"""On-demand assembly with ffmpeg.

Automatic strategy:
1. fMP4 (EXT-X-MAP present): init + fragments are byte-concatenated into a
   single fMP4 (moof fragments are not individually demuxable).
   If the init is missing, it is recovered from the backend. TS: concat demuxer.
2. Separate video and audio: concat each track and final mux.
3. Fallback chain: stream-copy -> audio to AAC -> full re-encode.
4. Trimming of the excess (`-t`) for window/realtime/accumulated with surplus.

The result is always written to a file (never assembled fully in RAM).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .memory import malloc_trim
from .models import SegmentMeta, Track
from .request import DownloadRequest
from .source import SegmentBackend

log = logging.getLogger("hlsd.merger")


class MergeError(RuntimeError):
    pass


def _ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return None


def _has_ffmpeg() -> bool:
    return _ffmpeg_path() is not None


async def _run_ffmpeg(args: list[str]) -> None:
    ffmpeg = _ffmpeg_path()
    if ffmpeg is None:
        raise MergeError("ffmpeg is not available (install it or 'pip install imageio-ffmpeg')")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = (stderr or b"").decode(errors="ignore")[-800:]
        raise MergeError(f"ffmpeg failed ({proc.returncode}): {tail}")


def _write_list(path: Path, files: list[Path]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(f"file '{str(f).replace(chr(92), '/')}'\n" for f in files)


def _mp4_brand(path: Path) -> bytes:
    """Fourth field of the ISO-BMFF header: 'ftyp', 'styp', 'moof'... or b''."""
    try:
        with open(path, "rb") as fh:
            return fh.read(8)[4:8]
    except OSError:
        return b""


def _concat_bytes(files: list[Path], out: Path) -> None:
    """Byte concatenation (container-level stream copy).

    fMP4 fragments (moof+mdat) are NOT individually demuxable: the
    concat demuxer opens each file separately and needs a moov in each
    one => "Output file does not contain any stream". init (ftyp+moov) +
    concatenated fragments form a single valid fMP4."""
    with open(out, "wb") as fh:
        for f in files:
            with open(f, "rb") as src:
                while chunk := src.read(1 << 20):
                    fh.write(chunk)


def _fmp4_boxes(data: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        size = int.from_bytes(data[i : i + 4], "big")
        typ = data[i + 4 : i + 8]
        if size == 1:
            size = int.from_bytes(data[i + 8 : i + 16], "big")
        if size < 8:
            return
        yield typ, i, min(i + size, end)
        i += size


def _fmp4_timebase(path: Path) -> tuple[int, int] | None:
    """(first tfdt, timescale) of an fMP4 file (init + fragments)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(1 << 18)
    except OSError:
        return None
    end = len(data)
    timescale = None
    tfdt = None
    for typ, a, b in _fmp4_boxes(data, 0, end):
        if typ == b"moov":
            # timescale of the first mdhd (moov > trak > mdia > mdhd)
            for t2, a2, b2 in _fmp4_boxes(data, a + 8, b):
                if t2 == b"trak":
                    for t3, a3, b3 in _fmp4_boxes(data, a2 + 8, b2):
                        if t3 == b"mdia":
                            for t4, a4, b4 in _fmp4_boxes(data, a3 + 8, b3):
                                if t4 == b"mdhd":
                                    ver = data[a4 + 8]
                                    off = a4 + 12 + (16 if ver == 1 else 8)
                                    timescale = int.from_bytes(data[off : off + 4], "big")
        elif typ == b"moof" and tfdt is None:
            for t2, a2, b2 in _fmp4_boxes(data, a + 8, b):
                if t2 == b"traf":
                    for t3, a3, b3 in _fmp4_boxes(data, a2 + 8, b2):
                        if t3 == b"tfdt":
                            ver = data[a3 + 8]
                            off = a3 + 12
                            n = 8 if ver == 1 else 4
                            tfdt = int.from_bytes(data[off : off + n], "big")
    if timescale and tfdt is not None:
        return tfdt, timescale
    return None


def _av_offset(video: list[SegmentMeta], audio: list[SegmentMeta], video_file: Path, audio_file: Path) -> tuple[float | None, str]:
    """Seconds the audio track starts after the video track in absolute time.

    ffmpeg normalizes each input's start to zero and loses absolute time; the
    desync equals (pdt_audio - pdt_video) and is re-introduced with -itsoffset.
    Fallback: tfdt of the concatenated files (rehydrated requests without
    persisted pdt). Returns (offset, method) with method "pdt", "tfdt" or
    "none" (unknown)."""
    v0 = next((m.pdt for m in video if m.pdt is not None), None)
    a0 = next((m.pdt for m in audio if m.pdt is not None), None)
    if v0 is not None and a0 is not None:
        return a0 - v0, "pdt"
    v = _fmp4_timebase(video_file)
    a = _fmp4_timebase(audio_file)
    if v is None or a is None:
        return None, "none"
    return (a[0] / a[1]) - (v[0] / v[1]), "tfdt"


def _av_offset_override() -> float:
    """Manual A/V correction in seconds (HLSD_AV_OFFSET), added on top of the
    detected offset — for sources where auto-detection is off."""
    try:
        return float(os.environ.get("HLSD_AV_OFFSET", "0"))
    except ValueError:
        return 0.0


def _av_shift(offset: float) -> tuple[list[str], list[str]]:
    """(-itsoffset args before the video input, -itsoffset args before the
    audio input) that align the tracks.

    A positive offset means the audio starts later in absolute time, so the
    audio input is delayed by it. A negative offset (audio starts earlier)
    delays the video input instead: a negative audio shift produces negative
    timestamps that the muxer clamps when stream-copying, leaving the audio
    late (the observed 1-2s desync)."""
    if offset > 0:
        return [], ["-itsoffset", f"{offset:.3f}"]
    if offset < 0:
        return ["-itsoffset", f"{-offset:.3f}"], []
    return [], []


async def _concat(files: list[Path], out: Path, workdir: Path, label: str) -> None:
    list_file = workdir / f"list_{label}.txt"
    _write_list(list_file, files)
    await _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(out)])


async def merge_request(
    request: DownloadRequest,
    backend: SegmentBackend,
    out_path: Path,
    *,
    workdir: Path | None = None,
) -> Path:
    """Assembles the request's window into `out_path` and returns it."""
    if not _has_ffmpeg():
        raise MergeError("ffmpeg is not available (install it or 'pip install imageio-ffmpeg')")

    video = _ordered(request.segments[Track.VIDEO])
    if not video:
        raise MergeError("the request has no video segments yet")

    # fMP4: concatenating .m4s fragments without the init (EXT-X-MAP) yields
    # no streams ("Output file does not contain any stream"). The init may
    # not have reached the request (activated after the source bootstrap, or
    # daemon restarted), so we recover it from the backend.
    source_key = request.spec.template.resource_key(request.spec.selectors)
    if video[0].seq >= 0:
        init = backend.get_init_meta(source_key, Track.VIDEO)
        if init is not None:
            video.insert(0, init)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    trim = request.target_trim()
    count = request.target_segment_count()
    has_separate_audio = bool(request.segments[Track.AUDIO])
    audio = _ordered(request.segments[Track.AUDIO])
    if audio and audio[0].seq >= 0:
        a_init = backend.get_init_meta(source_key, Track.AUDIO)
        if a_init is not None:
            audio.insert(0, a_init)
    if count is not None:
        # Segments mode: hard cap by count; separate audio is trimmed to the
        # total duration of the trimmed video (sum of EXTINF).
        video = video[:count]
        audio_budget = sum(m.extinf for m in video)
        trimmed_audio: list[SegmentMeta] = []
        acc = 0.0
        for meta in audio:
            if acc >= audio_budget:
                break
            trimmed_audio.append(meta)
            acc += meta.extinf
        audio = trimmed_audio

    tmp_ctx = tempfile.TemporaryDirectory(dir=workdir)
    try:
        work = Path(tmp_ctx.name)
        v_files = await _materialize(video, backend, work, "v")
        a_files = await _materialize(audio, backend, work, "a") if has_separate_audio else []

        # fMP4 (init 'ftyp'/'styp' + 'moof' fragments): a single file with
        # everything byte-concatenated. TS (sync 0x47): concat demuxer.
        if v_files and _mp4_brand(v_files[0]) in (b"ftyp", b"styp"):
            int_v = work / "v_only.mp4"
            _concat_bytes(v_files, int_v)
        elif v_files and _mp4_brand(v_files[0]) == b"moof":
            raise MergeError(
                "fMP4 fragments without init segment (EXT-X-MAP): cannot assemble"
            )
        else:
            int_v = work / "v_only.ts"
            await _concat(v_files, int_v, work, "vcat")

        if a_files:
            if _mp4_brand(a_files[0]) in (b"ftyp", b"styp"):
                int_a = work / "a_only.m4a"
                _concat_bytes(a_files, int_a)
            else:
                int_a = work / "a_only.ts"
                await _concat(a_files, int_a, work, "acat")
            # A/V alignment: delay the earlier track by the absolute-time
            # difference (PDT or tfdt) so the shift is always positive
            # (a negative audio shift gets clamped by the muxer with -c copy)
            override = _av_offset_override()
            offset, method = _av_offset(video, audio, int_v, int_a)
            if override:
                offset = (offset or 0.0) + override
                method = f"{method}+override"
            v_shift, a_shift = _av_shift(offset) if offset is not None else ([], [])
            if v_shift or a_shift:
                log.info(
                    "A/V offset %.3fs (method: %s): shifting %s track",
                    offset, method, "video" if v_shift else "audio",
                )
            base_args = [*v_shift, "-i", str(int_v), *a_shift, "-i", str(int_a)]
            attempts = [
                base_args + ["-c", "copy"],
                base_args + ["-c:v", "copy", "-c:a", "aac"],
                base_args + ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"],
            ]
        else:
            base_args = ["-i", str(int_v)]
            attempts = [base_args + ["-c", "copy"]]

        last_error: Exception | None = None
        for args in attempts:
            final_args = args + (["-t", f"{trim:.3f}"] if trim else []) + [str(out_path)]
            try:
                await _run_ffmpeg(final_args)
                last_error = None
                break
            except MergeError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
    finally:
        tmp_ctx.cleanup()
        malloc_trim()
    return out_path


def _ordered(metas: list[SegmentMeta]) -> list[SegmentMeta]:
    return sorted(metas, key=lambda m: m.seq)


async def _materialize(metas: list[SegmentMeta], backend: SegmentBackend, work: Path, label: str) -> list[Path]:
    files: list[Path] = []
    for i, meta in enumerate(metas):
        data = backend.read(meta)
        # the suffix comes from the path without query/fragment ('?' is
        # invalid in Windows file names)
        suffix = Path(urlsplit(meta.uri).path).suffix or ".bin"
        path = work / f"{label}_{i:06d}{suffix}"
        path.write_bytes(data)
        files.append(path)
    return files
