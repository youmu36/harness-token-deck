"""Minimal validated ledger projection and immutable statistics snapshots."""

from dataclasses import dataclass, field

from htd.domain.common.errors import COST_OVERFLOW, PICO_INVALID, STATISTICS_INVALID, DomainError
from htd.domain.common.time import Instant, format_instant
from htd.domain.statistics.window import Grain
from htd.domain.usage.model import Harness, Tokens
from htd.domain.usage.model_key import model_key

type MetricValue = int | str | float | bool | None
type OrderKey = tuple[str, str, Instant, str]


@dataclass(frozen=True, slots=True)
class RollupCounts:
    records: int
    rated_input: int
    rated_cache: int
    unknown_cache: int
    missing_usage: int
    unknown_model: int
    miss_input: int
    cache_loss: bool
    cache_bust: bool = False
    large_miss: bool = False


@dataclass(frozen=True, slots=True)
class LedgerRow:
    harness: Harness
    dedupe_key: str
    session_id: str | None
    occurred_at: Instant
    model_id: str | None
    input: int | None
    cache_read: int | None
    output: int | None
    pico_usd: int
    incomplete: bool
    unpriced: bool
    display_name: str | None = None
    rollup: RollupCounts | None = None
    prior_cache: int | None = None
    latest_at: Instant | None = None
    daily_history: bool = False
    context_compacted: bool = False
    pending_valuation: bool = False
    model_key: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "harness", Harness(self.harness))
        if type(self.occurred_at) is not Instant:
            raise DomainError(STATISTICS_INVALID, "账本时间须使用已校验的 Instant", "occurred_at")
        for name in ("dedupe_key", "session_id", "model_id", "display_name"):
            value = getattr(self, name)
            if value is None and name != "dedupe_key":
                continue
            if type(value) is not str or (not value.strip() and name != "display_name"):
                raise DomainError(STATISTICS_INVALID, "账本文本字段无效", name)
        if self.rollup is None:
            Tokens(input=self.input, cache_read=self.cache_read, output=self.output)
        if type(self.pico_usd) is not int or self.pico_usd < 0:
            raise DomainError(PICO_INVALID, "账本金额须为非负整数 pico", "pico_usd")
        if self.rollup is None and self.pico_usd >= 2**127:
            raise DomainError(COST_OVERFLOW, "账本金额超出范围", "pico_usd")
        if type(self.incomplete) is not bool or type(self.unpriced) is not bool:
            raise DomainError(STATISTICS_INVALID, "账本质量标记须为布尔值", "quality")
        object.__setattr__(self, "model_key", "" if self.model_id is None else model_key(self.model_id))

    @property
    def order_key(self) -> OrderKey:
        return self.harness.value, self.session_id or "", self.occurred_at, self.dedupe_key

    @property
    def latest_key(self) -> tuple[Instant, str, str, str]:
        return self.latest_at or self.occurred_at, self.harness.value, self.session_id or "", self.dedupe_key

    @property
    def name(self) -> str:
        if not self.model_key:
            return "模型未知"
        return self.model_id or "模型未知"

    @property
    def unprobed(self) -> bool:
        return self.harness == Harness.ZCODE and "grok" in self.model_key


@dataclass(frozen=True, slots=True)
class Metrics:
    record_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    new_input: int
    total_tokens: int
    cache_hit_rate: float | None
    cache_coverage_incomplete: bool
    pico_usd: str
    incomplete: bool
    unpriced: bool
    unknown_model_count: int
    missing_usage_count: int
    pending_valuation_count: int = 0
    estimate_pico: str | None = None
    correction_pico: str = "0"
    unallocated_correction_pico: str = "0"
    allocation_incomplete: bool = False

    def to_dict(self) -> dict[str, MetricValue]:
        return {
            "recordCount": self.record_count,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "newInput": self.new_input,
            "totalTokens": self.total_tokens,
            "cacheHitRate": self.cache_hit_rate,
            "cacheCoverageIncomplete": self.cache_coverage_incomplete,
            "picoUsd": self.pico_usd,
            "incomplete": self.incomplete,
            "unpriced": self.unpriced,
            "unknownModelCount": self.unknown_model_count,
            "missingUsageCount": self.missing_usage_count,
            "pendingValuationCount": self.pending_valuation_count,
            "estimatePico": self.estimate_pico if self.estimate_pico is not None else self.pico_usd,
            "correctionPico": self.correction_pico,
            "unallocatedCorrectionPico": self.unallocated_correction_pico,
            "allocationIncomplete": self.allocation_incomplete,
        }


@dataclass(frozen=True, slots=True)
class TrendPoint:
    bucket: str
    label: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_hit_rate: float | None
    cache_bust: bool
    cache_loss: bool
    pico_usd: str
    record_count: int
    context_compacted: bool = False

    def to_dict(self) -> dict[str, MetricValue]:
        return {
            "bucket": self.bucket,
            "label": self.label,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cache_read_tokens,
            "cacheHitRate": self.cache_hit_rate,
            "cacheBust": self.cache_bust,
            "cacheLoss": self.cache_loss,
            "picoUsd": self.pico_usd,
            "recordCount": self.record_count,
            "contextCompacted": self.context_compacted,
        }


@dataclass(frozen=True, slots=True)
class ModelSummary:
    model_key: str
    display_name: str
    metrics: Metrics

    def to_dict(self) -> dict[str, MetricValue]:
        return {"modelKey": self.model_key, "displayName": self.display_name, **self.metrics.to_dict()}


@dataclass(frozen=True, slots=True)
class HourlySummary:
    bucket: str
    label: str
    model_key: str | None
    display_name: str
    metrics: Metrics

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "bucket": self.bucket,
            "label": self.label,
            "displayName": self.display_name,
            "metrics": self.metrics.to_dict(),
        }
        if self.model_key is not None:
            result["modelKey"] = self.model_key
        return result


@dataclass(frozen=True, slots=True)
class HarnessSummary:
    harness: Harness
    display_name: str
    metrics: Metrics

    def to_dict(self) -> dict[str, MetricValue]:
        return {"harness": self.harness.value, "displayName": self.display_name, **self.metrics.to_dict()}


@dataclass(frozen=True, slots=True)
class Summary:
    start: Instant
    end: Instant
    offset_minutes: int
    grain: Grain
    metrics: Metrics
    trend: tuple[TrendPoint, ...]
    models: tuple[ModelSummary, ...]
    hourly: tuple[HourlySummary, ...]
    hourly_totals: tuple[HourlySummary, ...]
    harnesses: tuple[HarnessSummary, ...]
    history_notice: str | None = None
    cost_revision: int = 0
    unallocated_trend_correction_pico: str = "0"

    def to_dict(self) -> dict[str, object]:
        return {
            "start": format_instant(self.start),
            "end": format_instant(self.end),
            "offsetMinutes": self.offset_minutes,
            "grain": self.grain,
            "metrics": self.metrics.to_dict(),
            "trend": [item.to_dict() for item in self.trend],
            "models": [item.to_dict() for item in self.models],
            "hourly": [item.to_dict() for item in self.hourly],
            "hourlyTotals": [item.to_dict() for item in self.hourly_totals],
            "harnesses": [item.to_dict() for item in self.harnesses],
            "historyNotice": self.history_notice,
            "costRevision": self.cost_revision,
            "unallocatedTrendCorrectionPico": self.unallocated_trend_correction_pico,
        }
