"""Bounded, opt-in evidence chains contain numeric metadata, never source content."""

from typing import Annotated

from pydantic import Field

from htd.ipc.dto.base import InstantStr, PicoStr, SafeInt, StrictModel
from htd.ipc.dto.events import HarnessName


class EvidenceRequest(StrictModel):
    start: InstantStr
    end: InstantStr
    harness: HarnessName | None = None
    sessionRef: Annotated[str, Field(max_length=64)] | None = None
    vendorId: Annotated[str, Field(max_length=512)] | None = None
    localBatchIds: Annotated[list[str], Field(max_length=500)] = Field(default_factory=list[str])
    offset: Annotated[int, Field(ge=0, le=1000000)] = 0
    queryId: Annotated[str, Field(max_length=100)] | None = None


class RequestEvidence(StrictModel):
    harness: HarnessName
    requestKey: str
    sessionRef: str | None
    occurredAt: InstantStr
    receivedAt: InstantStr
    watermark: str
    state: str
    acceptedRevision: SafeInt
    valuedRevision: SafeInt
    projectedRevision: SafeInt
    publishedRevision: SafeInt
    groupId: str | None
    versionId: str | None
    priceRevisionId: str | None
    originalGroupId: str | None
    originalVersionId: str | None
    originalPico: PicoStr | None
    estimatePico: PicoStr | None
    voucherSequence: SafeInt | None
    localBatchId: str | None
    calibrationTargetId: str | None
    remoteIntervalIds: list[str]
    canRecalculate: bool


class EvidenceResponse(StrictModel):
    items: list[RequestEvidence]
    nextOffset: SafeInt | None
    sourceStates: dict[str, str]
    snapshot: dict[str, SafeInt]
    generatedAt: InstantStr
