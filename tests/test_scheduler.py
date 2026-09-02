import time
from datetime import datetime, timedelta

import pytest

from hlsd.scheduler import ScheduleParseError, parse_start


def test_none_and_now_are_immediate():
    assert parse_start(None) is None
    assert parse_start("now") is None


def test_relative():
    before = time.time()
    assert parse_start("in 5m") == pytest.approx(before + 300, abs=2)
    assert parse_start("in 1h30m") == pytest.approx(before + 5400, abs=2)
    assert parse_start("in 90s") == pytest.approx(before + 90, abs=2)


def test_absolute_time_today_or_tomorrow():
    now = datetime(2026, 9, 2, 12, 0, 0).astimezone()
    ts = parse_start("17:00", now=now)
    expected = now.replace(hour=17, minute=0, second=0, microsecond=0)
    assert ts == pytest.approx(expected.timestamp())
    ts2 = parse_start("09:00", now=now)
    expected2 = (now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
    assert ts2 == pytest.approx(expected2)


def test_pm_format():
    now = datetime(2026, 9, 2, 10, 0, 0).astimezone()
    ts = parse_start("5:30pm", now=now)
    expected = now.replace(hour=17, minute=30, second=0, microsecond=0)
    assert ts == pytest.approx(expected.timestamp())


def test_iso_datetime():
    now = datetime(2026, 9, 2, 10, 0, 0).astimezone()
    ts = parse_start("2026-09-02T17:00:00", now=now)
    expected = now.replace(hour=17, minute=0, second=0, microsecond=0)
    assert ts == pytest.approx(expected.timestamp())


def test_past_date_raises():
    now = datetime(2026, 9, 2, 10, 0, 0).astimezone()
    with pytest.raises(ScheduleParseError):
        parse_start("2026-09-01T10:00:00", now=now)


def test_garbage_raises():
    with pytest.raises(ScheduleParseError):
        parse_start("tomorrow afternoon")
