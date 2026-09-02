"""HTTP client shared by the pollers of a Source.

Applies the RequestTemplate (headers/cookies/body/auth extracted from cURL)
to everything it downloads: playlists, variants, segments, audio renditions.
Includes retries with exponential backoff + jitter and a semaphore to avoid
saturating the server.
"""

from __future__ import annotations

import asyncio
import random
from urllib.parse import parse_qsl, urlparse

import httpx

from .models import RequestTemplate


class FetchError(RuntimeError):
    pass


class GlobalLimiter:
    """Cap on simultaneous HTTP requests across the WHOLE daemon (all
    sources). Configured once at startup; the semaphore is created
    lazily inside the daemon's event loop."""

    def __init__(self) -> None:
        self._limit = 3
        self._sem: asyncio.Semaphore | None = None

    def configure(self, limit: int) -> None:
        self._limit = max(1, int(limit))

    @property
    def limit(self) -> int:
        return self._limit

    def _get(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self._limit)
        return self._sem

    def __call__(self):
        return self._get()

    def reset(self) -> None:
        """After an event loop restart (tests) forces a fresh semaphore."""
        self._sem = None


global_limiter = GlobalLimiter()


class HttpClient:
    def __init__(
        self,
        template: RequestTemplate,
        *,
        concurrency: int = 4,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.template = template
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sem = asyncio.Semaphore(max(1, concurrency))
        headers = {k: v for k, v in template.headers.items()}
        headers.setdefault("Accept", "*/*")
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            headers=headers,
            cookies=template.cookies or None,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_concurrency(self, n: int) -> None:
        """Adjusts the download semaphore at runtime (lives: low; VOD: high)."""
        self._sem = asyncio.Semaphore(max(1, n))

    async def fetch(self, url: str, *, params: dict | None = None, timeout: float | None = None) -> bytes:
        attempt = 0
        last_error: Exception | None = None
        if params:
            # httpx REPLACES the URL query when params= is passed;
            # we must merge here to avoid losing tokens/sessions already
            # present in the query (e.g. LL-HLS blocking reload on URLs
            # with ?session=...)
            existing = dict(parse_qsl(urlparse(url).query))
            existing.update(params)
            params = existing
        while attempt <= self.max_retries:
            try:
                async with global_limiter(), self._sem:
                    response = await self._client.request(
                        self.template.method,
                        url,
                        params=params,
                        content=self.template.body,
                        auth=self.template.auth,
                        timeout=timeout,
                    )
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", "0") or 0)
                    raise _Retryable(httpx.HTTPStatusError(f"429 from {url}", request=response.request, response=response), retry_after)
                response.raise_for_status()
                return response.content
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise FetchError(f"HTTP {exc.response.status_code} on {url}") from exc
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc
            except _Retryable as exc:
                last_error = exc.cause
                attempt += 1
                await asyncio.sleep(max(exc.retry_after, 0.1) if exc.retry_after else self._backoff(attempt))
                continue
            attempt += 1
            if attempt <= self.max_retries:
                await asyncio.sleep(self._backoff(attempt))
        raise FetchError(f"Failed to download {url} after {self.max_retries + 1} attempts: {last_error!r}")

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base * (2 ** (attempt - 1)) * (0.5 + random.random())

    async def fetch_text(self, url: str, *, params: dict | None = None, timeout: float | None = None) -> str:
        data = await self.fetch(url, params=params, timeout=timeout)
        return data.decode("utf-8", errors="replace")


class _Retryable(Exception):
    def __init__(self, cause: Exception, retry_after: float = 0.0):
        super().__init__(str(cause))
        self.cause = cause
        self.retry_after = retry_after
