"""HLS playlist parsing: master, media, variants, EXT-X-MEDIA, LL-HLS."""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

_MASTER_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_MAP_URI = re.compile(r'URI="([^"]+)"')

log = logging.getLogger("hlsd.hls")

def looks_like_m3u8(text: str) -> bool:
    return text.lstrip().startswith("#EXTM3U")


def _unescape_url(candidate: str) -> str:
    """Undoes common escapes of URLs embedded in JS/JSON: \\u002F,
    \\u0026, \\/, etc."""
    candidate = candidate.replace("\\/", "/")
    # unicode_escape can corrupt non-ASCII characters; we only use the
    # result if it still looks like a reasonable URL.
    with contextlib.suppress(ValueError):
        decoded = candidate.encode().decode("unicode_escape", errors="ignore")
        if decoded.startswith(("http://", "https://", "//")):
            return decoded
    return candidate


def extract_m3u8_from_page(text: str, page_url: str) -> str | None:
    """Heuristic (from get_url.py / stream_daemon.py) to find an .m3u8 URL
    in an HTML page, tolerating JS/JSON escapes (\\u002F) and relative
    paths."""
    idx = text.find(".m3u8")
    if idx != -1:
        # the URL may be scheme-relative (//cdn/x.m3u8) or absolute;
        # we search backwards for the most plausible start of the literal.
        # "https:" without // covers URLs with escaped slashes (\u002F).
        for probe in ("https://", "http://", "https:", "http:", "//"):
            start = text.rfind(probe, max(0, idx - 400), idx)
            if start != -1:
                break

        if start != -1:
            # End of the literal: real or escaped quotes (\u0022, \u0027),
            # whitespace, parentheses or tag. A loose backslash does NOT cut:
            # escapes like \u003D (=) or \u002D (-) are part of the URL.
            end_candidates = [text.find(marker, idx) for marker in ('\\u0022', '\\u0027', '"', "'", " ", ")", "<")]
            ends = [e for e in end_candidates if e != -1]
            end = min(ends) if ends else idx + 5
            candidate = _unescape_url(text[start:end])
            return urljoin(page_url, candidate)
    # Absolute URL with the .m3u8 further down the line (undoes a split
    # \u002F: https:\u002F\u002Fcdn\u002Fstream.m3u8 does not contain ".m3u8"
    # as-is, so we search for the unescaped pattern). The span allows
    # \u00XX escapes except quotes (\u0022, \u0027), which do end the URL.
    unescaped_spans = list(re.finditer(
        r'https?(?::|\\u002F)(?://|\\u002F)(?:\\u00(?!22|27)[0-9a-fA-F]{2}|[^\s"\'<>\\])+',
        text,
    ))
    for span in reversed(unescaped_spans):
        candidate = _unescape_url(span.group(0))
        if ".m3u8" in candidate:
            return urljoin(page_url, candidate)
    matches = re.findall(r'["\']([^"\']+\.m3u8[^"\']*)["\']', text)
    if matches:
        return urljoin(page_url, matches[-1].replace("\\/", "/"))
    return None


@dataclass
class Variant:
    uri: str
    bandwidth: int = 0
    width: int | None = None
    height: int | None = None
    codecs: str | None = None
    audio_group: str | None = None

    @property
    def has_audio_codec(self) -> bool:
        if not self.codecs:
            return False
        codecs = self.codecs.lower()
        return any(codec in codecs for codec in ("mp4a", "aac", "ac-3", "ec-3", "opus", "flac", "alac"))


@dataclass
class AudioRendition:
    group_id: str
    name: str = ""
    language: str | None = None
    uri: str | None = None
    channels: str | None = None
    default: bool = False


@dataclass
class MasterInfo:
    variants: list[Variant] = field(default_factory=list)
    renditions: list[AudioRendition] = field(default_factory=list)


@dataclass
class MediaPlaylist:
    media_sequence: int = 0
    target_duration: float = 0.0
    segments: list[tuple[str, float, float | None]] = field(default_factory=list)
    map_uri: str | None = None
    endlist: bool = False
    playlist_type: str | None = None  # VOD | EVENT | None (pure live)
    is_ll: bool = False
    part_hold_back: float | None = None
    can_block_reload: bool = False
    version: int | None = None

    @property
    def is_complete(self) -> bool:
        """Explicit VOD: ENDLIST or PLAYLIST-TYPE:VOD (no more segments will come)."""
        return self.endlist or self.playlist_type == "VOD"


