from hlsd.curl_parser import parse_curl

CMD_EXAMPLE = r'''curl --url ^"https://8vvlio0nv6xr.tnmr.org/hls2/03/04172/nf208yap750k_h/master.m3u8?t=DwOfLQEUdyWBndUzCGxV_tjLr2tcEIDjr59S80096Js^&s=1788320574^&e=28800^&f=20862487^&i=0.3^&sp=0^" ^
  -H ^"Accept: */*^" ^
  -H ^"Origin: https://luluvdo.com^" ^
  -H ^"Referer: https://luluvdo.com/^" ^
  -H ^"User-Agent: Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36^" ^
  -H ^"sec-ch-ua: ^\^"Not=A?Brand^\^";v=^\^"99^\^", ^\^"Google Chrome^\^";v=^\^"151^\^", ^\^"Chromium^\^";v=^\^"151^\^"^" ^
  -H ^"Cookie: session=abc123; theme=dark^"'''


def test_parse_cmd_curl_with_escapes():
    template = parse_curl(CMD_EXAMPLE)
    assert template.url.startswith("https://8vvlio0nv6xr.tnmr.org/hls2/03/04172/nf208yap750k_h/master.m3u8")
    assert "t=DwOfLQEUdyWBndUzCGxV_tjLr2tcEIDjr59S80096Js" in template.url
    assert "&s=1788320574" in template.url
    assert template.headers["Origin"] == "https://luluvdo.com"
    assert template.headers["Referer"] == "https://luluvdo.com/"
    assert template.headers["sec-ch-ua"] == '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"'
    assert template.method == "GET"
    assert template.cookies == {"session": "abc123", "theme": "dark"}


def test_parse_bash_curl_with_data_defaults_to_post():
    command = r"""curl 'https://example.com/api?x=1' -X POST -H 'Content-Type: application/json' --data-raw '{"k":1}' -u user:pass"""
    template = parse_curl(command)
    assert template.url == "https://example.com/api?x=1"
    assert template.method == "POST"
    assert template.headers["Content-Type"] == "application/json"
    assert template.body == b'{"k":1}'
    assert template.auth == ("user", "pass")


def test_parse_bare_url_and_unknown_options():
    template = parse_curl("curl -sL --some-unknown-opt itsarg https://example.com/master.m3u8")
    assert template.url == "https://example.com/master.m3u8"


FETCH_EXAMPLE = '''fetch("https://8vvlio0nv6xr.tnmr.org/hls2/03/04172/nf208yap750k_h/master.m3u8?t=DwOfLQEUdyWBndUzCGxV_tjLr2tcEIDjr59S80096Js&s=1788320574&e=28800&f=20862487&i=0.3&sp=0", {
  "headers": {
    "accept": "*/*",
    "accept-language": "es-AR,es;q=0.9",
    "sec-ch-ua": "\\"Not=A?Brand\\";v=\\"99\\"",
    "sec-ch-ua-mobile": "?1",
    "sec-fetch-dest": "empty",
    "Referer": "https://luluvdo.com/"
  },
  "body": null,
  "method": "GET"
});'''


def test_parse_fetch_format():
    template = parse_curl(FETCH_EXAMPLE)
    assert template.url.startswith("https://8vvlio0nv6xr.tnmr.org/hls2/03/04172/nf208yap750k_h/master.m3u8")
    assert "t=DwOfLQEUdyWBndUzCGxV_tjLr2tcEIDjr59S80096Js" in template.url
    assert template.method == "GET"
    assert template.body is None
    assert template.headers["accept"] == "*/*"
    assert template.headers["Referer"] == "https://luluvdo.com/"
    assert template.headers["sec-ch-ua"] == '"Not=A?Brand";v="99"'
    assert template.cookies == {}


def test_parse_fetch_tolerant_js():
    command = """await fetch('https://cdn.example.com/live.m3u8?token=abc', {
      headers: { // comment
        'user-agent': 'test',
        'x-custom': 'a"b',
      },
      method: 'POST',
      body: 'data=1',
    });"""
    template = parse_curl(command)
    assert template.url == "https://cdn.example.com/live.m3u8?token=abc"
    assert template.method == "POST"
    assert template.body == b"data=1"
    assert template.headers["user-agent"] == "test"
    assert template.headers["x-custom"] == 'a"b'


def test_parse_source_command_dispatch():
    from hlsd.curl_parser import parse_source_command

    assert parse_source_command(FETCH_EXAMPLE).url.startswith("https://8vvlio0nv6xr")
    assert parse_source_command("curl https://x/m.m3u8").url == "https://x/m.m3u8"


def test_url_with_cookie_header():
    template = parse_curl("curl https://example.com/m.m3u8 -H 'Cookie: a=1; b=2'")
    assert template.cookies == {"a": "1", "b": "2"}
