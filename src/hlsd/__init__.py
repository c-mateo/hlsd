"""hlsd: HLS recording daemon.

A single daemon process manages Sources (pollers per HLS resource that
download each segment exactly once) and Requests (windows of segments with
a completion condition). On-demand ffmpeg assembly.

Modules:
- config: daemon configuration
- models: core dataclasses and enums
- curl_parser: cURL command -> RequestTemplate
- hls: HLS parsing (master/media, variants, EXT-X-MEDIA, LL-HLS)
- net: HTTP client with retries/backoff/semaphore
- memory: bounded volatile segment store + memory utilities
- store: SQLite persistence (requests, jobs, disk segment index)
- scheduler: relative/absolute scheduling
- request: DownloadRequest state machine
- source: StreamSource (multi-rendition poller with dedup)
- merger: on-demand ffmpeg assembly
- daemon: StreamDaemon (wiring, lifecycle, graceful shutdown)
- api / cli: interfaces
"""

__version__ = "0.1.0"
