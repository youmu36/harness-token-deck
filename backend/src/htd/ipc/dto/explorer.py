"""Scoped statistics contracts with exact counters and explicit historical capability."""

from typing import Annotated, Literal

from pydantic import Field

from htd.ipc.dto.base import InstantStr, PicoStr, SafeInt, StrictModel
from htd.ipc.dto.events import HarnessName
from htd.ipc.dto.statistics import HitRate

type Choices = Annotated[list[Annotated[str, Field(max_length=160)]], Field(max_length=50)]
type QualityReason = Literal["version", "cache", "incomplete", "unpriced"]


class ExploreRequest(StrictModel):
    start: InstantStr
    end: InstantStr
    rangeKind: Literal["recent", "custom", "month", "quarter", "year", "all"] | None = None
    sessionRefs: Choices = Field(default_factory=list[str])
    activityBatchIds: Choices = Field(default_factory=list[str])
    harnesses: Annotated[list[Literal["claude", "codex", "zcode", "dsh"]], Field(max_length=4)] = Field(
        default_factory=list[HarnessName]
    )
    vendorIds: Choices = Field(default_factory=list[str])
    groupIds: Choices = Field(default_factory=list[str])
    versionIds: Choices = Field(default_factory=list[str])
    modelKeys: Choices = Field(default_factory=list[str])
    grain: Literal["hour", "day", "month", "year"] = "day"
    offset: Annotated[int, Field(ge=0, le=5000)] = 0
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    qualityOffset: Annotated[int, Field(ge=0, le=30000)] = 0
    qualityStatus: Literal["all", "unacknowledged", "acknowledged"] = "all"
    qualityReasons: Annotated[list[QualityReason], Field(max_length=4)] = Field(
        default_factory=list[QualityReason]
    )
    costBasis: Literal["local", "remote"] = "local"
    sections: list[
        Literal["totals", "models", "trend", "breakdowns", "quality", "comparison", "accounts"]
    ] = Field(default_factory=lambda: ["totals", "models", "trend", "breakdowns", "quality"])
    compareDimension: Literal["vendor", "group", "version", "model"] = "vendor"
    compareIds: Annotated[list[Annotated[str, Field(max_length=160)]], Field(max_length=5)] = Field(
        default_factory=list[str]
    )
    accountOffset: Annotated[int, Field(ge=0, le=30000)] = 0
    accountCorrectionOffset: Annotated[int, Field(ge=0, le=30000)] = 0
    queryId: Annotated[str, Field(max_length=100)] | None = None


class QualityItem(StrictModel):
    id: str
    fingerprint: str
    date: str
    harness: str
    vendorId: str
    groupId: str
    versionId: str
    modelKey: str
    displayName: str
    reasons: list[str]
    recordCount: PicoStr
    acknowledged: bool
    costDifferencePico: PicoStr | None = None
    countAccuracy: Literal["exact", "at_least"] = "exact"
    reasonCounts: dict[str, PicoStr] = Field(default_factory=dict[str, str])


class QualityAcknowledgement(StrictModel):
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    revision: SafeInt | None = None


class AcknowledgeQualityRequest(StrictModel):
    items: Annotated[list[QualityAcknowledgement], Field(max_length=100)]


class AcknowledgeQualityResponse(StrictModel):
    acknowledged: SafeInt
    conflicts: SafeInt = 0


class ExploreMetrics(StrictModel):
    recordCount: PicoStr
    inputTokens: PicoStr
    cacheReadTokens: PicoStr
    outputTokens: PicoStr
    cacheWriteTokens: PicoStr
    newInput: PicoStr
    totalTokens: PicoStr
    picoUsd: PicoStr
    cacheHitRate: HitRate | None
    incomplete: bool
    unpriced: bool
    cacheCoverageIncomplete: bool
    tokensPerDollar: str | None
    perDollar: dict[str, str | None]
    parts: dict[str, PicoStr]
    eligibleCostPico: PicoStr
    eligibleTokens: PicoStr
    estimatePico: PicoStr | None = None
    correctionPico: PicoStr = "0"
    unallocatedCorrectionPico: PicoStr = "0"
    allocationIncomplete: bool = False
    ratioUnavailable: bool = False
    componentCostEstimated: bool = False
    pendingValuationCount: PicoStr = "0"


class ExploreModel(ExploreMetrics):
    modelKey: str
    displayName: str


class ExplorePoint(ExploreMetrics):
    bucket: str
    label: str


class ExploreBreakdown(ExploreMetrics):
    harness: str
    vendorId: str
    groupId: str
    versionId: str
    modelKey: str
    displayName: str


class ExploreComparison(StrictModel):
    id: str
    totals: ExploreMetrics
    trend: list[ExplorePoint]


class AnalysisCoverage(StrictModel):
    code: str
    message: str


