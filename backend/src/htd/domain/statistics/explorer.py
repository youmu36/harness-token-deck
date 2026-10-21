"""Mergeable dimensional counters; monetary ratios use integer historical costs."""

from dataclasses import dataclass, field

from htd.domain.statistics.analysis import decimal_ratio

FIELDS = (
    "records",
    "input",
    "cache",
    "output",
    "write",
    "pico",
    "rated_input",
    "rated_cache",
    "unknown",
    "incomplete",
    "unpriced",
    "eligible_input",
    "eligible_cache",
    "eligible_output",
    "eligible_write",
    "eligible_pico",
)


@dataclass
class ExploreTotals:
    values: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FIELDS, 0))

    def add(self, values: dict[str, int], sign: int = 1) -> None:
        for key in FIELDS:
            self.values[key] += sign * values.get(key, 0)

    def export(self) -> dict[str, object]:
        v = self.values
        cost = v["eligible_pico"]
        parts = {
            name: decimal_ratio(v[key] * 10**12, cost, 1)
            for name, key in (
                ("input", "eligible_input"),
                ("cache", "eligible_cache"),
                ("output", "eligible_output"),
                ("write", "eligible_write"),
            )
        }
        total = v["eligible_input"] + v["eligible_cache"] + v["eligible_output"] + v["eligible_write"]
        return {
            "recordCount": str(v["records"]),
            "inputTokens": str(v["input"]),
            "cacheReadTokens": str(v["cache"]),
            "outputTokens": str(v["output"]),
            "cacheWriteTokens": str(v["write"]),
            "newInput": str(v["input"] - v["cache"]),
            "totalTokens": str(v["input"] + v["output"]),
            "picoUsd": str(v["pico"]),
            "cacheHitRate": v["rated_cache"] / v["rated_input"] if v["rated_input"] else None,
            "incomplete": bool(v["incomplete"]),
            "unpriced": bool(v["unpriced"]),
            "cacheCoverageIncomplete": bool(v["unknown"]),
            "tokensPerDollar": decimal_ratio(total * 10**12, cost, 1),
            "perDollar": parts,
            "eligibleCostPico": str(cost),
            "eligibleTokens": str(total),
            "parts": {
                name: str(v[key])
                for name, key in (
                    ("input", "eligible_input"),
                    ("cache", "eligible_cache"),
                    ("output", "eligible_output"),
                    ("write", "eligible_write"),
                )
            },
        }
