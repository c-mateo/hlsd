"""Scheduling of future downloads: relative ("in 5m", "in 1h30m") and
absolute (ISO, "17:00", "5:30pm")."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

log = logging.getLogger("hlsd.scheduler")


class ScheduleParseError(ValueError):
    pass


_REL_RE = re.compile(
    r"^\s*in\s+(?:(\d+)\s*(?:hours?|hrs?|h))?\s*(?:(\d+)\s*(?:minutes?|mins?|m))?\s*(?:(\d+)\s*(?:seconds?|secs?|s))?\s*$",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?\s*(am|pm)?\s*$", re.IGNORECASE)


def parse_start(value: str | None, now: datetime | None = None) -> float | None:
    """Converts a start specification to a unix timestamp, or None if immediate."""
    if value is None or value.strip().lower() in ("", "now", "inmediato", "immediate"):
        return None
    value = value.strip()
    now = now or datetime.now().astimezone()

    match = _REL_RE.match(value)
    if match:
        hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
        total = hours * 3600 + minutes * 60 + seconds
        if total <= 0:
            raise ScheduleParseError(f"Invalid relative duration: {value!r}")
        return time.time() + total

    iso = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
        if dt <= now:
            raise ScheduleParseError(f"Date {value!r} is in the past")
        return dt.timestamp()
    except ValueError:
        pass

    match = _TIME_RE.match(value)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        second = int(match.group(3) or 0)
        ampm = (match.group(4) or "").lower()
        if ampm:
            if not 1 <= hour <= 12:
                raise ScheduleParseError(f"Invalid hour with am/pm: {value!r}")
            if ampm == "pm" and hour != 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
        elif hour > 23:
            raise ScheduleParseError(f"Invalid hour: {value!r}")
        target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    raise ScheduleParseError(f"Could not parse the start time: {value!r}")


class Scheduler:
    """Loop that triggers activation of due requests."""

    def __init__(self, on_due: Callable[[str], Awaitable[None]]):
        self._on_due = on_due
        self._jobs: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopped = False

    def add(self, request_id: str, due_at: float) -> None:
        self._jobs[request_id] = due_at
        self._wake.set()

    def cancel(self, request_id: str) -> None:
        self._jobs.pop(request_id, None)

    def next_due(self) -> float | None:
        return min(self._jobs.values()) if self._jobs else None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopped = False
            self._wake.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stopped:
            if not self._jobs:
                self._wake.clear()
                await self._wake.wait()
                continue
            now = time.time()
            due = [(rid, at) for rid, at in self._jobs.items() if at <= now]
            if due:
                for rid, _ in due:
                    self._jobs.pop(rid, None)
                for rid, _ in due:
                    try:
                        await self._on_due(rid)
                    except Exception:
                        log.exception("scheduled job %s failed to run", rid)
                continue
            next_at = min(self._jobs.values())
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0.05, next_at - time.time()))
            except asyncio.TimeoutError:
                pass