class AccountInterval(StrictModel):
    id: str
    vendorId: str
    start: InstantStr
    end: InstantStr
    method: str
    originalCostPico: PicoStr
    costPico: PicoStr
    bookedEstimatePico: PicoStr
    currentEstimatePico: PicoStr
    differencePico: PicoStr
    status: Literal["matched", "review", "estimated", "collecting", "coverage_incomplete"]
    coverageComplete: bool


class CancelAnalysisRequest(StrictModel):
    queryId: Annotated[str, Field(min_length=1, max_length=100)]


class CancelAnalysisResponse(StrictModel):
    cancelled: bool


class AccountCorrection(StrictModel):
    id: str
    vendorId: str
    occurredAt: InstantStr
    kind: str
    costPico: PicoStr
    note: str


class ExploreOptionsRequest(StrictModel):
    modelSearch: Annotated[str, Field(max_length=160)] = ""
    modelOffset: Annotated[int, Field(ge=0, le=30000)] = 0
    modelLimit: Annotated[int, Field(ge=1, le=500)] = 100
    modelKeys: Choices = Field(default_factory=list[str])
    includeCatalog: bool = True


class QueryPlanDescription(StrictModel):
    layer: Literal["recent", "day_month", "mixed"]
    recentBoundary: InstantStr
    sessionScoped: bool
    rangeKind: Literal["recent", "custom", "month", "quarter", "year", "all"] | None


class ExploreHarness(ExploreMetrics):
    harness: HarnessName
    displayName: str


class ExploreResponse(StrictModel):
    start: InstantStr
    end: InstantStr
    generatedAt: InstantStr
    businessTimezone: str
    grain: Literal["hour", "day", "month", "year"]
    totals: ExploreMetrics
    models: list[ExploreModel]
    trend: list[ExplorePoint]
    breakdowns: list[ExploreBreakdown]
    totalModels: SafeInt
    nextOffset: SafeInt | None
    missingVersionRecords: PicoStr
    notices: list[str]
    revision: SafeInt
    quality: list[QualityItem]
    qualityTotal: SafeInt
    qualityUnacknowledged: SafeInt
    qualityNextOffset: SafeInt | None
    costBasis: Literal["local", "remote"] = "local"
    snapshot: dict[str, SafeInt] = Field(default_factory=dict[str, int])
    coverage: list[AnalysisCoverage] = Field(default_factory=list[AnalysisCoverage])
    comparisons: list[ExploreComparison] = Field(default_factory=list[ExploreComparison])
    accounts: list[AccountInterval] = Field(default_factory=list[AccountInterval])
    accountTotal: SafeInt = 0
    accountNextOffset: SafeInt | None = None
    remoteCostPico: PicoStr = "0"
    intervalCostPico: PicoStr = "0"
    unallocatedCostPico: PicoStr = "0"
    accountCorrections: list[AccountCorrection] = Field(default_factory=list[AccountCorrection])
    accountCorrectionTotal: SafeInt = 0
    accountCorrectionNextOffset: SafeInt | None = None
    plan: QueryPlanDescription | None = None
    unallocatedTrendCorrectionPico: PicoStr = "0"
    harnesses: list[ExploreHarness] = Field(default_factory=list[ExploreHarness])


class ExploreOption(StrictModel):
    id: str
    name: str
    parentId: str | None = None


class ExploreOptions(StrictModel):
    vendors: list[ExploreOption]
    groups: list[ExploreOption]
    versions: list[ExploreOption]
    models: list[ExploreOption]
    totalModels: SafeInt = 0
    modelNextOffset: SafeInt | None = None
    groupVendors: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    selectedModels: list[ExploreOption] = Field(default_factory=list[ExploreOption])


class OutputReferencesRequest(StrictModel):
    queryId: Annotated[str, Field(max_length=100)] | None = None
    start: InstantStr | None = None
    end: InstantStr | None = None
    harnesses: Annotated[list[Literal["claude", "codex", "zcode", "dsh"]], Field(max_length=4)] = Field(
        default_factory=list[HarnessName]
    )
    modelKeys: Annotated[list[str], Field(max_length=5)] = Field(default_factory=list[str])
    vendorIds: Choices = Field(default_factory=list[str])
    groupIds: Choices = Field(default_factory=list[str])
    versionIds: Choices = Field(default_factory=list[str])


class OutputDaily(StrictModel):
    day: str
    modelKey: str
    displayName: str
    tokensPerSecond: str | None
    samples: SafeInt


class OutputReferencesResponse(StrictModel):
    today: str
    references: list[OutputDaily]
    generatedAt: InstantStr
    start: InstantStr | None = None
    end: InstantStr | None = None
    coverage: list[AnalysisCoverage] = Field(default_factory=list[AnalysisCoverage])
    todayReferences: list[OutputDaily] | None = None
    businessTimezone: str = "Asia/Shanghai"
