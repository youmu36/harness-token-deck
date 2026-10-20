"""Strict manual maintenance, repricing and history request/response contracts."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StringConstraints

from htu.ipc.dto.base import InstantStr, PicoStr, SafeInt, StrictModel, WriteRequest
from htu.ipc.dto.tasks import Identifier

type Retention = Literal[90, 180, 360, "forever"]
type HarnessName = Literal["claude", "codex", "zcode", "dsh"]
type Fingerprint = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class StorageFootprint(StrictModel):
    mainFileBytes: SafeInt
    walBytes: SafeInt
    shmBytes: SafeInt
    allocatedBytes: SafeInt
    freePageBytes: SafeInt
    usedPageBytes: SafeInt
    backupBytes: SafeInt
    migrationBytes: SafeInt
    sampledAt: InstantStr


class MaintenanceStatus(StrictModel):
    paused: bool
    retention: Retention
    lastRetentionAt: InstantStr | None
    lastVacuumAt: InstantStr | None
    lastLogRotationAt: InstantStr | None
    pendingExpired: SafeInt
    logCount: SafeInt
    databaseBytes: SafeInt
    storage: StorageFootprint | None = None
    appVersion: str
    dataDir: str


class RetentionRequest(StrictModel):
    retention: Retention


class SetRetentionRequest(WriteRequest):
    retention: Retention
    expectedRevision: SafeInt


class RetentionPreview(StrictModel):
    retention: Retention
    cutoff: InstantStr | None
    ledgerCount: SafeInt
    earliest: InstantStr | None
    latest: InstantStr | None
    inboxCount: SafeInt
    identityCount: SafeInt


class RetentionResponse(StrictModel):
    retention: Retention
    revision: SafeInt
    cutoff: InstantStr | None
    deletedLedger: SafeInt
    expiredInbox: SafeInt
    complete: bool
    errorCode: str | None


class RecalcRequest(StrictModel):
    harness: HarnessName
    start: InstantStr
    end: InstantStr
    groupId: Identifier
    versionId: Identifier


class CommitRecalcRequest(RecalcRequest, WriteRequest):
    fingerprint: Fingerprint


class VendorDelta(StrictModel):
    vendorId: Identifier
    name: str
    consumptionDelta: PicoStr
    balanceDelta: PicoStr


class RecalcPreview(StrictModel):
    recordCount: SafeInt
    oldTotal: PicoStr
    newTotal: PicoStr
    unpricedCount: SafeInt
    incompleteCount: SafeInt
    groupName: str
    versionDisplayName: str
    fingerprint: Fingerprint
    vendors: list[VendorDelta]


class RecalcResponse(RecalcPreview):
    batchId: Identifier
    revision: SafeInt


class HistoryScanRequest(WriteRequest):
    harness: Literal["claude", "codex", "zcode", "dsh", "all"] = "all"
    days: Literal[7, 14] = 14
    offsetMinutes: Annotated[int, Field(ge=-840, le=840)]


def valid_date(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("无效日期")
    return value


type DateStr = Annotated[
    str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(valid_date)
]


class HistoryImportRequest(WriteRequest):
    scanId: Identifier
    dates: Annotated[list[DateStr], Field(min_length=1, max_length=14)]


class HistoryImportResponse(StrictModel):
    inserted: SafeInt
    duplicates: SafeInt
    tooOld: SafeInt


class HistoryDay(StrictModel):
    date: DateStr
    recordCount: SafeInt
    newCount: SafeInt


class HistoryScanResponse(StrictModel):
    scanId: Identifier
    start: InstantStr
    end: InstantStr
    dates: list[HistoryDay]
    models: list[str]
    diagnostics: list[str]
