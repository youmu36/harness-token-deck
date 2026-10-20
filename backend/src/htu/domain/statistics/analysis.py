"""Calendar analysis and exact counters; output throughput uses completion-minute unions."""

import calendar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal, localcontext
from typing import Literal
from zoneinfo import ZoneInfo

from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.statistics.aggregator import MetricsAccumulator
from htu.domain.statistics.model import LedgerRow

type AnalysisPeriod = Literal["range", "month", "quarter", "year", "years", "all"]


@dataclass(frozen=True)
class AnalysisQuery:
    harness: str = "all"
    period: AnalysisPeriod = "month"
    year: int | None = None
    end_year: int | None = None
    quarter: int | None = None
    start: str | None = None
    end: str | None = None
    model_key: str | None = None
    offset: int = 0
    limit: int = 50


def month_start(year: int, month: int, zone: tzinfo | None) -> datetime:
    year, index = divmod(year * 12 + month - 1, 12)
    return datetime(year, index + 1, 1, tzinfo=zone)


def shift_month(value: datetime, months: int) -> datetime:
    first = month_start(value.year, value.month + months, value.tzinfo)
    return first.replace(
        day=min(value.day, calendar.monthrange(first.year, first.month)[1]),
        hour=value.hour,
        minute=value.minute,
        second=value.second,
        microsecond=value.microsecond,
    )


def analysis_bounds(query: AnalysisQuery, now: Instant, zone_name: str, first: str | None):
    zone = ZoneInfo(zone_name)
    local_now = now.value.astimezone(zone)
    year = query.year or local_now.year
    previous: tuple[datetime, datetime] | None = None
    if query.period == "range":
        if not query.start or not query.end:
            raise DomainError("RANGE_INVALID", "请填写完整分析范围")
        start, end = parse_instant(query.start).value, parse_instant(query.end).value
        if end - start > timedelta(days=93):
            raise DomainError("RANGE_TOO_LONG", "近期分析范围最多 93 天，更长历史请选择年份或全部历史")
        previous = start - (end - start), start
    elif query.period == "all":
        start = parse_instant(first).value if first else local_now - timedelta(days=1)
        # Include the complete first business day when it has permanent authority.
        start = start.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        end = local_now
    else:
        if query.period == "month":
            start = month_start(local_now.year, local_now.month, zone)
            months = 1
        elif query.period == "quarter":
            start = month_start(year, ((query.quarter or (local_now.month - 1) // 3 + 1) - 1) * 3 + 1, zone)
            months = 3
        else:
            last_year = (query.end_year or year) if query.period == "years" else year
            if last_year < year or last_year - year > 99:
                raise DomainError("RANGE_INVALID", "年份范围须按先后顺序且不超过 100 年")
            start = month_start(year, 1, zone)
            months = (last_year - year + 1) * 12
        full_end = shift_month(start, months)
        end = min(full_end, local_now)
        previous_start = shift_month(start, -months)
        previous_end = start if end == full_end else shift_month(end, -months)
        previous = previous_start, previous_end
    if end <= start:
        raise DomainError("RANGE_INVALID", "分析范围尚未开始或结束时间无效")
    grain = "day" if query.period in ("range", "month") else "month"
    if (end.year - start.year) * 12 + end.month - start.month >= 120:
        grain = "year"
    return (
        Instant(start.astimezone(UTC)),
        Instant(end.astimezone(UTC)),
        None if previous is None else tuple(Instant(v.astimezone(UTC)) for v in previous),
        grain,
    )


def decimal_ratio(numerator: int, denominator: int, places: int = 2) -> str | None:
    if denominator == 0:
        return None
    with localcontext() as context:
        context.prec = max(40, len(str(abs(numerator))) + len(str(abs(denominator))) + places + 2)
        return f"{Decimal(numerator) / Decimal(denominator):.{places}f}"


@dataclass
class ExactMetrics:
    counts: MetricsAccumulator = field(default_factory=lambda: MetricsAccumulator(exact=True))
    minutes: dict[int, int] = field(default_factory=lambda: {})
    days: set[str] = field(default_factory=set[str])
    missing: set[str] = field(default_factory=set[str])
    first: str | None = None
    last: str | None = None
    last_output: str | None = None

    def add(
        self,
        row: LedgerRow,
        *,
        zone: ZoneInfo,
        minute_start: int | None = None,
        active_bits: int | None = None,
        missing: tuple[str, ...] = (),
    ) -> None:
        self.counts.add(row)
        self.missing.update(missing)
        if (row.rollup and row.rollup.missing_usage) or row.output is None:
            self.missing.add("complete_output_samples")
        stamp = format_instant(row.latest_at or row.occurred_at)
        self.first = min(self.first or stamp, format_instant(row.occurred_at))
        self.last = max(self.last or stamp, stamp)
        self.days.add(row.occurred_at.value.astimezone(zone).date().isoformat())
        if active_bits is None:
            active_bits = int((row.output or 0) > 0)
            minute_start = int(row.occurred_at.value.timestamp()) // 60
        assert minute_start is not None
        if (row.output or 0) > 0 and not active_bits:
            self.missing.add("active_output_minutes")
        if active_bits:
            output_at = format_instant(
                Instant(datetime.fromtimestamp((minute_start + active_bits.bit_length() - 1) * 60, UTC))
            )
            self.last_output = max(self.last_output or output_at, output_at)
        # UTC-day bitmaps combine even when business-day offsets or DST differ.
        while active_bits:
            day, offset = divmod(minute_start, 1440)
            length = 1440 - offset
            self.minutes[day] = self.minutes.get(day, 0) | ((active_bits & ((1 << length) - 1)) << offset)
            active_bits >>= length
            minute_start += length

    def export(self) -> dict[str, object]:
        count = self.counts
        active = sum(bits.bit_count() for bits in self.minutes.values())
        return {
            "recordCount": str(count.records),
            "inputTokens": str(count.input),
            "outputTokens": str(count.output),
            "cacheReadTokens": str(count.cache),
            "totalTokens": str(count.input + count.output),
            "picoUsd": str(count.pico),
            "cacheHitRate": count.rated_cache / count.rated_input if count.rated_input else None,
            "cacheCoverageIncomplete": count.unknown_cache > 0,
            "incomplete": count.incomplete,
            "unpriced": count.unpriced,
            "unknownModelCount": str(count.unknown_model),
            "missingUsageCount": str(count.missing_usage),
            "activeMinutes": str(active),
            "tokensPerMinute": decimal_ratio(count.output, active)
            if "active_output_minutes" not in self.missing
            else None,
            "tokensPerSecond": decimal_ratio(count.output, active * 60)
            if "active_output_minutes" not in self.missing
            else None,
            "coveredDays": len(self.days),
            "coverageStart": self.first,
            "coverageEnd": self.last,
            "lastOutputAt": self.last_output,
            "missingCapabilities": sorted(self.missing),
        }


def changes(current: ExactMetrics, previous: ExactMetrics) -> dict[str, str | None]:
    pairs = {"cost": "pico", "tokens": "input", "output": "output", "records": "records"}
    result: dict[str, str | None] = {}
    for name, field_name in pairs.items():
        before, after = getattr(previous.counts, field_name), getattr(current.counts, field_name)
        if name == "tokens":
            before += previous.counts.output
            after += current.counts.output
        result[name] = decimal_ratio((after - before) * 100, before)
    return result
