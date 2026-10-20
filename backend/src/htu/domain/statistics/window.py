"""Fixed-offset buckets and deterministic query/cache-monitor windows; no clock I/O."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from htu.domain.common.errors import (
    INVALID_TIMESTAMP,
    RANGE_INVALID,
    RANGE_TOO_LONG,
    STATISTICS_INVALID,
    DomainError,
)
from htu.domain.common.time import Instant, format_instant

type Grain = Literal["minute", "hour", "twoHours", "day"]
type MonitorMode = Literal["minute", "hour"]
MAX_POINTS = 1500
_STEPS = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "twoHours": timedelta(hours=2),
    "day": timedelta(days=1),
}
_LIMITS = {
    "minute": timedelta(hours=24),
    "hour": timedelta(hours=72),
    "twoHours": timedelta(days=7),
    "day": timedelta(days=30),
}


def validate_offset(offset_minutes: int) -> None:
    if type(offset_minutes) is not int or not -840 <= offset_minutes <= 840:
        raise DomainError(STATISTICS_INVALID, "时区偏移须为 -840 到 840 的整数分钟", "offset_minutes")


def validate_grain(grain: Grain) -> None:
    if type(grain) is not str or grain not in _STEPS:
        raise DomainError(STATISTICS_INVALID, "统计粒度无效", "grain")


def validate_range(start: Instant, end: Instant) -> timedelta:
    if type(start) is not Instant or type(end) is not Instant:
        raise DomainError(INVALID_TIMESTAMP, "统计区间须使用已校验的 Instant", "range")
    span = end.value - start.value
    if span <= timedelta():
        raise DomainError(RANGE_INVALID, "结束时间须晚于开始时间", "end")
    if span > _LIMITS["day"]:
        raise DomainError(RANGE_TOO_LONG, "自定义范围不能超过 30 天", "range")
    return span


def choose_grain(start: Instant, end: Instant, requested: Grain | None = None) -> Grain:
    span = validate_range(start, end)
    if requested is not None:
        validate_grain(requested)
        if requested == "hour" or span <= _LIMITS[requested]:
            return requested
        return "hour" if span <= _LIMITS["hour"] else "day"
    if span <= _LIMITS["minute"]:
        return "minute"
    return "hour" if span <= _LIMITS["hour"] else "day"


def align(instant: Instant, offset_minutes: int, grain: Grain) -> datetime:
    validate_offset(offset_minutes)
    validate_grain(grain)
    if type(instant) is not Instant:
        raise DomainError(INVALID_TIMESTAMP, "分桶时间须使用已校验的 Instant", "timestamp")
    try:
        local = instant.value.astimezone(timezone(timedelta(minutes=offset_minutes)))
        local = local.replace(second=0, microsecond=0)
        if grain in ("hour", "twoHours", "day"):
            local = local.replace(minute=0)
        if grain == "twoHours":
            local = local.replace(hour=local.hour // 2 * 2)
        return local.replace(hour=0) if grain == "day" else local
    except (ValueError, OverflowError) as error:
        raise DomainError(INVALID_TIMESTAMP, "偏移后的桶超出有效日期范围", "timestamp") from error


def bucket_key(bucket: datetime) -> str:
    return bucket.isoformat(timespec="seconds")


def bucket_label(bucket: datetime, grain: Grain) -> str:
    validate_grain(grain)
    if grain == "day":
        return f"{bucket.year:04}-{bucket.month:02}-{bucket.day:02}"
    return f"{bucket.month:02}-{bucket.day:02} {bucket.hour:02}:{bucket.minute:02}"


def bucket_starts(start: Instant, end: Instant, offset_minutes: int, grain: Grain) -> tuple[datetime, ...]:
    """Enumerate aligned buckets without silently truncating an oversized request."""
    validate_range(start, end)
    cursor = align(start, offset_minutes, grain)
    step = _STEPS[grain]
    points: list[datetime] = []
    while cursor < end.value:
        if len(points) == MAX_POINTS:
            raise DomainError(RANGE_TOO_LONG, "趋势点数不能超过 1500", "grain")
        points.append(cursor)
        # Do not add past datetime.max just to discover that the loop is finished.
        if end.value - cursor <= step:
            break
        cursor += step
    return tuple(points)


@dataclass(frozen=True, slots=True)
class Window:
    start: Instant
    end: Instant
    offset_minutes: int
    grain: Grain | None = None

    def __post_init__(self) -> None:
        validate_offset(self.offset_minutes)
        object.__setattr__(self, "grain", choose_grain(self.start, self.end, self.grain))

    @property
    def actual_grain(self) -> Grain:
        assert self.grain is not None
        return self.grain

    @property
    def key(self) -> tuple[str, str, int, Grain]:
        return format_instant(self.start), format_instant(self.end), self.offset_minutes, self.actual_grain


def floor_minute(now: Instant) -> Instant:
    return Instant(align(now, 0, "minute"))


def next_minute(now: Instant) -> Instant:
    try:
        return Instant(floor_minute(now).value + _STEPS["minute"])
    except OverflowError as error:
        raise DomainError(INVALID_TIMESTAMP, "下一分钟超出有效日期范围", "timestamp") from error


def monitor_window(now: Instant, mode: MonitorMode, offset_minutes: int) -> Window:
    validate_offset(offset_minutes)
    if type(mode) is not str or mode not in ("minute", "hour"):
        raise DomainError(STATISTICS_INVALID, "缓存监控模式无效", "mode")
    end = floor_minute(now)
    try:
        if mode == "minute":
            start = Instant(end.value - timedelta(minutes=60))
        else:
            latest = align(Instant(end.value - timedelta(milliseconds=1)), offset_minutes, "hour")
            start = Instant(latest - timedelta(hours=23))
        return Window(start, end, offset_minutes, mode)
    except OverflowError as error:
        raise DomainError(INVALID_TIMESTAMP, "缓存窗口超出有效日期范围", "timestamp") from error
