"""Scoped statistics contracts with exact counters and explicit historical capability."""

from typing import Annotated, Literal

from pydantic import Field

from htd.ipc.dto.base import InstantStr, PicoStr, SafeInt, StrictModel
from htd.ipc.dto.events import HarnessName
from htd.ipc.dto.statistics import HitRate

type Choices = Annotated[list[Annotated[str, Field(max_length=160)]], Field(max_length=50)]


class ExploreRequest(StrictModel):
    start: InstantStr
    end: InstantStr
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


class QualityAcknowledgement(StrictModel):
    id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AcknowledgeQualityRequest(StrictModel):
    items: Annotated[list[QualityAcknowledgement], Field(max_length=100)]


class AcknowledgeQualityResponse(StrictModel):
    acknowledged: SafeInt


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


class ExploreOption(StrictModel):
    id: str
    name: str
    parentId: str | None = None


class ExploreOptions(StrictModel):
    vendors: list[ExploreOption]
    groups: list[ExploreOption]
    versions: list[ExploreOption]
    models: list[ExploreOption]


class OutputReferencesRequest(StrictModel):
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
