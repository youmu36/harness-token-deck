"""Stream all authority tiers once, retain bounded aggregate state, page only the result."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htu.adapters.sqlite.ledger_read import LedgerReader, counter_row
from htu.adapters.sqlite.usage_daily import rows as daily_rows
from htu.adapters.sqlite.wallet_closing import timezone
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.statistics.analysis import AnalysisQuery, ExactMetrics, analysis_bounds, changes, shift_month
from htu.domain.statistics.model import LedgerRow
from htu.domain.usage.model import Harness


@dataclass
class Analysis:
    total: ExactMetrics = field(default_factory=ExactMetrics)
    models: dict[str, ExactMetrics] = field(default_factory=lambda: {})
    harnesses: dict[str, ExactMetrics] = field(default_factory=lambda: {})
    trend: dict[str, ExactMetrics] = field(default_factory=lambda: {})
    boundary: bool = False


def first_usage(db: sqlite3.Connection, harness: str) -> str | None:
    clause, args = ("", ()) if harness == "all" else (" WHERE harness=?", (harness,))
    return db.execute(
        "SELECT MIN(stamp) FROM ("
        + " UNION ALL ".join(
            f"SELECT MIN({column}) stamp FROM {table}{clause}"
            for table, column in (
                ("ledger", "occurred_at"),
                ("usage_rollup", "bucket"),
                ("usage_archive", "start_at"),
                ("usage_daily", "start_at"),
            )
        )
        + ")",
        args * 4,
    ).fetchone()[0]


def aggregate(
    db: sqlite3.Connection,
    query: AnalysisQuery,
    start: Instant,
    end: Instant,
    zone: ZoneInfo,
    grain: str,
    *,
    groups: bool = True,
) -> Analysis:
    result = Analysis()
    reader = LedgerReader(db)
    harness = "all" if query.harness == "all" else Harness(query.harness)
    start_text, end_text = format_instant(start), format_instant(end)
    processed = 0

    def accept(
        row: LedgerRow,
        *,
        minute_start: int | None = None,
        active_bits: int | None = None,
        missing: tuple[str, ...] = (),
    ) -> None:
        nonlocal processed
        local = row.occurred_at.value.astimezone(zone)
        bucket = local.strftime("%Y" if grain == "year" else "%Y-%m" if grain == "month" else "%Y-%m-%d")
        targets = [result.total]
        if groups:
            if row.model_key not in result.models and len(result.models) >= 5000:
                raise DomainError("ANALYSIS_LIMIT", "模型分组超过 5000 个，请缩小范围或选择单个模型")
            targets.extend(
                (
                    result.models.setdefault(row.model_key, ExactMetrics()),
                    result.harnesses.setdefault(row.harness.value, ExactMetrics()),
                    result.trend.setdefault(bucket, ExactMetrics()),
                )
            )
        for target in targets:
            target.add(row, zone=zone, minute_start=minute_start, active_bits=active_bits, missing=missing)
        processed += 1
        if processed % 512 == 0:
            counters = [
                result.total,
                *result.models.values(),
                *result.harnesses.values(),
                *result.trend.values(),
            ]
            estimated = sum(len(c.minutes) * 256 + len(c.days) * 80 + 1024 for c in counters)
            if estimated > 16 * 1024**2:
                raise DomainError("ANALYSIS_LIMIT", "分析分组已达内存预算，请缩小年份范围或选择单个模型")

    # This path has its own calendar contract. The ordinary Window limit stays 30 days.
    for row in reader.analysis_rows(start, end, harness, query.model_key):
        accept(row)
    for record, counter in daily_rows(db, start_text, end_text, query.harness, query.model_key):
        result.boundary |= start_text > record["start_at"] or end_text < record["end_at"]
        accept(
            counter_row(counter),
            minute_start=int(parse_instant(record["start_at"]).value.timestamp()) // 60,
            active_bits=int(record["active_minutes"]),
            missing=tuple(json.loads(record["missing_capabilities"])),
        )
    # Empty buckets are meaningful, but chart size is bounded independently of years of data.
    cursor = start.value.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    cursor = (
        cursor.replace(month=1, day=1)
        if grain == "year"
        else cursor.replace(day=1)
        if grain == "month"
        else cursor
    )
    while cursor < end.value.astimezone(zone):
        key = cursor.strftime("%Y" if grain == "year" else "%Y-%m" if grain == "month" else "%Y-%m-%d")
        if groups:
            result.trend.setdefault(key, ExactMetrics())
        if len(result.trend) > 120:
            raise DomainError("ANALYSIS_LIMIT", "趋势最多 120 个点，请按月或年查看")
        cursor = (
            shift_month(cursor, 12 if grain == "year" else 1)
            if grain != "day"
            else cursor + timedelta(days=1)
        )
    return result


def analyze(db: sqlite3.Connection, query: AnalysisQuery, now: Instant) -> dict[str, Any]:
    zone_name = timezone(db)
    zone = ZoneInfo(zone_name)
    first = first_usage(db, query.harness)
    start, end, previous, grain = analysis_bounds(query, now, zone_name, first)
    data = aggregate(db, query, start, end, zone, grain)
    comparison = None
    if previous is not None:
        before = aggregate(db, query, previous[0], previous[1], zone, grain, groups=False)
        comparison = {
            "start": format_instant(previous[0]),
            "end": format_instant(previous[1]),
            "totals": before.total.export(),
            "changes": changes(data.total, before.total),
            "includesWholeBoundaryDays": before.boundary,
        }
    ranked = sorted(
        data.models.items(), key=lambda item: (-item[1].counts.pico, item[1].counts.name, item[0])
    )
    from htu.adapters.sqlite.output_speed import ranking, references

    page_keys = tuple(key for key, _ in ranked[query.offset : query.offset + query.limit])
    speeds = ranking(db, query.harness, page_keys)

    output_references = references(
        db,
        query.harness,
        format_instant(Instant(now.value - timedelta(days=30))),
        format_instant(now),
        query.model_key,
        page_keys,
    )
    return {
        "start": format_instant(start),
        "end": format_instant(end),
        "businessTimezone": zone_name,
        "grain": grain,
        "totals": data.total.export(),
        "comparison": comparison,
        "trend": [
            {"bucket": key, "label": key, **metrics.export()} for key, metrics in sorted(data.trend.items())
        ],
        "models": [
            {
                "modelKey": key,
                "displayName": metrics.counts.name,
                **metrics.export(),
                "tokensPerSecond": speeds.get(key),
            }
            for key, metrics in ranked[query.offset : query.offset + query.limit]
        ],
        "harnesses": [
            {"harness": name, **metrics.export()} for name, metrics in sorted(data.harnesses.items())
        ],
        "totalModels": len(ranked),
        "outputReferences": output_references,
        "nextOffset": query.offset + query.limit if query.offset + query.limit < len(ranked) else None,
        "includesWholeBoundaryDays": data.boundary,
        "generatedAt": format_instant(now),
        "revision": db.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0],
        "basis": "本地用量与估算成本，输出吞吐按完成分钟计算",
        "comparisonNotice": "按业务时区比较相同日历进度，短月与闰日对齐到上期最后一日。零基期不计算增长率。",
    }
