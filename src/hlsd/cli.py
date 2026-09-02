"""CLI of the daemon: hlsd serve|request|stream|inspect|list|status|stop|retry|download|shutdown.

If no daemon is running, commands start it in the background
(detached from the CLI, logging to the data dir); the daemon shuts
itself down after a period of idle time with no tasks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import click
import httpx

DEFAULT_BASE = "http://127.0.0.1:8000"
IDLE_EXIT_DEFAULT = 900.0  # 15 min with no tasks -> the daemon shuts itself down

log = logging.getLogger("hlsd.cli")


def _default_idle_exit() -> float:
    try:
        return float(os.environ.get("HLSD_IDLE_EXIT", IDLE_EXIT_DEFAULT))
    except ValueError:
        return IDLE_EXIT_DEFAULT


def ensure_daemon(base: str, idle_exit: float | None = None) -> None:
    """If no daemon is listening on `base`, launch one in the background and wait for it to respond."""
    if idle_exit is None:
        idle_exit = _default_idle_exit()
    try:
        httpx.get(f"{base}/health", timeout=0.5)
        return
    except (httpx.HTTPError, OSError) as exc:
        log.debug("daemon not responding at %s: %r", base, exc)
    from hlsd.config import DaemonConfig

    cfg = DaemonConfig()
    cfg.ensure_dirs()
    log_path = cfg.data_dir / "daemon.log"
    parts = urlparse(base)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 8000
    cmd = [
        sys.executable, "-m", "hlsd.cli", "serve",
        "--host", host, "--port", str(port),
        "--idle-exit", str(idle_exit),
    ]
    env = os.environ.copy()
    env.setdefault("HLSD_DATA_DIR", str(cfg.data_dir))
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True
    with open(log_path, "ab") as log_file:
        subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, env=env, **popen_kwargs)
    for _ in range(60):
        try:
            httpx.get(f"{base}/health", timeout=0.5)
            click.echo(f"Daemon started in the background (log: {log_path}).")
            return
        except (httpx.HTTPError, OSError):
            time.sleep(0.25)
    raise click.ClickException(f"Could not start the daemon in the background. Check {log_path}")


def _post(base: str, path: str, payload: dict) -> dict:
    response = httpx.post(f"{base}{path}", json=payload, timeout=30)
    if response.status_code >= 400:
        detail = response.json().get("detail", response.text)
        raise click.ClickException(f"HTTP {response.status_code}: {detail}")
    return response.json()


def _get(base: str, path: str) -> dict:
    response = httpx.get(f"{base}{path}", timeout=30)
    if response.status_code >= 400:
        detail = response.json().get("detail", response.text)
        raise click.ClickException(f"HTTP {response.status_code}: {detail}")
    return response.json()


@click.group()
@click.option("--base", default=DEFAULT_BASE, show_default=True, help="URL of the daemon")
@click.pass_context
def cli(ctx: click.Context, base: str):
    ctx.ensure_object(dict)
    ctx.obj["base"] = base


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--idle-exit", type=float, default=None, help="Seconds with no tasks before auto-shutdown (default: env HLSD_IDLE_EXIT or disabled)")
def serve(host: str, port: int, idle_exit: float | None):
    """Start the daemon (including the HTTP API)."""
    import asyncio

    import uvicorn

    from hlsd.api import create_app
    from hlsd.config import DaemonConfig

    cfg = DaemonConfig()
    cfg.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    app = create_app(cfg)
    daemon = app.state.daemon
    if idle_exit is not None:
        daemon.idle_exit_after = idle_exit
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    daemon.on_idle = lambda: setattr(server, "should_exit", True)
    asyncio.run(server.serve())


def _resolve_curl(curl_text: str | None) -> str | None:
    """-: interactive multiline | @file: read the command from a file."""
    if curl_text is None:
        return None
    if curl_text == "-":
        click.echo("Paste the command (cURL or fetch) and finish with an empty line / Ctrl-D:")
        return _read_multiline()
    if curl_text.startswith("@"):
        path = Path(curl_text[1:])
        if not path.is_file():
            raise click.ClickException(f"Command file does not exist: {path}")
        return path.read_text(encoding="utf-8")
    return curl_text


@cli.command("request")
@click.argument("source", required=False)
@click.option("--curl", "curl_text", default=None, help="Full cURL command (or @file, or '-' for stdin)")
@click.option("--playlist-file", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--base-url", default=None, help="Base for resolving segments with --playlist-file")
@click.option("-m", "--mode", type=click.Choice(["realtime", "window", "accumulated", "segments"]), default="window", show_default=True)
@click.option("-t", "--duration", type=float, default=None, help="Seconds (or N segments with -m segments); omit = indefinite until stop")
@click.option("--in", "start_in", default=None, help="Schedule: 'in 5m', 'in 1h30m'")
@click.option("--at", "start_at", default=None, help="Schedule: '17:00', '5:30pm', ISO")
@click.option("-o", "--select-order", type=int, default=None, help="Variant by bitrate order (0 worst, -1 best)")
@click.option("-r", "--select-height", type=int, default=None, help="Variant by closest height (e.g. 720)")
@click.option("-b", "--select-bandwidth", type=int, default=None)
@click.option("-a", "--audio", default="auto", show_default=True, help="auto | none | order:N | lang:xx | name:...")
@click.option("--volatile", "volatile_", is_flag=True, help="Segments in (capped) memory only")
@click.option("-c", "--concurrency", type=click.IntRange(1, 3), default=None, help="Fixed parallel segments (1-3); default: adaptive (2 live, 6 pre-recorded)")
@click.pass_context
def request_cmd(
    ctx: click.Context,
    source: str | None,
    curl_text: str | None,
    playlist_file: str | None,
    base_url: str | None,
    mode: str,
    duration: float | None,
    start_in: str | None,
    start_at: str | None,
    select_order: int | None,
    select_height: int | None,
    select_bandwidth: int | None,
    audio: str,
    volatile_: bool,
    concurrency: int | None,
):
    """Create a download request. SOURCE may be an m3u8 URL, a page
    containing an m3u8, or a cURL/fetch command pasted as text (or @file)."""
    url: str | None = None
    curl = _resolve_curl(curl_text)
    if curl is None and source and _is_command(source):
        curl = source
    elif curl is None and source:
        url = source
    start = start_in or start_at
    payload = {
        "url": url,
        "curl": curl,
        "playlist_file": playlist_file,
        "base_url": base_url,
        "mode": mode,
        "duration": duration,
        "start": start,
        "select_order": select_order,
        "select_height": select_height,
        "select_bandwidth": select_bandwidth,
        "audio": audio,
        "volatile": volatile_,
        "concurrency": concurrency,
    }
    ensure_daemon(ctx.obj["base"])
    result = _post(ctx.obj["base"], "/requests", payload)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command()
@click.argument("source", required=False)
@click.option("--curl", "curl_text", default=None, help="cURL/fetch command, @file, or '-' for stdin")
@click.option("-o", "--select-order", type=int, default=None)
@click.option("-r", "--select-height", type=int, default=None)
@click.option("-b", "--select-bandwidth", type=int, default=None)
@click.option("-a", "--audio", default="auto", show_default=True)
@click.option("--volatile/--no-volatile", "volatile_", default=None, help="Segments in (capped) memory [default] or on disk")
@click.pass_context
def stream_cmd(ctx: click.Context, source: str | None, curl_text: str | None, select_order, select_height, select_bandwidth, audio, volatile_, concurrency):
    """Open a live stream that shares the daemon's Source (a single fetch
    to the remote resource). Serves an HLS playlist (hls.js/VLC) and chunked MP4."""
    curl = _resolve_curl(curl_text)
    url: str | None = None
    if curl is None and source and _is_command(source):
        curl = source
    elif curl is None and source:
        url = source
    if curl is None and url is None:
        raise click.ClickException("Pass SOURCE or --curl (@file / '-')")
    ensure_daemon(ctx.obj["base"])
    result = _post(
        ctx.obj["base"],
        "/streams",
        {
            "url": url,
            "curl": curl,
            "select_order": select_order,
            "select_height": select_height,
            "select_bandwidth": select_bandwidth,
            "audio": audio,
            "volatile": volatile_,
            "concurrency": concurrency,
        },
    )
    base = ctx.obj["base"].rstrip("/")
    click.echo(f"Stream: {result['id']}")
    click.echo(f"Playlist (hls.js/VLC/ffplay): {base}{result['playlist']}")
    click.echo("The session expires on its own after 120s without accesses, or DELETE /streams/<ID>.")


def _is_command(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(("curl ", "fetch("))


@cli.command("inspect")
@click.argument("source", required=False)
@click.option("--curl", "curl_text", default=None, help="cURL/fetch command, @file, or '-' for stdin")
@click.pass_context
def inspect_cmd(ctx: click.Context, source: str | None, curl_text: str | None):
    """Show video/audio variants of a master (URL, page or cURL/fetch,
    supporting --curl @file)."""
    curl = _resolve_curl(curl_text)
    url: str | None = None
    if curl is None and source and _is_command(source):
        curl = source
    elif curl is None and source:
        url = source
    if curl is None and url is None:
        raise click.ClickException("Pass SOURCE or --curl (@file / '-')")
    curl = _resolve_curl(curl) or curl
    ensure_daemon(ctx.obj["base"])
    result = _post(ctx.obj["base"], "/inspect", {"url": url, "curl": curl})
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))


@cli.command("list")
@click.pass_context
def list_cmd(ctx: click.Context):
    """List the requests known to the daemon (active, scheduled and
    finished), with their state, mode and statistics."""
    ensure_daemon(ctx.obj["base"])
    rows = _get(ctx.obj["base"], "/requests")
    for row in rows:
        stats = row.get("stats") or {}
        click.echo(
            f"{row['id']}  {row['state']:<10}  {row.get('mode', '?'):<12}"
            f"  segs_v={stats.get('segments_video', '-')}  {row.get('url', '')[:60]}"
        )


@cli.command()
@click.argument("request_id")
@click.pass_context
def status(ctx: click.Context, request_id: str):
    ensure_daemon(ctx.obj["base"])
    click.echo(json.dumps(_get(ctx.obj["base"], f"/requests/{request_id}"), indent=2, ensure_ascii=False))


@cli.command()
@click.argument("request_id")
@click.pass_context
def stop(ctx: click.Context, request_id: str):
    ensure_daemon(ctx.obj["base"])
    click.echo(json.dumps(_post(ctx.obj["base"], f"/requests/{request_id}/stop", {}), indent=2, ensure_ascii=False))


@cli.command()
@click.pass_context
def shutdown(ctx: click.Context):
    """Shut down the daemon (finalizes active requests and frees resources)."""
    try:
        _post(ctx.obj["base"], "/shutdown", {})
    except click.ClickException:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise click.ClickException(f"Could not reach the daemon: {exc}")
    click.echo("Daemon shutting down.")


def _default_dest(row: dict, request_id: str) -> str:
    """Default file name derived from the request URL + the download
    start date/time (`created_at`): the last meaningful path segment
    (`.../creator/` -> `creator`, `.../origin.xx/llhls.m3u8` ->
    `origin.xx`). If nothing recognizable is found, falls back to
    `<id>-<date>.mp4`."""
    ts_src = row.get("started_at") or row.get("created_at") or time.time()
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts_src))
    url = row.get("url") or ""
    if url:
        parts = urlparse(url)
        segments = [s for s in parts.path.split("/") if s]
        if segments:
            last = segments[-1]
            # the playlist's file name is usually generic
            # (llhls.m3u8, index.m3u8): prefer the parent directory
            if re.search(r"\.(m3u8|mpd)$", last, re.IGNORECASE) and len(segments) > 1:
                last = segments[-2]
            name = re.sub(r"[^A-Za-z0-9._-]+", "-", last).strip("-.")
            name = re.sub(r"\.(m3u8|mpd)$", "", name, flags=re.IGNORECASE)
            if name:
                return f"{name[:60].rstrip('-')}-{ts}.mp4"
    return f"{request_id}-{ts}.mp4"


@cli.command()
@click.argument("request_id")
@click.pass_context
def retry(ctx: click.Context, request_id: str):
    """Retry assembling a request that failed during finalization.
    Works after restarting the daemon (segments persist on disk)."""
    ensure_daemon(ctx.obj["base"])
    click.echo(json.dumps(_post(ctx.obj["base"], f"/requests/{request_id}/retry", {}), indent=2, ensure_ascii=False))


@cli.command()
@click.argument("request_id")
@click.option("-f", "--out", type=click.Path(dir_okay=False), default=None, help="Destination file (.mp4); default: <URL slug>-<datetime>.mp4")
@click.pass_context
def download(ctx: click.Context, request_id: str, out: str | None):
    """Assemble and download the current result of the request."""
    ensure_daemon(ctx.obj["base"])
    row = _get(ctx.obj["base"], f"/requests/{request_id}")
    response = httpx.get(f"{ctx.obj['base']}/requests/{request_id}/download", timeout=600)
    if response.status_code >= 400:
        detail = response.json().get("detail", response.text)
        raise click.ClickException(f"HTTP {response.status_code}: {detail}")
    dest = out or _default_dest(row, request_id)
    Path(dest).write_bytes(response.content)
    click.echo(f"Saved: {dest} ({len(response.content)} bytes)")


def _read_multiline() -> str:
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "":
            break
        lines.append(line)
    return "\n".join(lines)


if __name__ == "__main__":
    cli()
