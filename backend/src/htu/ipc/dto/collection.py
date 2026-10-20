"""Strict source configuration and collection command contracts."""

import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, StringConstraints

from htu.application.ports import ScanTrigger, SchedulerState, SourceStatus
from htu.ipc.dto.base import InstantStr, SafeInt, StrictModel, WriteRequest
from htu.ipc.dto.events import HarnessName


def absolute_path(value: str) -> str:
    if not Path(value).is_absolute() or "\x00" in value:
        raise ValueError("来源路径须为绝对路径")
    return os.path.normcase(os.path.abspath(value))


type SourcePath = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=32767), AfterValidator(absolute_path)
]


class UnmeteredRequestData(StrictModel):
    date: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    kind: Literal["title_generation"]
    count: SafeInt


class SourceData(StrictModel):
    harness: HarnessName
    path: str
    startedAt: InstantStr | None
    lastScanAt: InstantStr | None
    status: SourceStatus
    diagnostics: list[str]
    unmeteredRequests: list[UnmeteredRequestData] | None
    identityCount: SafeInt | None
    processedCount: SafeInt | None
    inboxCount: SafeInt | None
    ledgerCount: SafeInt | None


class SourcesResponse(StrictModel):
    sources: list[SourceData]
    revision: SafeInt
    paused: bool


class SourcePathRequest(StrictModel):
    harness: HarnessName
    path: SourcePath


class SetPathRequest(SourcePathRequest, WriteRequest):
    expectedRevision: SafeInt


class AutoDetectRequest(WriteRequest):
    expectedRevision: SafeInt


class ValidatePathResponse(StrictModel):
    harness: HarnessName
    path: str
    available: bool
    diagnostics: list[str]


class CollectionStatusRequest(StrictModel):
    details: bool = True


class CollectionStatusResponse(SourcesResponse):
    state: SchedulerState
    nextPeriodicAt: InstantStr | None
    lastCompletedAt: InstantStr | None
    trigger: ScanTrigger | None
    lastError: str | None


class ScanNowResponse(StrictModel):
    completedAt: InstantStr
    nextPeriodicAt: InstantStr


class SetPausedRequest(WriteRequest):
    paused: bool


class SetPausedResponse(StrictModel):
    paused: bool
    revision: SafeInt
