"""Statistics output keeps financial values as exact strings and counts as safe integers."""

from typing import Annotated, Literal

from pydantic import Field

from htu.domain.statistics.aggregator import Section
from htu.domain.statistics.analysis import AnalysisPeriod
from htu.domain.statistics.window import Grain, MonitorMode
from htu.ipc.dto.base import InstantStr, PicoStr, SafeInt, StrictModel
from htu.ipc.dto.events import HarnessName

type OffsetMinutes = Annotated[int, Field(strict=True, ge=-840, le=840)]
type HitRate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
type SignedSafeInt = Annotated[int, Field(strict=True, ge=-(2**53 - 1), le=2**53 - 1)]


class SummaryRequest(StrictModel):
    harness: Literal["claude", "codex", "zcode", "dsh", "all"]
    start: InstantStr
    end: InstantStr
    offsetMinutes: OffsetMinutes
    grain: Grain | None = None
    sections: Annotated[list[Section], Field(max_length=5)] | None = None
    modelKey: Annotated[str, Field(max_length=160)] | None = None


class CacheMonitorRequest(StrictModel):
    harness: HarnessName
    mode: MonitorMode
    offsetMinutes: OffsetMinutes


class MetricsData(StrictModel):
    recordCount: SafeInt
    inputTokens: SafeInt
    outputTokens: SafeInt
    cacheReadTokens: SafeInt
    newInput: SignedSafeInt
    totalTokens: SafeInt
    cacheHitRate: HitRate | None
    cacheCoverageIncomplete: bool
    picoUsd: PicoStr
    incomplete: bool
    unpriced: bool
    unknownModelCount: SafeInt
    missingUsageCount: SafeInt


class TrendData(StrictModel):
    bucket: str
    label: str
    inputTokens: SafeInt
    outputTokens: SafeInt
    cacheReadTokens: SafeInt
    cacheHitRate: HitRate | None
    cacheBust: bool
    cacheLoss: bool
    contextCompacted: bool = False
    picoUsd: PicoStr
    recordCount: SafeInt


class ModelData(MetricsData):
    modelKey: str
    displayName: str


class HarnessData(MetricsData):
    harness: HarnessName
    displayName: str


class HourlyTotalData(StrictModel):
    bucket: str
    label: str
    displayName: str
    metrics: MetricsData


class HourlyData(HourlyTotalData):
    modelKey: str


class SummaryResponse(StrictModel):
    start: InstantStr
    end: InstantStr
    offsetMinutes: OffsetMinutes
    grain: Grain
    metrics: MetricsData
    trend: list[TrendData]
    models: list[ModelData]
    hourly: list[HourlyData]
    hourlyTotals: list[HourlyTotalData]
    harnesses: list[HarnessData]
    historyNotice: str | None
    revision: SafeInt


class OutputSpeed(StrictModel):
    modelKey: str
    displayName: str
    tokensPerSecond: str | None
    outputTokens: PicoStr
    activeMinutes: SafeInt
    lastOutputAt: InstantStr | None
    frozen: bool
    basis: Literal["generation", "request_estimate", "minute_estimate"] = "minute_estimate"


class SessionHitRate(StrictModel):
    sessionKey: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
    displayName: Annotated[str, Field(max_length=512)]
    startedAt: InstantStr
    lastActivityAt: InstantStr
    cacheHitRate: HitRate | None
    cacheCoverageIncomplete: bool
    frozen: bool


class CacheMonitorResponse(StrictModel):
    start: InstantStr
    end: InstantStr
    offsetMinutes: OffsetMinutes
    grain: MonitorMode
    windowKey: str
    nextRefreshAt: InstantStr
    trend: list[TrendData]
    revision: SafeInt
    outputSpeeds: list[OutputSpeed] = Field(default_factory=list[OutputSpeed])
    sessionHitRates: list[SessionHitRate] = Field(default_factory=list[SessionHitRate], max_length=100)


class AuxiliaryRequest(StrictModel):
    harness: Literal["claude", "codex", "zcode", "dsh", "all"]
    start: InstantStr
    end: InstantStr


class AuxiliaryModel(StrictModel):
    modelId: str | None
    purpose: str
    count: SafeInt
    costPico: PicoStr
    tokens: None = None


class AuxiliaryResponse(StrictModel):
    costPico: PicoStr
    models: list[AuxiliaryModel]
    revision: SafeInt


class LongTermResponse(StrictModel):
    totals: dict[str, PicoStr]
    activeMinutes: PicoStr
    coveredDays: SafeInt
    businessTimezone: str
    includesWholeBoundaryDays: bool
    missingCapabilities: list[str]
    basis: str


class AnalysisRequest(StrictModel):
    harness: Literal["claude", "codex", "zcode", "dsh", "all"] = "all"
    period: AnalysisPeriod = "month"
    year: Annotated[int, Field(strict=True, ge=1971, le=9997)] | None = None
    endYear: Annotated[int, Field(strict=True, ge=1971, le=9997)] | None = None
    quarter: Annotated[int, Field(strict=True, ge=1, le=4)] | None = None
    start: InstantStr | None = None
    end: InstantStr | None = None
    modelKey: Annotated[str, Field(max_length=160)] | None = None
    offset: Annotated[int, Field(strict=True, ge=0, le=5000)] = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50


class AnalysisMetrics(StrictModel):
    recordCount: PicoStr
    inputTokens: PicoStr
    outputTokens: PicoStr
    cacheReadTokens: PicoStr
    totalTokens: PicoStr
    picoUsd: PicoStr
    cacheHitRate: HitRate | None
    cacheCoverageIncomplete: bool
    incomplete: bool
    unpriced: bool
    unknownModelCount: PicoStr
    missingUsageCount: PicoStr
    activeMinutes: PicoStr
    tokensPerMinute: str | None
    tokensPerSecond: str | None
    coveredDays: SafeInt
    coverageStart: InstantStr | None
    coverageEnd: InstantStr | None
    lastOutputAt: InstantStr | None
    missingCapabilities: list[str]


class AnalysisPoint(AnalysisMetrics):
    bucket: str
    label: str


class AnalysisModel(AnalysisMetrics):
    modelKey: str
    displayName: str


class AnalysisHarness(AnalysisMetrics):
    harness: HarnessName


class AnalysisComparison(StrictModel):
    start: InstantStr
    end: InstantStr
    totals: AnalysisMetrics
    changes: dict[str, str | None]
    includesWholeBoundaryDays: bool


class OutputReference(StrictModel):
    day: str
    modelKey: str
    displayName: str
    tokensPerSecond: str | None
    samples: SafeInt


class AnalysisResponse(StrictModel):
    start: InstantStr
    end: InstantStr
    businessTimezone: str
    grain: Literal["day", "month", "year"]
    totals: AnalysisMetrics
    comparison: AnalysisComparison | None
    trend: list[AnalysisPoint]
    models: list[AnalysisModel]
    harnesses: list[AnalysisHarness]
    totalModels: SafeInt
    nextOffset: SafeInt | None
    includesWholeBoundaryDays: bool
    generatedAt: InstantStr
    revision: SafeInt
    basis: str
    comparisonNotice: str
    outputReferences: list[OutputReference] = Field(default_factory=list[OutputReference])
