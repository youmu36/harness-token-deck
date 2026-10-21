"""Independent throughput reads avoid recomputing cost rankings on output changes."""

import sqlite3
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.output_scope import references as scoped_references
from htd.adapters.sqlite.output_speed import references
from htd.adapters.sqlite.wallet_closing import bounds, timezone
from htd.domain.common.time import Instant, format_instant


def read(db: sqlite3.Connection, query: dict[str, Any], now: Instant) -> dict[str, object]:
    today = now.value.astimezone(ZoneInfo(timezone(db))).date()
    result: list[dict[str, Any]] = []
    keys = tuple(dict.fromkeys(query.get("modelKeys", [])))
    if keys:
        if any(query.get(name) for name in ("vendorIds", "groupIds", "versionIds")):
            result = scoped_references(db, query, now)
        else:
            start = bounds((today - timedelta(days=29)).isoformat(), timezone(db))[0]
            sources = list(dict.fromkeys(query.get("harnesses") or ["all"]))
            # references already weights all sources by session sample counts.
            if len(sources) == 1:
                result = references(db, sources[0], format_instant(start), format_instant(now), None, keys)
            else:
                from decimal import Decimal, localcontext

                merged: dict[tuple[str, str], dict[str, Any]] = {}
                with localcontext() as context:
                    context.prec = 60
                    for source in sources:
                        for row in references(
                            db, source, format_instant(start), format_instant(now), None, keys
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
    return {
        "today": today.isoformat(),
        "references": sorted(result, key=lambda r: (r["day"], r["modelKey"])),
        "generatedAt": format_instant(now),
    }
