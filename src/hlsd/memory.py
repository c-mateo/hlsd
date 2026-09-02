"""Volatile segment store (bounded memory) and memory utilities."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import sys
from collections import OrderedDict


class MemoryPressureError(RuntimeError):
    pass


class VolatileSegmentStore:
    """Segments in RAM with a hard cap. When the cap is reached, `put`
    suspends (backpressure) until space is freed, instead of silently
    discarding segments."""

    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes
        self._data: OrderedDict[tuple, bytes] = OrderedDict()
        self._bytes = 0
        self._cond: asyncio.Condition | None = None

    def _ensure_cond(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    @property
    def bytes_used(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._data)

    async def put(self, key: tuple, data: bytes) -> None:
        cond = self._ensure_cond()
        async with cond:
            while self._bytes + len(data) > self.max_bytes:
                await cond.wait()
            self._data[key] = bytes(data)
            self._bytes += len(data)

    def put_nowait(self, key: tuple, data: bytes) -> None:
        if self._bytes + len(data) > self.max_bytes:
            raise MemoryPressureError(f"memory cap reached ({self._bytes + len(data)} > {self.max_bytes})")
        self._data[key] = bytes(data)
        self._bytes += len(data)

    def get(self, key: tuple) -> bytes | None:
        return self._data.get(key)

    def has(self, key: tuple) -> bool:
        return key in self._data

    def delete(self, key: tuple) -> None:
        data = self._data.pop(key, None)
        if data is not None:
            self._bytes -= len(data)
            self._notify()

    def delete_prefix(self, prefix: tuple) -> int:
        keys = [k for k in self._data if k[: len(prefix)] == prefix]
        for k in keys:
            self._bytes -= len(self._data.pop(k))
        if keys:
            self._notify()
        return len(keys)

    def clear(self) -> None:
        self._data.clear()
        self._bytes = 0
        self._notify()

    def _notify(self) -> None:
        cond = self._cond
        if cond is not None:
            async def _wake() -> None:
                async with cond:
                    cond.notify_all()
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_wake())
            except RuntimeError:
                pass

    def trim_process_memory(self) -> None:
        malloc_trim()


def malloc_trim() -> int:
    """Returns free memory to the OS on glibc (mitigates allocator retention)."""
    if sys.platform != "linux":
        return 0
    try:
        libc = ctypes.CDLL("libc.so.6")
        return libc.malloc_trim(0)
    except (OSError, AttributeError):
        return 0


def process_rss_bytes() -> int:
    with contextlib.suppress(Exception):
        import psutil  # type: ignore

        return psutil.Process().memory_info().rss
    if sys.platform == "linux" or sys.platform == "darwin":
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    return 0
