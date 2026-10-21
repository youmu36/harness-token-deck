from datetime import timedelta
from typing import cast

import pytest
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.window import (
    Grain,
    MonitorMode,
    Window,
    align,
    bucket_key,
    bucket_label,
    bucket_starts,
    monitor_window,
    next_minute,
)

START = parse_instant("2026-09-13T00:00:00Z")


@pytest.mark.parametrize("delta", [timedelta(), timedelta(milliseconds=-1), timedelta(days=-1)])
def test_empty_or_reversed_range(delta: timedelta) -> None:
    with pytest.raises(DomainError, match="结束时间须晚于开始时间") as error:
        Window(START, Instant(START.value + delta), 0)
    assert error.value.code == "RANGE_INVALID"


@pytest.mark.parametrize("offset", [-841, 841, True, 480.0, "480", None])
def test_invalid_offset(offset: object) -> None:
    with pytest.raises(DomainError) as error:
        Window(START, Instant(START.value + timedelta(hours=1)), cast(int, offset))
    assert (error.value.code, error.value.field) == ("STATISTICS_INVALID", "offset_minutes")


@pytest.mark.parametrize("grain", ["week", "", True, 1, []])
def test_invalid_grain(grain: object) -> None:
    with pytest.raises(DomainError) as error:
        Window(START, Instant(START.value + timedelta(hours=1)), 0, cast(Grain, grain))
    assert (error.value.code, error.value.field) == ("STATISTICS_INVALID", "grain")


@pytest.mark.parametrize(
    ("hours", "extra_ms", "requested", "actual"),
    [
        (24, 0, None, "minute"),
        (24, 1, None, "hour"),
        (72, 0, None, "hour"),
        (72, 1, None, "day"),
        (24, 0, "minute", "minute"),
        (24, 1, "minute", "hour"),
        (72, 1, "minute", "day"),
        (1, 0, "day", "day"),
        (1, 0, "hour", "hour"),
        (720, 0, "day", "day"),
        (73, 0, "twoHours", "twoHours"),
        (168, 0, "twoHours", "twoHours"),
        (168, 1, "twoHours", "day"),
    ],
)
def test_grain_boundaries(hours: int, extra_ms: int, requested: Grain | None, actual: str) -> None:
    end = Instant(START.value + timedelta(hours=hours, milliseconds=extra_ms))
    assert Window(START, end, 0, requested).grain == actual


@pytest.mark.parametrize(
    ("offset", "grain", "key", "label"),
    [
        (0, "minute", "2026-09-13T00:17:00+00:00", "09-13 00:17"),
        (480, "hour", "2026-09-13T08:00:00+08:00", "09-13 08:00"),
        (-210, "hour", "2026-09-12T20:00:00-03:30", "09-12 20:00"),
        (345, "minute", "2026-09-13T06:02:00+05:45", "09-13 06:02"),
        (345, "twoHours", "2026-09-13T06:00:00+05:45", "09-13 06:00"),
        (-210, "twoHours", "2026-09-12T20:00:00-03:30", "09-12 20:00"),
        (840, "day", "2026-09-13T00:00:00+14:00", "2026-09-13"),
        (-840, "day", "2026-09-12T00:00:00-14:00", "2026-09-12"),
    ],
)
def test_offset_alignment_and_rfc3339_keys(offset: int, grain: Grain, key: str, label: str) -> None:
    bucket = align(parse_instant("2026-09-13T00:17:59.999Z"), offset, grain)
    assert bucket_key(bucket) == key
    assert bucket_label(bucket, grain) == label


def test_point_limit_1500_is_inclusive_and_never_truncates() -> None:
    end = Instant(START.value + timedelta(minutes=1500))
    assert len(bucket_starts(START, end, 0, "minute")) == 1500
    with pytest.raises(DomainError, match="趋势点数不能超过 1500") as error:
        bucket_starts(START, Instant(end.value + timedelta(milliseconds=1)), 0, "minute")
    assert error.value.code == "RANGE_TOO_LONG"


