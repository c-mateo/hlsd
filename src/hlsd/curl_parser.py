"""Parser of network-inspector commands -> RequestTemplate.

Supported formats: cURL ("Copy as cURL", tolerating Windows CMD
escaping of ^, ^", \\^\\^" and bash escaping) and Node/Chrome fetch(...)
("Copy as fetch").
"""

from __future__ import annotations

import codecs
import json
import re
import shlex
from urllib.parse import urlsplit

from .models import RequestTemplate


class CurlParseError(ValueError):
    pass


class UnsupportedFormatError(CurlParseError):
    pass


_OPTIONS_WITH_ARG = {
    "-H", "--header",
    "-b", "--cookie",
    "-A", "--user-agent",
    "-e", "--referer",
    "-X", "--request",
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii",
    "-u", "--user",
    "--url",
    "-o", "--output",
    "--connect-timeout", "--max-time", "--retry", "--resolve",
    "--cert", "--key", "--cacert", "--capath",
    "-x", "--proxy",
}
_FLAG_OPTIONS = {
    "-L", "--location", "-k", "--insecure", "-s", "--silent", "-v", "--verbose",
    "-i", "--include", "-f", "--fail", "-#", "--progress-bar", "-S", "--show-error",
    "--compressed", "-G", "--get", "-I", "--head", "-N", "--no-buffer",
}


def _preprocess(command: str) -> str:
    command = command.replace("\r\n", "\n")
    if "^" in command:
        # CMD mode: ^ at end of line is a continuation; elsewhere it escapes
        # the next char (e.g. ^" -> "). Sequences like \" remain, which
        # shlex(posix=True) interprets as a literal quote inside quotes.
        command = command.replace("^\n", " ").replace("^\\\n", " ")
        command = command.replace("^", "")
    return command


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(_preprocess(command), posix=True)
    except ValueError:
        return _preprocess(command).split()


