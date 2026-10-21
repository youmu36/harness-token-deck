"""One streaming accumulator for overview, Harness statistics, and cache monitoring."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from htd.domain.common.errors import COUNT_OVERFLOW, STATISTICS_INVALID, DomainError
from htd.domain.common.time import Instant
from htd.domain.statistics.cache_warnings import cache_warnings
from htd.domain.statistics.model import (
    HarnessSummary,
    HourlySummary,
    LedgerRow,
    Metrics,
    ModelSummary,
    OrderKey,
    Summary,
    TrendPoint,
)
from htd.domain.statistics.window import Grain, Window, align, bucket_key, bucket_label, bucket_starts
from htd.domain.usage.model import MAX_TOKEN_COUNT, Harness

_HARNESS_NAMES = {
    Harness.CLAUDE: "Claude Code",
    Harness.CODEX: "Codex",
    Harness.ZCODE: "Zcode",
    Harness.DSH: "Deepseek Harness",
}

type Section = Literal["trend", "models", "hourly", "hourlyTotals", "harnesses"]
ALL_SECTIONS: frozenset[Section] = frozenset(("trend", "models", "hourly", "hourlyTotals", "harnesses"))


@dataclass(slots=True)
class MetricsAccumulator:
    exact: bool = False
    records: int = 0
    input: int = 0
    output: int = 0
    cache: int = 0
    rated_input: int = 0
    rated_cache: int = 0
    unknown_cache: int = 0
    miss_input: int = 0
    missing_usage: int = 0
    unknown_model: int = 0
    incomplete: bool = False
    unpriced: bool = False
    pico: int = 0
    cache_loss: bool = False
    cache_bust: bool = False
    context_compacted: bool = False
    latest: LedgerRow | None = None

    def merge(self, other: "MetricsAccumulator") -> None:
        for name in (
            "records",
            "input",
            "output",
            "cache",
            "rated_input",
            "rated_cache",
            "unknown_cache",
            "miss_input",
            "missing_usage",
            "unknown_model",
            "pico",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        if (
            not self.exact
            and max(self.records, self.input, self.output, self.cache, self.input + self.output)
            > MAX_TOKEN_COUNT
        ):
            raise DomainError(COUNT_OVERFLOW, "统计计数超出 JSON 安全整数范围", "counts")
        for name in ("incomplete", "unpriced", "cache_loss", "cache_bust", "context_compacted"):
            setattr(self, name, getattr(self, name) or getattr(other, name))
        if other.latest and (self.latest is None or other.latest.latest_key > self.latest.latest_key):
            self.latest = other.latest

    def add(self, row: LedgerRow) -> None:
        counts = row.rollup
        records = counts.records if counts else 1
        input_total = self.input + (row.input or 0)
        output_total = self.output + (row.output or 0)
        cache_total = self.cache + (row.cache_read or 0)
        if not self.exact and any(
            value > MAX_TOKEN_COUNT
            for value in (
                self.records + records,
                input_total,
                output_total,
                cache_total,
                input_total + output_total,
            )
        ):
            raise DomainError(COUNT_OVERFLOW, "统计计数超出 JSON 安全整数范围", "counts")
        self.records += records
        bust, loss = (
            (counts.cache_bust, counts.large_miss)
            if counts
            else cache_warnings(
                row.input,
                row.cache_read,
                session=row.session_id is not None,
                unprobed=row.unprobed,
                compacted=row.context_compacted,
            )
        )
        self.cache_bust |= bust
        self.cache_loss |= loss
        self.context_compacted |= row.context_compacted
        self.input, self.output, self.cache = input_total, output_total, cache_total
        if counts is not None:
            self.rated_input += counts.rated_input
            self.rated_cache += counts.rated_cache
            self.unknown_cache += counts.unknown_cache
            self.miss_input += counts.miss_input
            self.missing_usage += counts.missing_usage
            self.unknown_model += counts.unknown_model
        elif row.input is not None:
            if row.cache_read is None:
                self.unknown_cache += 1
            elif row.cache_read > 0 or not row.unprobed:
                self.rated_input += row.input
                self.rated_cache += row.cache_read
            if row.cache_read == 0 and not row.unprobed:
                self.miss_input += row.input
        if counts is None:
            self.missing_usage += row.input is None or row.output is None
            self.unknown_model += row.model_id is None
        self.incomplete |= row.incomplete
        self.unpriced |= row.unpriced
        self.pico += row.pico_usd
        if self.latest is None or row.latest_key > self.latest.latest_key:
            self.latest = row

    @property
    def name(self) -> str:
        return "模型未知" if self.latest is None else self.latest.name

    def metrics(self) -> Metrics:
        return Metrics(
            self.records,
            self.input,
            self.output,
            self.cache,
            self.input - self.cache,
            self.input + self.output,
            self.rated_cache / self.rated_input if self.rated_input else None,
            self.unknown_cache > 0,
            str(self.pico),
            self.incomplete,
            self.unpriced,
            self.unknown_model,
            self.missing_usage,
        )

    def trend(self, bucket: datetime, grain: Grain) -> TrendPoint:
        return TrendPoint(
            bucket_key(bucket),
            bucket_label(bucket, grain),
            self.input,
            self.output,
            self.cache,
            self.rated_cache / self.rated_input if self.rated_input else None,
            self.cache_bust,
            self.cache_loss and not self.cache_bust,
            str(self.pico),
            self.records,
            self.context_compacted,
        )


class Aggregator:
    def __init__(
        self,
        offset_minutes: int,
        grain: Grain | None,
        start: Instant,
        end: Instant,
        *,
        harness: Harness | str = "all",
        sections: frozenset[Section] = ALL_SECTIONS,
        model_key: str | None = None,
    ) -> None:
        self.window = Window(start, end, offset_minutes, grain)
        self.harness = "all" if harness == "all" else Harness(harness)
        self.sections = sections
        self.model_key = model_key
        self._total = MetricsAccumulator()
        self._trend: dict[datetime, MetricsAccumulator] = {}
        self._models: dict[str, MetricsAccumulator] = {}
        self._hourly: dict[tuple[datetime, str], MetricsAccumulator] = {}
        self._hourly_totals: dict[datetime, MetricsAccumulator] = {}
        self._harnesses: dict[Harness, MetricsAccumulator] = {}
        self._last_order: OrderKey | None = None
        self._daily_history = False

    def push(self, row: LedgerRow) -> None:
        if type(row) is not LedgerRow:
            raise DomainError(STATISTICS_INVALID, "统计行须为已校验的 LedgerRow", "row")
        window = self.window
        if self.harness != "all" and row.harness != self.harness:
            return
        if not window.start <= row.occurred_at < window.end:
            return
        if self.model_key is not None and row.model_key != self.model_key:
            return
        if row.rollup is None and self._last_order is not None and row.order_key <= self._last_order:
            raise DomainError(STATISTICS_INVALID, "账本行重复或未按会话时间顺序读取", "rows")
        # Validate global counts before changing any group or session state.
        self._total.add(row)
        self._daily_history |= row.daily_history
        # Immutable counters from separate archive blocks may overlap in time;
        # deduplication is committed with the source identity, before compaction.
        if row.rollup is None:
            self._last_order = row.order_key

        def add[K](groups: dict[K, MetricsAccumulator], key: K) -> None:
            if key not in groups:
                groups[key] = MetricsAccumulator()
            groups[key].add(row)

        if "trend" in self.sections and not row.daily_history:
            add(self._trend, align(row.occurred_at, window.offset_minutes, window.actual_grain))
        if "models" in self.sections:
            add(self._models, row.model_key)
        if "hourly" in self.sections and not row.daily_history:
            add(self._hourly, (align(row.occurred_at, window.offset_minutes, "hour"), row.model_key))
        if "hourlyTotals" in self.sections and not row.daily_history:
            add(self._hourly_totals, align(row.occurred_at, window.offset_minutes, "hour"))
        if "harnesses" in self.sections and self.harness == "all":
            add(self._harnesses, row.harness)

    def merge(self, other: "Aggregator") -> None:
        """Combine disjoint immutable query parts without losing weighted denominators."""
        self._total.merge(other._total)
        self._daily_history |= other._daily_history
        for name in ("_trend", "_models", "_hourly", "_hourly_totals", "_harnesses"):
            groups = getattr(self, name)
            for key, acc in getattr(other, name).items():
                if key not in groups:
                    groups[key] = MetricsAccumulator()
                groups[key].merge(acc)

    def estimated_bytes(self) -> int:
        return 1024 + sum(
            len(groups) * 1400
            for groups in (self._trend, self._models, self._hourly, self._hourly_totals, self._harnesses)
        )

    @property
    def has_daily_history(self) -> bool:
        return self._daily_history

    def finish(self) -> Summary:
        window = self.window
        trend = (
            tuple(
                self._trend.get(bucket, MetricsAccumulator()).trend(bucket, window.actual_grain)
                for bucket in bucket_starts(
                    window.start, window.end, window.offset_minutes, window.actual_grain
                )
            )
            if self._total.records and "trend" in self.sections and not self._daily_history
            else ()
        )
        models = tuple(
            ModelSummary(key, acc.name, acc.metrics())
            for key, acc in sorted(
                self._models.items(), key=lambda item: (-item[1].pico, item[1].name, item[0])
            )
        )
        hourly = tuple(
            HourlySummary(bucket_key(bucket), bucket_label(bucket, "hour"), key, acc.name, acc.metrics())
            for (bucket, key), acc in sorted(self._hourly.items())
        )
        totals = tuple(
            HourlySummary(bucket_key(bucket), bucket_label(bucket, "hour"), None, "全部模型", acc.metrics())
            for bucket, acc in sorted(self._hourly_totals.items())
        )
        harnesses = (
            tuple(
                HarnessSummary(harness, _HARNESS_NAMES[harness], self._harnesses[harness].metrics())
                for harness in Harness
                if harness in self._harnesses
            )
            if self.harness == "all"
            else ()
        )
        return Summary(
            window.start,
            window.end,
            window.offset_minutes,
            window.actual_grain,
            self._total.metrics(),
            trend,
            models,
            () if self._daily_history else hourly,
            () if self._daily_history else totals,
            harnesses,
            "此区间包含仅保留业务日汇总的历史；总量包含完整边界日，无法恢复小时分布，趋势与小时明细不展示。"  # noqa: RUF001
            if self._daily_history
            else None,
        )