def test_unaligned_max_minute_window_has_1441_buckets() -> None:
    start = Instant(START.value + timedelta(milliseconds=1))
    window = Window(start, Instant(start.value + timedelta(hours=24)), 345, "minute")
    points = bucket_starts(window.start, window.end, window.offset_minutes, window.actual_grain)
    assert len(points) == 1441
    assert points[0] < start.value
    assert points[-1] < window.end.value


@pytest.mark.parametrize("offset", [-840, -210, 0, 345, 480, 840])
@pytest.mark.parametrize(
    "now", ["2026-09-13T12:00:00Z", "2026-09-13T12:15:39.999Z", "2026-09-13T12:34:59.999Z"]
)
@pytest.mark.parametrize("mode", ["minute", "hour"])
def test_monitor_has_exactly_60_minutes_or_24_hours(offset: int, now: str, mode: MonitorMode) -> None:
    window = monitor_window(parse_instant(now), mode, offset)
    points = bucket_starts(window.start, window.end, offset, window.actual_grain)
    assert len(points) == (60 if mode == "minute" else 24)
    assert window.end.value.second == window.end.value.microsecond == 0
    assert window.start.value == points[0]
    assert points[-1] < window.end.value


@pytest.mark.parametrize(
    ("now", "start"),
    [
        ("2026-09-13T12:00:00Z", "2026-09-12T12:00:00.000Z"),
        ("2026-09-13T12:00:59.999Z", "2026-09-12T12:00:00.000Z"),
        ("2026-09-13T12:01:00Z", "2026-09-12T13:00:00.000Z"),
    ],
)
def test_monitor_hour_boundary_has_only_completed_minutes(now: str, start: str) -> None:
    assert format_instant(monitor_window(parse_instant(now), "hour", 480).start) == start


def test_monitor_key_changes_only_at_next_minute_and_includes_mode_offset() -> None:
    first = parse_instant("2026-09-13T12:34:00Z")
    last = parse_instant("2026-09-13T12:34:59.999Z")
    assert monitor_window(first, "minute", 480).key == monitor_window(last, "minute", 480).key
    assert next_minute(first) == next_minute(last) == parse_instant("2026-09-13T12:35:00Z")
    assert monitor_window(last, "minute", 480).key != monitor_window(next_minute(last), "minute", 480).key
    assert monitor_window(first, "minute", 480).key != monitor_window(first, "hour", 480).key
    assert monitor_window(first, "minute", 480).key != monitor_window(first, "minute", 0).key


@pytest.mark.parametrize("mode", ["day", None, False, []])
def test_invalid_monitor_mode(mode: object) -> None:
    with pytest.raises(DomainError) as error:
        monitor_window(START, cast(MonitorMode, mode), 0)
    assert error.value.code == "STATISTICS_INVALID"


@pytest.mark.parametrize(("now", "offset"), [("0001-01-01T00:00:00Z", -1), ("9999-12-31T23:59:59Z", 1)])
def test_datetime_edges_report_domain_error(now: str, offset: int) -> None:
    with pytest.raises(DomainError) as error:
        align(parse_instant(now), offset, "minute")
    assert error.value.code == "INVALID_TIMESTAMP"


def test_last_supported_bucket_does_not_increment_past_datetime_max() -> None:
    points = bucket_starts(
        parse_instant("9999-12-31T23:59:00Z"), parse_instant("9999-12-31T23:59:59.999Z"), 0, "minute"
    )
    assert len(points) == 1


def test_monitor_and_next_tick_before_or_after_supported_dates() -> None:
    for now in (parse_instant("0001-01-01T00:00:00Z"),):
        with pytest.raises(DomainError) as error:
            monitor_window(now, "hour", 0)
        assert error.value.code == "INVALID_TIMESTAMP"
    with pytest.raises(DomainError) as error:
        next_minute(parse_instant("9999-12-31T23:59:59Z"))
    assert error.value.code == "INVALID_TIMESTAMP"