def _parse_attrs(line: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    _name, _, rest = line.partition(":")
    for match in _MASTER_ATTR.finditer(rest):
        key = match.group(1)
        value = match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attrs[key] = value
    return attrs


def parse_master(text: str, base_url: str) -> MasterInfo:
    master = MasterInfo()
    lines = text.splitlines()
    pending: dict[str, str] | None = None
    for line in lines:
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = _parse_attrs(line)
        elif line.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_attrs(line)
            if attrs.get("TYPE", "").upper() == "AUDIO":
                uri = attrs.get("URI")
                master.renditions.append(
                    AudioRendition(
                        group_id=attrs.get("GROUP-ID", ""),
                        name=attrs.get("NAME", ""),
                        language=attrs.get("LANGUAGE"),
                        uri=urljoin(base_url, uri) if uri else None,
                        channels=attrs.get("CHANNELS"),
                        default=attrs.get("DEFAULT", "").upper() == "YES",
                    )
                )
        elif line and not line.startswith("#"):
            if pending is not None:
                res = pending.get("RESOLUTION", "")
                width = height = None
                if "x" in res:
                    try:
                        width, height = (int(v) for v in res.split("x", 1))
                    except ValueError:
                        pass
                try:
                    bandwidth = int(pending.get("BANDWIDTH", "0"))
                except ValueError:
                    bandwidth = 0
                master.variants.append(
                    Variant(
                        uri=urljoin(base_url, line),
                        bandwidth=bandwidth,
                        width=width,
                        height=height,
                        codecs=pending.get("CODECS"),
                        audio_group=pending.get("AUDIO"),
                    )
                )
                pending = None
            else:
                master.variants.append(Variant(uri=urljoin(base_url, line)))
    return master


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    playlist = MediaPlaylist()
    lines = text.splitlines()
    pending_extinf: float | None = None
    # #EXT-X-PROGRAM-DATE-TIME sets the absolute start of the next segment;
    # subsequent ones are derived by adding EXTINF until the next tag
    pending_pdt: float | None = None
    for line in lines:
        line = line.strip()
        if line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                playlist.media_sequence = int(line.split(":", 1)[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                playlist.target_duration = float(line.split(":", 1)[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("#EXT-X-VERSION:"):
            try:
                playlist.version = int(line.split(":", 1)[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("#EXT-X-MAP"):
            match = _MAP_URI.search(line)
            if match:
                playlist.map_uri = urljoin(base_url, match.group(1))
        elif line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            value = line.split(":", 1)[1].strip().upper()
            playlist.playlist_type = value if value in ("VOD", "EVENT") else None
        elif line.startswith("#EXT-X-ENDLIST"):
            playlist.endlist = True
        elif line.startswith("#EXT-X-PART"):
            playlist.is_ll = True
        elif line.startswith("#EXT-X-SERVER-CONTROL:"):
            attrs = _parse_attrs(line)
            playlist.can_block_reload = attrs.get("CAN-BLOCK-RELOAD", "").upper() == "YES"
            if "PART-HOLD-BACK" in attrs:
                try:
                    playlist.part_hold_back = float(attrs["PART-HOLD-BACK"])
                except ValueError:
                    pass
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            try:
                from datetime import datetime

                pending_pdt = datetime.fromisoformat(line.split(":", 1)[1].strip()).timestamp()
            except ValueError:
                pending_pdt = None
        elif line.startswith("#EXTINF:"):
            try:
                pending_extinf = float(line.split(":", 1)[1].split(",")[0])
            except (IndexError, ValueError):
                pending_extinf = 0.0
        elif line and not line.startswith("#"):
            extinf = pending_extinf or 0.0
            pdt = pending_pdt
            if pending_pdt is not None:
                pending_pdt = pending_pdt + extinf
            playlist.segments.append((urljoin(base_url, line), extinf, pdt))
            pending_extinf = None
    if playlist.is_ll and playlist.version is not None and playlist.version <= 4:
        playlist.is_ll = False
    return playlist


def select_variant(master: MasterInfo, order: int | None = None, height: int | None = None,
                   bandwidth: int | None = None) -> Variant:
    if not master.variants:
        raise ValueError("The master has no video variants")
    if order is not None:
        ranked = sorted(master.variants, key=lambda v: v.bandwidth)
        return ranked[order]
    if height is not None:
        candidates = [v for v in master.variants if v.height is not None]
        if not candidates:
            raise ValueError("No variant declares RESOLUTION")
        return min(candidates, key=lambda v: abs((v.height or 0) - height))
    if bandwidth is not None:
        candidates = [v for v in master.variants if v.bandwidth]
        if not candidates:
            raise ValueError("No variant declares BANDWIDTH")
        return min(candidates, key=lambda v: abs(v.bandwidth - bandwidth))
    return max(master.variants, key=lambda v: v.bandwidth)


def select_audio(master: MasterInfo, variant: Variant, selector: str = "auto") -> AudioRendition | None:
    renditions = [r for r in master.renditions if r.uri]
    if selector == "none":
        return None
    if selector == "auto":
        if variant.audio_group:
            renditions = [r for r in renditions if r.group_id == variant.audio_group]
        if not renditions:
            return None
        defaults = [r for r in renditions if r.default]
        return (defaults or renditions)[0]
    if selector.startswith("order:"):
        index = int(selector.split(":", 1)[1])
        ranked = sorted(renditions, key=lambda r: r.name or "")
        return ranked[index]
    if selector.startswith("lang:"):
        lang = selector.split(":", 1)[1].lower()
        matches = [r for r in renditions if (r.language or "").lower().startswith(lang)]
        if not matches:
            raise ValueError(f"No audio track with language {lang!r}")
        return matches[0]
    if selector.startswith("name:"):
        name = selector.split(":", 1)[1].lower()
        matches = [r for r in renditions if name in (r.name or "").lower()]
        if not matches:
            raise ValueError(f"No audio track named {name!r}")
        return matches[0]
    raise ValueError(f"Invalid audio selector: {selector!r}")


def blocking_params(playlist: MediaPlaylist, next_seq: int) -> dict[str, int] | None:
    """Query params for LL-HLS blocking reload (avoids unnecessary polling)."""
    if playlist.is_ll and playlist.can_block_reload:
        return {"_HLS_msn": next_seq, "_HLS_part": 0}
    return None
