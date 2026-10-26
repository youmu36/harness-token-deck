"""Explicit local member ranges and independent account evidence."""

from typing import Annotated, Literal

from pydantic import Field

from htd.ipc.dto.base import InstantStr, PicoStr, SafeInt, StrictModel, WriteRequest
from htd.ipc.dto.events import HarnessName
from htd.ipc.dto.maintenance import DateStr, Fingerprint


class ReconciliationQuery(StrictModel):
    vendorId: Annotated[str, Field(max_length=512)]
    start: InstantStr
    end: InstantStr
    localBatchIds: Annotated[list[str], Field(max_length=500)] = Field(default_factory=list[str])
    intervalIds: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list[str])


class BatchQuery(ReconciliationQuery):
    offset: Annotated[int, Field(ge=0, le=1000000)] = 0
    remoteOffset: Annotated[int, Field(ge=0, le=1000000)] = 0


class ReconciliationInterval(StrictModel):
    id: str
    start: InstantStr
    end: InstantStr
    revision: SafeInt
    method: str
    unitVersion: str
    consumptionPico: PicoStr = "0"
    estimatePico: PicoStr = "0"
    localStart: InstantStr | None = None
    localEnd: InstantStr | None = None
    sessionCount: SafeInt = 0


class LocalBatch(StrictModel):
    id: str
    sessionRef: str | None
    start: InstantStr
    end: InstantStr
    revision: SafeInt
    publishedRevision: SafeInt
    state: str


class BatchResponse(StrictModel):
    batches: list[LocalBatch]
    remoteIntervals: list[ReconciliationInterval]
    nextOffset: SafeInt | None
    remoteNextOffset: SafeInt | None
    generatedAt: InstantStr


class ReconciliationPreview(StrictModel):
    vendorId: str
    start: InstantStr
    end: InstantStr
    businessTimezone: str
    memberDays: list[DateStr]
    memberCount: PicoStr
    memberDigest: str
    batchIds: list[str]
    originalEstimatePico: PicoStr
    currentEstimatePico: PicoStr
    currentEffectivePico: PicoStr
    existingCorrectionPico: PicoStr
    pendingCount: PicoStr
    incompleteCount: PicoStr
    harnesses: list[str]
    modelKeys: list[str]
    groupIds: list[str]
    localComplete: bool
    quiet: bool
    coverageAligned: bool
    omittedMemberCount: PicoStr
    auxiliaryIncluded: bool
    externalUsagePossible: bool
    legacyOverrides: SafeInt
    legacyFlowIds: list[str]
    legacyAllocationIncomplete: bool
    unallocatedCorrectionPico: PicoStr
    allocationIncomplete: bool
    independentCorrectionPico: PicoStr
    fingerprint: str
    snapshot: dict[str, SafeInt]
    notices: list[str]
    remoteTargetPico: PicoStr | None
    remoteUsedDeltaPico: PicoStr | None
    bookedEstimatePico: PicoStr
    reliability: Literal["missing", "invalid", "reliable_usage", "balance_estimate"]
    remoteIntervals: list[ReconciliationInterval]


class CalibrationDayTarget(StrictModel):
    day: DateStr
    targetPico: PicoStr


class CalibrationAllocation(CalibrationDayTarget):
    harnesses: Annotated[list[HarnessName], Field(max_length=4)] = Field(default_factory=list[HarnessName])
    sessionRefs: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list[str])
    modelKeys: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list[str])
    groupIds: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list[str])
    versionIds: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list[str])
    confirmModelCoverage: bool = False


class CalibrationRequest(ReconciliationQuery):
    action: Literal["apply", "revoke"] = "apply"
    targetId: str | None = None
    targetPico: PicoStr = "0"
    basis: Literal["manual", "remote"] = "manual"
    confirmLocalScope: bool = False
    acknowledgeIncomplete: bool = False
    confirmReplacement: bool = False
    replaceTargetIds: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list[str])
    dayTargets: Annotated[list[CalibrationDayTarget], Field(max_length=366)] = Field(
        default_factory=list[CalibrationDayTarget]
    )
    allocations: Annotated[list[CalibrationAllocation], Field(max_length=100)] = Field(
        default_factory=list[CalibrationAllocation]
    )
    note: Annotated[str, Field(max_length=500)] = ""
    allocationMode: Literal["explicit", "unallocated", "proportional", "weighted"] = "explicit"
    modelWeights: Annotated[
        dict[str, Annotated[str, Field(pattern=r"^(?:0(?:\.[0-9]{1,4})?|1(?:\.0{1,4})?)$")]],
        Field(max_length=100),
    ] = Field(default_factory=dict)


class CommitCalibrationRequest(CalibrationRequest, WriteRequest):
    fingerprint: Fingerprint


class CalibrationDayPreview(CalibrationDayTarget):
    estimatePico: PicoStr
    currentEffectivePico: PicoStr
    deltaPico: PicoStr
    unallocatedPico: PicoStr


class ReplacedTarget(StrictModel):
    id: str
    targetPico: PicoStr
    revision: SafeInt


class CalibrationModelPreview(CalibrationDayTarget):
    modelKey: str
    estimatePico: PicoStr
    deltaPico: PicoStr


class CalibrationPreview(ReconciliationPreview):
    action: Literal["apply", "revoke"]
    targetPico: PicoStr
    deltaPico: PicoStr
    days: list[CalibrationDayPreview]
    modelAllocationCount: SafeInt
    replacedTargets: list[ReplacedTarget]
    modelTargets: list[CalibrationModelPreview] = Field(default_factory=list[CalibrationModelPreview])


class CalibrationResponse(CalibrationPreview):
    targetId: str
    revision: SafeInt


class CalibrationTargetsRequest(StrictModel):
    vendorId: str
    state: Literal["all", "active", "replaced", "revoked"] = "all"
    cursor: str | None = None


class CalibrationTarget(ReplacedTarget):
    vendorId: str
    state: Literal["active", "replaced", "revoked"]
    estimatePico: PicoStr
    deltaPico: PicoStr
    scope: ReconciliationQuery
    createdAt: InstantStr
    basis: Literal["manual", "remote", "legacy_flow"]
    legacyFlowId: str | None
    allocations: list[CalibrationAllocation]
    dayTargets: list[CalibrationDayTarget]


class CalibrationTargetsResponse(StrictModel):
    targets: list[CalibrationTarget]
    nextCursor: str | None
    revision: SafeInt