def _parse_cookie_header(value: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in value.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, val = part.partition("=")
        cookies[name.strip()] = val.strip()
    return cookies


def _parse_cookie_arg(value: str) -> dict[str, str]:
    if value.startswith("@"):
        raise CurlParseError("--cookie @file not supported; paste the cookie inline")
    return _parse_cookie_header(value)


def _looks_like_fetch(command: str) -> bool:
    return _FETCH_RE.search(command) is not None


def parse_curl(command: str) -> RequestTemplate:
    if _looks_like_fetch(command):
        return parse_fetch(command)
    tokens = _tokenize(command)
    if not tokens:
        raise CurlParseError("Empty command")

    url: str | None = None
    method: str | None = None
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body: bytes | None = None
    auth: tuple[str, str] | None = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--url",):
            i += 1
            if i < len(tokens):
                url = tokens[i]
        elif tok in ("-X", "--request"):
            i += 1
            if i < len(tokens):
                method = tokens[i].upper()
        elif tok in ("-H", "--header"):
            i += 1
            if i < len(tokens):
                header = tokens[i]
                name, sep, value = header.partition(":")
                if sep:
                    if name.strip().lower() == "cookie":
                        cookies.update(_parse_cookie_header(value))
                    else:
                        headers[name.strip()] = value.strip()
        elif tok in ("-b", "--cookie"):
            i += 1
            if i < len(tokens):
                cookies.update(_parse_cookie_arg(tokens[i]))
        elif tok in ("-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--data-ascii"):
            i += 1
            if i < len(tokens):
                raw = tokens[i]
                if body is None:
                    body = raw.encode()
                else:
                    body += b"&" + raw.encode()
                if tok == "--data-urlencode":
                    body = body.replace(b"%20", b" ")
        elif tok in ("-u", "--user"):
            i += 1
            if i < len(tokens):
                user, _, password = tokens[i].partition(":")
                auth = (user, password)
        elif tok in ("-A", "--user-agent"):
            i += 1
            if i < len(tokens):
                headers["User-Agent"] = tokens[i]
        elif tok in ("-e", "--referer"):
            i += 1
            if i < len(tokens):
                headers["Referer"] = tokens[i]
        elif tok in _FLAG_OPTIONS or tok in ("-G", "--get"):
            pass
        elif tok.startswith("-"):
            # unknown option: if the next token is not an option, it is
            # probably its argument; drop both to avoid polluting the URL.
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 1
        else:
            candidate = tok
            if url is None and _is_url(candidate):
                url = candidate
        i += 1

    if not url:
        raise CurlParseError("No URL found in the command")
    if not url.lower().startswith(("http://", "https://")):
        raise CurlParseError(f"Unsupported URL: {url!r}")

    if body is not None and method is None:
        method = "POST"

    return RequestTemplate(
        url=url,
        method=method or "GET",
        headers=headers,
        cookies=cookies,
        body=body,
        auth=auth,
    )


def _is_url(token: str) -> bool:
    try:
        parts = urlsplit(token)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# ---------------------------------------------------------------------------
# fetch("url", { "headers": {...}, "body": null, "method": "GET" })
# ---------------------------------------------------------------------------

_FETCH_RE = re.compile(r"(?:await\s+)?fetch\s*\(")
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _read_js_string(text: str, start: int) -> tuple[str, int]:
    """Reads a JS string starting at the quote in `start`. Returns (value, end index)."""
    quote = text[start]
    i = start + 1
    out: list[str] = []
    while i < len(text):
        c = text[i]
        if c == "\\":
            escape = text[i : i + 2]
            try:
                decoded = codecs.decode(escape, "unicode_escape")
                out.append(decoded if len(decoded) == 1 else escape)
            except ValueError:
                out.append(escape)
            i += 2
            continue
        if c == quote:
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise CurlParseError("Unterminated string in the fetch command")


def _balanced_json(text: str, start: int) -> str:
    """Extracts a balanced {...} starting at the brace in `start`, respecting strings."""
    depth = 0
    in_str: str | None = None
    i = start
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in "\"'":
            in_str = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    raise CurlParseError("Unterminated options object in the fetch command")


def _js_object_to_json(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            quote = c
            out.append('"')
            i += 1
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    out.append(ch)
                    out.append(text[i + 1])
                    i += 2
                    continue
                if ch == quote:
                    out.append('"')
                    i += 1
                    break
                if ch == '"':
                    out.append('\\"')
                    i += 1
                    continue
                out.append(ch)
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            i = text.find("\n", i)
            if i == -1:
                break
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(c)
        i += 1
    converted = "".join(out)
    converted = re.sub(r",(\s*[}\]])", r"\1", converted)  # trailing commas
    converted = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)", r'\1"\2"\3', converted)  # unquoted keys
    return converted


def parse_fetch(command: str) -> RequestTemplate:
    match = _FETCH_RE.search(command)
    if not match:
        raise CurlParseError("No fetch(...) found in the command")
    pos = match.end()
    while pos < len(command) and command[pos].isspace():
        pos += 1
    if pos >= len(command) or command[pos] not in "\"'":
        raise CurlParseError("The first argument of fetch must be a string")
    url, pos = _read_js_string(command, pos)

    options: dict = {}
    rest = command[pos:]
    brace = rest.find("{")
    if brace != -1:
        obj_text = _balanced_json(rest, brace)
        try:
            options = json.loads(obj_text)
        except json.JSONDecodeError:
            options = json.loads(_js_object_to_json(obj_text))

    if not _is_url(url):
        raise CurlParseError(f"Unsupported URL: {url!r}")

    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    for name, value in (options.get("headers") or {}).items():
        if name.lower() == "cookie":
            cookies.update(_parse_cookie_header(value))
        else:
            headers[str(name)] = str(value)

    method = (options.get("method") or "GET").upper()
    if method not in _METHODS:
        method = "GET"
    body = options.get("body")
    if body is not None and not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False)

    return RequestTemplate(
        url=url,
        method=method,
        headers=headers,
        cookies=cookies,
        body=body.encode() if body else None,
    )


def parse_source_command(command: str) -> RequestTemplate:
    """Accepts a cURL command or a Node/Chrome fetch(...)."""
    if _looks_like_fetch(command):
        return parse_fetch(command)
    return parse_curl(command)
