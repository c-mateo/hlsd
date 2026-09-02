"""Live streaming of a Source.

A rewritten HLS playlist pointing to /streams/{sid}/segment/... for
hls.js, VLC or ffplay. The remote upstream is touched only once: all
consumers (tabs, players) and recordings share the same Source.

Two playlist forms:
- Master (if there is a separate audio track): declares the audio group and
  references the daemon's video.m3u8 / audio.m3u8.
- Media (a single track, or the individual tracks): already-downloaded
  segments. The exposed window is longer than the remote's
  (config.stream_window): a lagging tab can catch up instead of freezing.
"""

from __future__ import annotations

import logging

from .models import Track
from .source import DiskBackend, StreamSource, VolatileBackend

M3U8_MEDIA_TYPE = "application/vnd.apple.mpegurl"
log = logging.getLogger("hlsd.streaming")


def read_segment(source: StreamSource, track_value: str, seq: int) -> bytes | None:
    if isinstance(source.backend, VolatileBackend):
        return source.backend.store.get((source.key, track_value, seq))
    if isinstance(source.backend, DiskBackend):
        row = source.backend.store.get_segment_row(source.key, track_value, seq)
        if row is None:
            return None
        from pathlib import Path

        try:
            return Path(row["path"]).read_bytes()
        except OSError:
            return None
    return None


def _available_seqs(source: StreamSource, track_value: str) -> list[int]:
    """Seqs of already-downloaded segments (excludes the init -1), sorted."""
    rt = source.tracks.get(Track(track_value))
    if rt is None:
        return []
    return sorted(s for s in rt.known_seqs if s >= 0)


def _track_extinf(rt) -> dict[int, float]:
    """EXTINF per seq of the remote's current window (the oldest ones fall
    out; for those the target duration is used as an estimate)."""
    playlist = rt.playlist
    out: dict[int, float] = {}
    if playlist is None:
        return out
    base_seq = playlist.media_sequence
    for offset, (_uri, extinf, _pdt) in enumerate(playlist.segments):
        out[base_seq + offset] = extinf
    return out


def render_track_playlist(
    source: StreamSource,
    track: Track,
    *,
    max_window: int | None = None,
) -> str | None:
    """Live media playlist whose URIs point to /streams/{sid}/segment/...

    Exposes all downloaded segments (limited to the last `max_window` units
    if the stream is live), not just the remote's current window: lagging
    consumers find their segments.
    """
    rt = source.tracks.get(track)
    if rt is None or rt.playlist is None:
        return None
    track_value = track.value
    seqs = _available_seqs(source, track_value)
    if not seqs:
        return None
    if max_window is not None and len(seqs) > max_window:
        seqs = seqs[-max_window:]

    playlist = rt.playlist
    target = playlist.target_duration or playlist.part_hold_back or 4.0
    extinf_by_seq = _track_extinf(rt)
    base_seq = seqs[0]
    lines = [
        "#EXTM3U",
        f"#EXT-X-TARGETDURATION:{max(1, round(target))}",
        "#EXT-X-VERSION:6",
        f"#EXT-X-MEDIA-SEQUENCE:{base_seq}",
    ]
    if source_has_init(source, track_value):
        lines.append(f'#EXT-X-MAP:URI="segment/{track_value}/-1"')
    for seq in seqs:
        extinf = extinf_by_seq.get(seq) or target
        lines.append(f"#EXTINF:{extinf:.3f},")
        lines.append(f"segment/{track_value}/{seq}")
    if playlist.endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def render_master_playlist(source: StreamSource) -> str | None:
    """Playlist for players: master with an audio group if there is a separate
    audio track; direct media playlist if there is only one track."""
    video = Track.VIDEO if Track.VIDEO in source.tracks else next(iter(source.tracks), None)
    if video is None:
        return None
    video_rt = source.tracks[video]
    audio = source.tracks.get(Track.AUDIO)
    if video_rt.playlist is None or audio is None or audio.playlist is None:
        return render_track_playlist(source, video)

    target = video_rt.playlist.target_duration or video_rt.playlist.part_hold_back or 2.0
    # bandwidth estimate (single variant: informational, not used to choose
    # between variants)
    bandwidth = max(500_000, round(target * 2_500_000))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="audio",DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"',
        f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},AUDIO="aud"',
        "video.m3u8",
    ]
    return "\n".join(lines) + "\n"


def source_has_init(source: StreamSource, track_value: str) -> bool:
    if isinstance(source.backend, VolatileBackend):
        return source.backend.store.has((source.key, track_value, -1))
    if isinstance(source.backend, DiskBackend):
        return -1 in source.backend.store.get_segment_seqs(source.key, track_value)
    return False


def guess_segment_media_type(uri: str) -> str:
    lower = uri.lower()
    if lower.endswith(".ts"):
        return "video/mp2t"
    return "video/mp4"
