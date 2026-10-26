"""Effective costs use frozen targets only where the requested scope proves full coverage."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htd.domain.common.time import Instant, format_instant
from htd.domain.statistics.analysis import decimal_ratio
from htd.domain.statistics.model import Metrics, Summary
from htd.domain.statistics.window import align
from htd.domain.wallet.calibration import DIMENSIONS


@dataclass(frozen=True)
class CostBucket:
    id: str
    target_id: str
    start: str
    end: str
    day: str
    delta: int
    target: int
    estimate: int
    vendor: str
    scope: dict[str, list[str]]
    model_allocated: bool
    ratio_supported: bool = False


@dataclass(frozen=True)
class CostProjection:
    query: dict[str, Any]
    buckets: tuple[CostBucket, ...] = ()
    revision: int = 0

    def scoped(self, **selectors: Any) -> "CostProjection":
        query = dict(self.query)
        for key, value in selectors.items():
            if key == "start":
                query[key] = max(query[key], value)
            elif key == "end":
                query[key] = min(query[key], value)
            elif query.get(key) and value:
                intersection = sorted(set(query[key]).intersection(value))
                if not intersection:
                    return replace(self, buckets=())
                query[key] = intersection
            else:
                query[key] = value
        return replace(self, query=query)

    def period(self, bucket: str, grain: str, zone: str) -> "CostProjection":
        from htd.domain.common.time import parse_instant

        if grain == "hour":
            start = parse_instant(bucket).value
            end = start + timedelta(hours=1)
        else:
            start = datetime.fromisoformat(
                bucket + ("-01-01" if grain == "year" else "-01" if grain == "month" else "")
            ).replace(tzinfo=ZoneInfo(zone))
            end = (
                start.replace(year=start.year + 1)
                if grain == "year"
                else (start + timedelta(days=32)).replace(day=1)
                if grain == "month"
                else start + timedelta(days=1)
            )
        return self.scoped(
            start=format_instant(Instant(start)), end=format_instant(Instant(end)), costGrain=grain
        )

    def selected(self) -> tuple[list[CostBucket], list[CostBucket]]:
        query = self.query
        full: list[CostBucket] = []
        partial: list[CostBucket] = []
        vendors = query.get("vendorIds", []) or ([query["vendorId"]] if query.get("vendorId") else [])
        for part in self.buckets:
            if query.get("vendorId") == "" or (vendors and part.vendor not in vendors):
                continue
            if part.start >= query["end"] or part.end <= query["start"]:
                continue
            if not all(
                not query.get(key) or set(query[key]).intersection(part.scope[key]) for key in DIMENSIONS
            ):
                continue
            supported = query["start"] <= part.start and query["end"] >= part.end
            supported &= all(
                not query.get(key) or set(part.scope[key]).issubset(query[key]) for key in DIMENSIONS
            )
            supported &= not query.get("modelKeys") or part.model_allocated
            supported &= not (
                query.get("modelKeys") and query.get("costGrain") in ("minute", "hour", "twoHours")
            )
            (full if supported else partial).append(part)
        return full, partial

    def costs(self, base: int) -> dict[str, Any]:
        full, partial = self.selected()
        delta = sum(part.delta for part in full)
        unassigned = [part for part in full if not part.model_allocated]
        return {
            "picoUsd": str(base + delta),
            "estimatePico": str(base),
            "correctionPico": str(delta),
            "unallocatedCorrectionPico": str(sum(part.delta for part in [*partial, *unassigned])),
            "allocationIncomplete": bool(partial or unassigned),
        }

    def metrics(self, data: dict[str, Any]) -> dict[str, Any]:
        full, partial = self.selected()
        data.update(self.costs(int(data["picoUsd"])))
        if "eligibleCostPico" not in data:
            return data
        unavailable = bool(partial) or any(
            not part.model_allocated or not part.ratio_supported for part in full
        )
        cost = int(data["eligibleCostPico"]) + sum(part.delta for part in full if part.ratio_supported)
        unavailable |= cost < 0
        data["ratioUnavailable"] = unavailable
        data["componentCostEstimated"] = bool(full or partial)
        if unavailable:
            data["tokensPerDollar"] = None
            data["perDollar"] = dict.fromkeys(data["perDollar"])
            data["eligibleCostPico"] = "0"
            data["eligibleTokens"] = "0"
        else:
            data["eligibleCostPico"] = str(cost)
            data["tokensPerDollar"] = decimal_ratio(int(data["eligibleTokens"]) * 10**12, cost, 1)
            data["perDollar"] = {
                key: decimal_ratio(int(value) * 10**12, cost, 1) for key, value in data["parts"].items()
            }
        return data

    def summary(self, result: Summary) -> Summary:
        def metrics(value: Metrics, projection: CostProjection) -> Metrics:
            costs = projection.costs(int(value.pico_usd))
            return replace(
                value,
                pico_usd=costs["picoUsd"],
                estimate_pico=costs["estimatePico"],
                correction_pico=costs["correctionPico"],
                unallocated_correction_pico=costs["unallocatedCorrectionPico"],
                allocation_incomplete=costs["allocationIncomplete"],
            )

        def period(stamp: Instant, grain: str) -> CostProjection:
            step = (
                timedelta(days=1)
                if grain == "day"
                else timedelta(hours=2 if grain == "twoHours" else 1)
                if grain != "minute"
                else timedelta(minutes=1)
            )
            return self.scoped(
                start=format_instant(stamp), end=format_instant(Instant(stamp.value + step)), costGrain=grain
            )

        from htd.domain.common.time import parse_instant

        model_rows = [
            replace(row, metrics=metrics(row.metrics, self.scoped(modelKeys=[row.model_key])))
            for row in result.models
        ]
        model_rows.sort(key=lambda row: (-int(row.metrics.pico_usd), row.display_name, row.model_key))
        trend_rows = tuple(
            replace(
                row,
                pico_usd=period(
                    Instant(align(parse_instant(row.bucket), result.offset_minutes, result.grain)),
                    result.grain,
                ).costs(int(row.pico_usd))["picoUsd"],
            )
            for row in result.trend
        )
        trend_correction = sum(
            int(new.pico_usd) - int(old.pico_usd) for old, new in zip(result.trend, trend_rows, strict=True)
        )
        return replace(
            result,
            cost_revision=self.revision,
            metrics=metrics(result.metrics, self),
            models=tuple(model_rows),
            harnesses=tuple(
                replace(row, metrics=metrics(row.metrics, self.scoped(harnesses=[row.harness.value])))
                for row in result.harnesses
            ),
            hourly=tuple(
                replace(
                    row,
                    metrics=metrics(
                        row.metrics,
                        period(parse_instant(row.bucket), "hour").scoped(modelKeys=[row.model_key]),
                    ),
                )
                for row in result.hourly
            ),
            hourly_totals=tuple(
                replace(row, metrics=metrics(row.metrics, period(parse_instant(row.bucket), "hour")))
                for row in result.hourly_totals
            ),
            trend=trend_rows,
            unallocated_trend_correction_pico=str(
                int(self.costs(int(result.metrics.pico_usd))["correctionPico"]) - trend_correction
            )
            if result.trend
            else "0",
        )
