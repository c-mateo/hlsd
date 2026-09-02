# hlsd — HLS recording daemon

Single-process daemon that records HLS streams. One **Source** per remote
resource downloads every segment **exactly once** and shares it with all
active **Requests** (dedup of overlapping windows). On-demand MP4 assembly
with ffmpeg.

## Installation

```bash
pip install git+https://github.com/c-mateo/hlsd

# from a local clone:
cd hlsd
pip install -e .            # base deps
pip install -e ".[ffmpeg]"  # + portable ffmpeg (if you don't have ffmpeg on PATH)
pip install -e ".[dev]"     # + pytest to run the tests
```

Requirements: Python 3.10+, ffmpeg on PATH (or the bundled `imageio-ffmpeg`
binary is used automatically).

## hlsd vs yt-dlp

**yt-dlp** is the right tool for most downloads: it supports 1000+ sites via
its extractor library and handles signatures, logins and format negotiation
for you. **hlsd is not a replacement — it complements it** in a few specific
situations:

- **You already have the m3u8 or a cURL command** (from the browser network
  inspector): hlsd records it directly, no extractor needed.
- **Single fetch, multiple consumers**: one daemon fetch downloads each
  segment exactly once from the origin, while any number of players and
  overlapping recordings (request 60s now, 70s ten seconds later) share it —
  useful for rate-limited or token-gated origins.
- **Live catch-up**: a behind-the-live-edge window (default 60 segments) is
  served locally, so players/tabs that lag can catch up instead of freezing.
- **Scheduling**: `--in 5m`, `--at 17:00` — start a recording while you're
  away.

If yt-dlp supports the site, use it. When it doesn't (obscure CDNs, tokens
that expire in minutes, sites behind `Referer` checks), copy the cURL command
and let hlsd do the rest.

## Quick start

You don't need to start anything by hand: **any CLI command boots the daemon
in the background** (it stays alive after the terminal closes, log at
`<data-dir>/daemon.log`) and it **auto-exits after ~15 min of idle time**
(`HLSD_IDLE_EXIT` or `serve --idle-exit N`).

```bash
# Record 60 seconds of a stream and download the MP4
hlsd request https://site.com/video/master.m3u8 -t 60
hlsd list                                  # see the ID
hlsd status <ID>                           # state, segments, result_path
hlsd download <ID> -f video.mp4            # save the MP4

# Check which qualities/tracks a stream has before recording
hlsd inspect https://site.com/video/master.m3u8
```

When `status <ID>` says `done`, the MP4 is already assembled at
`.hlsd/outputs/<ID>.mp4` — `download` just copies it wherever you want.
Without `-f`, the file is named after the stream URL plus the recording start
time, e.g. `creator-20260902-144301.mp4`.

## CLI commands

| Command | What it does |
|---|---|
| `hlsd serve` | Foreground daemon (`--host --port --idle-exit`) |
| `hlsd request SOURCE [options]` | Create a download (URL, page, cURL/fetch) |
| `hlsd stream SOURCE` | Open a live stream for a player |
| `hlsd inspect SOURCE` | List video/audio variants of a master |
| `hlsd list` | List all requests (active, scheduled, finished) |
| `hlsd status <ID>` | Detailed request status |
| `hlsd stop <ID>` | Stop and assemble what was downloaded |
| `hlsd retry <ID>` | Retry assembly of a failed request (works across daemon restarts) |
| `hlsd shutdown` | Gracefully stop the daemon (finalizes active requests) |
| `hlsd download <ID> [-f out.mp4]` | Assemble and save the result |

`SOURCE` can be: a direct m3u8 URL (master or media), an **HTML page**
containing an m3u8 (the daemon extracts it), a pasted **cURL** command, or
`--curl @file` / `--curl -`.

### `request` options

```bash
hlsd request SOURCE [options]

  -t, --duration N       Seconds of recording — or number of segments with
                         `-m segments`. Omitted: until the stream ends (VOD)
                         or until `hlsd stop <ID>` (live)
  -m, --mode MODE        realtime | window (default) | accumulated | segments
  --in 5m / --at 17:00   Schedule the download (relative or absolute)
  -o, --select-order N   Variant by bitrate rank (0 = worst, -1 = best)
  -r, --select-height N  Variant closest to this height (e.g. 720)
  -b, --select-bandwidth N
  -a, --audio SEL        auto (default) | none | order:N | lang:es | name:xxx
  -c, --concurrency 1-3  Fixed parallel segment downloads for this resource (default: adaptive — 2 live / 6 VOD)
  --volatile             Segments kept in memory only (lost on daemon restart)
```

