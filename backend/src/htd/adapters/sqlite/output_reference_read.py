"""Independent throughput reads avoid recomputing cost rankings on output changes."""

import sqlite3
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.output_scope import references as scoped_references
from htd.adapters.sqlite.output_speed import references
from htd.adapters.sqlite.wallet_closing import bounds, timezone
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant


def read(
    db: sqlite3.Connection, query: dict[str, Any], now: Instant, *, include_today: bool = True
) -> dict[str, object]:
    today = now.value.astimezone(ZoneInfo(timezone(db))).date()
    start = (
        parse_instant(query["start"])
        if query.get("start")
        else bounds((today - timedelta(days=29)).isoformat(), timezone(db))[0]
    )
    end = parse_instant(query["end"]) if query.get("end") else now
    if bool(query.get("start")) != bool(query.get("end")):
        raise DomainError("RANGE_INVALID", "吞吐参考须同时填写起止时间")
    if end <= start or end > now or end.value - start.value > timedelta(days=36600):
        raise DomainError("RANGE_INVALID", "请选择有效且不包含未来的吞吐范围")
    scoped = any(query.get(name) for name in ("vendorIds", "groupIds", "versionIds"))
    coverage: list[dict[str, str]] = []
    supported = not scoped or start.value >= now.value - timedelta(days=62)
    if not supported:
        coverage.append(
            {
                "code": "tps_scope_history",
                "message": "供应商、组与版本的详细吞吐只保留最近 62 天，当前范围不支持此筛选。",
            }
        )
    query = dict(
        query,
        startDay=start.value.astimezone(ZoneInfo(timezone(db))).date().isoformat(),
        endDay=(end.value - timedelta(microseconds=1)).astimezone(ZoneInfo(timezone(db))).date().isoformat(),
    )
    result: list[dict[str, Any]] = []
    keys = tuple(dict.fromkeys(query.get("modelKeys", [])))
    if keys and supported:
        if scoped:
            result = scoped_references(db, query, now)
        else:
            sources = list(dict.fromkeys(query.get("harnesses") or ["all"]))
            # references already weights all sources by session sample counts.
            if len(sources) == 1:
                result = references(
                    db, sources[0], format_instant(start), format_instant(end), None, keys, full_range=True
                )
            else:
                from decimal import Decimal, localcontext

                merged: dict[tuple[str, str], dict[str, Any]] = {}
                with localcontext() as context:
                    context.prec = 60
                    for source in sources:
                        for row in references(
                            db,
                            source,
                            format_instant(start),
                            format_instant(end),
                            None,
                            keys,
                            full_range=True,
                        ):
                            item = merged.setdefault(
                                (row["day"], row["modelKey"]), {**row, "sum": Decimal(0), "samples": 0}
                            )
                            if row["tokensPerSecond"] is not None:
                                item["sum"] += Decimal(row["tokensPerSecond"]) * row["samples"]
                                item["samples"] += row["samples"]
                    for row in merged.values():
                        total = row.pop("sum")
                        row["tokensPerSecond"] = (
                            str((total / row["samples"]).quantize(Decimal("0.1"))) if row["samples"] else None
                        )
                        result.append(row)
    today_rows = [row for row in result if row["day"] == today.isoformat()]
    today_start = bounds(today.isoformat(), timezone(db))[0]
    if (
        include_today
        and keys
        and today_start < now
        and (not supported or not query["startDay"] <= today.isoformat() <= query["endDay"])
    ):
        today_rows = read(
            db,
            dict(query, start=format_instant(today_start), end=format_instant(now)),
            now,
            include_today=False,
        )["references"]
    return {
        "today": today.isoformat(),
        "references": sorted(result, key=lambda r: (r["day"], r["modelKey"])),
        "generatedAt": format_instant(now),
        "start": format_instant(start),
        "end": format_instant(end),
        "coverage": coverage,
        "todayReferences": today_rows,
        "businessTimezone": timezone(db),
    }
