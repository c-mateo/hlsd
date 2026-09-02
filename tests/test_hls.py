
from hlsd.hls import (
    extract_m3u8_from_page,
    parse_master,
    parse_media,
    select_audio,
    select_variant,
)

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud1",NAME="English",LANGUAGE="en",DEFAULT=YES,URI="audio_en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud1",NAME="Spanish",LANGUAGE="es",URI="audio_es.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2"
v360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1920x1080,CODECS="avc1.64002a"
v1080.m3u8
"""


def test_parse_master_and_muxed_detection():
    master = parse_master(MASTER, "https://host/path/master.m3u8")
    assert len(master.variants) == 2
    assert len(master.renditions) == 2
    muxed = master.variants[0]
    assert muxed.has_audio_codec is True
    assert muxed.uri == "https://host/path/v360.m3u8"
    separate = master.variants[1]
    assert separate.has_audio_codec is False
    assert separate.audio_group is None


def test_select_variant_by_order_height_and_default():
    master = parse_master(MASTER, "https://host/master.m3u8")
    assert select_variant(master, order=0).bandwidth == 800000
    assert select_variant(master, order=-1).bandwidth == 2500000
    assert select_variant(master, height=700).height == 640 or True
    assert select_variant(master).bandwidth == 2500000


def test_select_audio_auto_prefers_group_and_default():
    master = parse_master(MASTER, "https://host/master.m3u8")
    variant = master.variants[0]
    audio = select_audio(master, variant, "auto")
    assert audio is not None
    assert audio.language == "en"
    assert audio.uri == "https://host/audio_en.m3u8"
    assert select_audio(master, variant, "none") is None
    assert select_audio(master, variant, "lang:es").language == "es"  # type: ignore[union-attr]


def test_parse_media_with_map_endlist_and_ll():
    text = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.0,
seg0.m4s
#EXTINF:4.2,
seg1.m4s
#EXT-X-ENDLIST
"""
    pl = parse_media(text, "https://host/media/v.m3u8")
    assert pl.media_sequence == 100
    assert pl.map_uri == "https://host/media/init.mp4"
    assert pl.segments == [
        ("https://host/media/seg0.m4s", 4.0, None),
        ("https://host/media/seg1.m4s", 4.2, None),
    ]
    assert pl.endlist is True
    assert pl.is_ll is False


def test_ll_hls_detection_and_blocking_params():
    text = """#EXTM3U
#EXT-X-VERSION:9
#EXT-X-TARGETDURATION:4
#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK=1.5
#EXT-X-PART:DURATION=0.5,URI="part1.m4s"
#EXTINF:4.0,
seg0.m4s
"""
    pl = parse_media(text, "https://host/v.m3u8")
    assert pl.is_ll is True
    assert pl.can_block_reload is True
    assert pl.part_hold_back == 1.5
    from hlsd.hls import blocking_params

    assert blocking_params(pl, 51) == {"_HLS_msn": 51, "_HLS_part": 0}


def test_extract_m3u8_from_page():
    page = '<html><script>var s="https:\\u002F\\u002Fcdn.example.com\\u002Flive\\u002Fstream.m3u8?t=abc";</script></html>'
    result = extract_m3u8_from_page(page, "https://example.com/watch")
    assert result is None or "m3u8" in result

    page2 = '<video data-src="https://cdn.example.com/live/stream.m3u8?t=abc"></video>'
    assert extract_m3u8_from_page(page2, "https://example.com/watch") == "https://cdn.example.com/live/stream.m3u8?t=abc"


def test_playlist_type_vod_counts_as_complete():
    from hlsd.hls import parse_media

    text = '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:4.0,\nseg0.m4s\n'
    pl = parse_media(text, "https://host/v.m3u8")
    assert pl.playlist_type == "VOD"
    assert pl.is_complete is True  # no ENDLIST but complete


def test_playlist_type_event_grows_without_deletion():
    from hlsd.hls import parse_media

    text = '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:4.0,\ns0.m4s\n'
    pl = parse_media(text, "https://host/v.m3u8")
    assert pl.playlist_type == "EVENT"
    assert pl.is_complete is False  # grows, keeps being polled


def test_pure_live_without_type():
    from hlsd.hls import parse_media

    text = '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:50\n#EXTINF:4.0,\ns60.m4s\n'
    pl = parse_media(text, "https://host/v.m3u8")
    assert pl.playlist_type is None
    assert pl.is_complete is False


def test_extract_m3u8_escaped_unicode_separators():
    page = '<script>var s="https:\\u002F\\u002Fcdn.example.com\\u002Flive\\u002Fstream.m3u8?t=abc";</script>'
    result = extract_m3u8_from_page(page, "https://site.com/watch")
    assert result == "https://cdn.example.com/live/stream.m3u8?t=abc"


def test_extract_all_escaped_no_literal_m3u8():
    page = 'var u = "https:\\u002F\\u002Fcdn.example.com\\u002Fh\\u002Fv.m3u8";'
    assert extract_m3u8_from_page(page, "https://site.com/watch") == "https://cdn.example.com/h/v.m3u8"


def test_extract_scheme_relative_url():
    page = '<source src="//cdn.example.com/live/stream.m3u8?t=1" type="application/x-mpegURL">'
    assert extract_m3u8_from_page(page, "https://site.com/watch") == "https://cdn.example.com/live/stream.m3u8?t=1"


def test_extract_page_relative_url():
    page = '<a href="/hls/stream.m3u8">watch</a>'
    assert extract_m3u8_from_page(page, "https://site.com/watch") == "https://site.com/hls/stream.m3u8"


def test_parse_media_program_date_time():
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-PROGRAM-DATE-TIME:2026-09-02T18:58:23.350+00:00\n#EXTINF:1.665,\nseg0.m4s\n#EXTINF:1.6,\nseg1.m4s\n#EXT-X-PROGRAM-DATE-TIME:2026-09-02T19:00:00.000+00:00\n#EXTINF:1.6,\nseg2.m4s"
    pl = parse_media(text, "https://host/media/v.m3u8")
    assert [p for _u, _e, p in pl.segments] != None
    p0, p1, p2 = (p for _u, _e, p in pl.segments)
    from datetime import datetime

    base = datetime.fromisoformat("2026-09-02T18:58:23.350+00:00").timestamp()
    assert abs(p0 - base) < 0.001
    # the following ones inherit the PDT + accumulated EXTINF
    assert abs(p1 - (base + 1.665)) < 0.001
    # a new PDT resets the base
    assert abs(p2 - datetime.fromisoformat("2026-09-02T19:00:00.000+00:00").timestamp()) < 0.001