## Import a cURL or fetch command from the network inspector

For streams with session tokens or Referer/Origin protection, copy the
command from the network inspector (F12 → Network → right-click the
`master.m3u8` → *Copy as cURL* or *Copy as fetch*).

**Recommended: save it to a file and use `@`** — immune to shell escaping and
line breaks in CMD/PowerShell/bash:

```bash
hlsd request --curl @command.txt -t 60      # record 60s
hlsd inspect --curl @command.txt            # list variants
hlsd stream  --curl @command.txt            # watch live
```

These also work:

```bash
hlsd request --curl -                       # interactive: paste, end with empty line
hlsd request 'curl "https://host/m.m3u8?t=T" -H "Referer: https://p.com/"'
hlsd request 'fetch("https://host/m.m3u8?t=T", { "headers": { "Referer": "https://p.com/" }, "method": "GET" });'
```

URL, method, headers (including `sec-ch-ua` with nested quotes), cookies
(`-H "Cookie: ..."` or `-b`), payload (`-d`/`body`) and auth (`-u`) are all
extracted. **All** daemon fetching (playlists, variants, segments, audio)
reuses those credentials. The query token can be ephemeral: Source dedup uses
the URL without query. See `examples/`.

> ⚠️ Tokens usually expire (e.g. `e=28800` = 2h). Copy the command fresh while
> the video is playing and use it right away. If you get 403, refresh the page
> and copy again — and make sure the cURL includes every header: some servers
> validate the full set (`Origin`, `Sec-Fetch-*`).

## Watch live (streaming)

One Source feeds both recordings and live streams: even with a player open
and a recording of the same resource at the same time, **the remote server is
fetched exactly once**.

```bash
hlsd stream --curl @command.txt
# Stream: 71e7320e6fe3
# Playlist (hls.js/VLC/ffplay): http://127.0.0.1:8000/streams/<ID>/playlist.m3u8
```

Point any HLS player (hls.js in one or more browser tabs, VLC, ffplay,
mpv) at `/streams/<ID>/playlist.m3u8`:

- If the source has **separate audio** (EXT-X-MEDIA), that URL is a **master
  playlist** that declares the audio group and references the daemon's own
  `video.m3u8` / `audio.m3u8` — players reproduce it with sound.
- If there is a single track, it is a media playlist directly.
- Segments are served from `/streams/<ID>/segment/{v|a}/{SEQ}`.
- **Segments stay in memory by default**: casual playback never writes to
  disk (`HLSD_STREAM_VOLATILE=0` restores disk-backed streaming, e.g. to
  record from a stream-shared source across restarts).

The playlist exposes an **extended window**: every segment already
downloaded by the daemon (last `HLSD_STREAM_WINDOW` segments for live, all
of them for VOD), not just the remote's current window. A tab that falls
behind finds its segments instead of freezing — several tabs can watch at
once without starving each other.

One Source feeds both streams and recordings: even with several players
open and a recording of the same resource at the same time, **the remote
server is fetched exactly once**.

Session management:

- Sessions expire after `HLSD_STREAM_TTL` (default 120s) without access.
- Manual close: `curl -X DELETE http://127.0.0.1:8000/streams/<ID>`.
- `GET /streams` lists active sessions.

## HTTP API

The same daemon exposes the API (see `http://127.0.0.1:8000/docs` for the
full schema). 100% async, fire-and-forget: `POST /requests` responds with the
ID and the daemon carries on, without holding connections open.

```
POST   /requests                     # create download (url | curl | playlist_file)
GET    /requests                     # list
GET    /requests/{id}                # status (stats, result_path)
POST   /requests/{id}/stop           # stop and assemble
POST   /requests/{id}/retry          # retry assembly of a failed request
DELETE /requests/{id}                # cancel a scheduled request
GET    /requests/{id}/download       # assembled MP4 (preview while still active)
POST   /inspect                      # master variants
POST   /streams                      # open live stream
GET    /streams/{id}/playlist.m3u8   # master playlist (media playlist if single track)
GET    /streams/{id}/video.m3u8      # video track media playlist
GET    /streams/{id}/audio.m3u8      # audio track media playlist
DELETE /streams/{id}                 # close streaming session
POST   /shutdown                     # stop the daemon gracefully
GET    /sources                      # active pollers
GET    /health                       # rss, requests, sources, volatile memory
```

Example:

```bash
curl -s -X POST http://127.0.0.1:8000/requests -H 'Content-Type: application/json' \
  -d '{"curl": "...", "mode": "window", "duration": 60, "start": "in 5m", "concurrency": 2}'
```

## Duration modes

| Mode | Counts from | Result |
|---|---|---|
| `realtime` | Request activation | Strict clock window |
| `window` (default) | The **first segment received** | Exact requested duration: surplus is downloaded and trimmed at assembly |
| `accumulated` | Sum of assigned EXTINF | Always lasts exactly what was asked even if segments were lost (may contain gaps) |
| `segments` | First video segment | Stops after exactly `-t` **video segments**; any surplus arriving before finalization is discarded (separate audio is trimmed to match) |

Without `-t/--duration` the request is **indefinite**:

- **VOD** (recorded stream): the daemon detects the end via `#EXT-X-ENDLIST`
  or `#EXT-X-PLAYLIST-TYPE:VOD` and assembles by itself — the full MP4 shows
  up in `status`/`download`, no need to stop anything.
- **Live**: keeps recording until `hlsd stop <ID>`.

## Failure handling and retries

Segments persist on disk (SQLite index + atomic writes), so a failed assembly
is never fatal:

- If `ffmpeg` fails to assemble (or the code had a bug that is now fixed),
  the request stays `failed` and `hlsd retry <ID>` (or
  `POST /requests/{id}/retry`) re-assembles it **with the current code**,
  even after a daemon restart.
- On daemon restart, active requests are finalized with whatever was
  downloaded so far.
- Volatile requests are the exception: their segments live in RAM and are
  lost on restart.

## A/V sync and assembly

- **fMP4 sources** (`EXT-X-MAP`): the init segment and fragments are
  concatenated **byte-wise** into a single fragmented MP4 — the concat
  demuxer cannot open bare `moof` fragments. If the request never received
  the init (late activation, daemon restart), it is recovered from the
  backend.
- **Separate audio** (`EXT-X-MEDIA` + `AUDIO="<group>"`): each track is
  concatenated separately and muxed; the mux falls back from stream copy to
  AAC re-encode to full re-encode if needed.
- **Sync**: HLS media playlists carry `#EXT-X-PROGRAM-DATE-TIME`; the merger
  computes the absolute start of each track's first segment and applies
  `-itsoffset` to the *earlier* track (playlists drift apart slightly because
  per-track segment durations differ — sequence numbers alone are not
  enough). The shift is always positive: delaying audio by a negative amount
  produces negative timestamps that the muxer clamps when stream-copying,
  leaving the audio late. Set `HLSD_AV_OFFSET` (seconds, e.g. `-0.5`) to
  nudge the result for a source where detection is off.
- **Trim**: surplus beyond the requested duration is trimmed (`-t`) for
  `window`/`realtime`/`accumulated`; `segments` mode trims by count.

## When each thing ends

- **Segment/track**: a track ends on `#EXT-X-ENDLIST` or
  `PLAYLIST-TYPE:VOD`; missing segments are retried while they remain in the
  playlist window.
- **Request**: the end condition of its mode, a user `stop`, or VOD end
  (auto-assembly).
- **Source**: while there is unsatisfied demand (+ a brief drain), then it
  shuts down by itself. Volatile sources release their RAM.
- **Daemon**: auto-exit after `HLSD_IDLE_EXIT` (default 900s) with no
  requests, streams or scheduled jobs.

## Recording the same stream twice (core use case)

If you request 60s and 10s later request 70s of the same stream, nothing is
downloaded twice: the Source downloads ~70s of segments once, and each
request assembles its own window with its own anchor. Example:

```bash
hlsd request URL -t 60 &        # t=0
sleep 10
hlsd request URL -t 70          # t=10: shares the already-downloaded segments
```

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `HLSD_DATA_DIR` | `<cwd>/.hlsd` | Where the DB, segments and outputs live |
| `HLSD_IDLE_EXIT` | `900` | Idle auto-exit (s; 0 = disabled) |
| `HLSD_LIVE_CONCURRENCY` | `2` | Parallel segment downloads **per track** (video/audio) while the stream is live — gentle on the origin |
| `HLSD_VOD_CONCURRENCY` | `6` | Parallel segment downloads **per track** for pre-recorded streams (VOD) — downloads the whole file faster without tripping rate limiters |
| `HLSD_SEGMENT_CONCURRENCY` | *(unset)* | Fixed per-Source parallelism that overrides both adaptive values (e.g. `-c/--concurrency` does the same per request) |
| `HLSD_GLOBAL_CONCURRENCY` | `6` | Total daemon-wide HTTP cap (all sources) |
| `HLSD_STREAM_WINDOW` | `60` | Segments exposed in stream playlists (catch-up room for slow tabs) |
| `HLSD_STREAM_VOLATILE` | `1` | Streams keep segments in memory only; `0` = disk-backed streams |
| `HLSD_SOURCE_DISK_BUDGET` | `20 MiB` | Cap on persisted-but-unclaimed segments per source (oldest pruned first; 0 = no cap) |
| `HLSD_AV_OFFSET` | `0` | Manual A/V correction in seconds, added to the detected offset |
| `HLSD_VOLATILE_MAX_BYTES` | `1 GiB` | Memory cap for volatile mode |
| `HLSD_STREAM_TTL` | `120` | Streaming session expiry (s) |
| `HLSD_HOST` / `HLSD_PORT` | `127.0.0.1` / `8000` | Daemon bind |
| `HLSD_POLL_MIN/MAX` | `1.0` / `6.0` | Adaptive playlist poll range |
| `HLSD_MAX_RETRIES` | `3` | Retries per request (exponential backoff) |

## Download concurrency

Chosen **after resolving** the source, by content type (unless overridden):

- **Live** (`HLSD_LIVE_CONCURRENCY`, default **2**): while the broadcast is
  ongoing, each track (video, audio) downloads up to 2 segments at a time —
  enough to stay on the live edge without hammering the origin.
- **VOD / pre-recorded** (`HLSD_VOD_CONCURRENCY`, default **6**): the whole
  file is known up front, so each track downloads up to 6 segments at a
  time to finish faster, while staying modest enough for rate limiters.
- **Override**: `HLSD_SEGMENT_CONCURRENCY` fixes one value for everything;
  `-c/--concurrency` (1–3) fixes it per request/stream.
- **Daemon global**: `HLSD_GLOBAL_CONCURRENCY` (default 6) caps total
  simultaneous HTTP requests across all sources.

**Guaranteed order**: segments in a batch are downloaded in parallel but
stored in **media sequence order**; a failed segment opens no gap
(`last_seq` advances contiguously and the missing one is retried on the next
poll while it remains in the playlist window).

## Modules

| File | Responsibility |
|---|---|
| `config.py` | Configuration (`HLSD_*` env vars) |
| `models.py` | `RequestTemplate`, `DownloadSpec`, `SegmentMeta`, enums |
| `curl_parser.py` | cURL command (CMD/bash) and fetch() → `RequestTemplate` |
| `hls.py` | Master/media parsing, variants, EXT-X-MEDIA, LL-HLS, PROGRAM-DATE-TIME |
| `net.py` | httpx with retries/backoff + per-Source and global semaphores |
| `memory.py` | Bounded volatile store, RSS, `malloc_trim` |
| `store.py` | SQLite (WAL): requests, jobs, segment index |
| `scheduler.py` | `in 5m` / `17:00` / ISO scheduling |
| `request.py` | State machine and end conditions |
| `source.py` | `StreamSource`: multi-rendition poller with dedup |
| `merger.py` | ffmpeg assembly (byte-concat fMP4, A/V offset, fallbacks, trim) |
| `streaming.py` | Rewritten live playlists (master / video / audio) |
| `daemon.py` | Orchestrator, graceful shutdown, recovery, retries, disk pruning |
| `api.py` / `cli.py` | Interfaces |

## Notes

- **Streams** (playback without a recording request) default to the
  **volatile** backend: segments in RAM with a hard cap
  (`HLSD_VOLATILE_MAX_BYTES`) and backpressure when full; nothing touches
  disk.
- **Recording requests** default to the **persistent** backend: atomic disk
  writes + SQLite index; active requests are finalized with what was
  downloaded if the daemon restarts. Persisted segments that no active
  request claims anymore are pruned per source down to
  `HLSD_SOURCE_DISK_BUDGET` (20 MiB by default), so nothing accumulates
  forever. Consequence: `retry` after a daemon restart is best-effort — it
  only sees segments that survived pruning (request-owned segments are
  always protected while the request is active).
- **LL-HLS**: full segments are recorded (granularity = segment), with
  blocking reload (`_HLS_msn`) when the server announces it.
- **EVENT** (`PLAYLIST-TYPE:EVENT`): the list grows without deleting old
  entries; the daemon keeps polling and deduplicating by sequence.
- Daemon data lives in `.hlsd/` (safe to delete: it is only cache).

## Tests

```bash
python3 -m pytest tests/        # 88 tests
```
