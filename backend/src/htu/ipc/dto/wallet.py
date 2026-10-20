"""Strict USD requests and lossless pico wallet snapshots."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from htu.ipc.dto.base import InstantStr, PicoStr, SafeInt, StrictModel, WriteRequest
from htu.ipc.dto.pricing import Identifier, Name

type AmountStr = Annotated[str, StringConstraints(pattern=r"^-?[0-9]{1,9}(?:\.[0-9]{1,6})?$")]


class AccountSummary(StrictModel):
    R: PicoStr
    C: PicoStr
    A: PicoStr
    B: PicoStr
    localEstimatePico: PicoStr
    observedAt: InstantStr | None
    coverageStart: InstantStr | None
    methods: list[str]
    remote: bool
    revision: SafeInt


class WalletTotals(StrictModel):
    R: PicoStr
    C: PicoStr
    A: PicoStr
    B: PicoStr
    pastTopUpPico: PicoStr
    pastConsumptionPico: PicoStr
    postedR: PicoStr | None = None
    postedC: PicoStr | None = None
    pendingTopUpPico: PicoStr = "0"
    pendingConsumptionPico: PicoStr = "0"


class Vendor(StrictModel):
    id: Identifier
    name: Name
    url: str | None = None
    status: Literal["active", "archived"]
    createdAt: InstantStr
    R: PicoStr
    pastTopUpPico: PicoStr = "0"
    pastConsumptionPico: PicoStr = "0"
    C: PicoStr
    A: PicoStr
    B: PicoStr
    lastCalibratedAt: InstantStr | None
    linkedGroups: list[Identifier]
    coverageIncomplete: bool
    unpricedCount: SafeInt
    incompleteCount: SafeInt
    historyCoverageStart: InstantStr | None
    walletRevision: SafeInt
    account: AccountSummary | None = None
    localWallet: WalletTotals | None = None
    remoteWallet: WalletTotals | None = None
    walletVisibility: Literal["both", "local", "remote"] = "both"


class WalletListRequest(StrictModel):
    includeArchived: bool = False


class WalletListResponse(StrictModel):
    vendors: list[Vendor]
    revision: SafeInt


class EntriesRequest(StrictModel):
    vendorId: Identifier
    cursor: Annotated[int, Field(ge=1, le=2**53 - 1)] | None = None
    limit: Annotated[int, Field(ge=1, le=500)] = 100
    kind: Literal["all", "top_up", "adjustment", "consumption_daily"] = "all"
    offsetMinutes: Annotated[int, Field(ge=-840, le=840)] = 0


class Calibration(StrictModel):
    targetPico: PicoStr
    observedBalancePico: PicoStr
    watermarkSeq: SafeInt


class EntryNoteDetails(StrictModel):
    calibrationCount: SafeInt
    calibrationPico: PicoStr


class Entry(StrictModel):
    id: Identifier | None
    kind: Literal["top_up", "adjustment", "consumption_daily"]
    seq: SafeInt
    amountPico: PicoStr
    occurredAt: InstantStr
    count: SafeInt
    note: str
    noteDetails: EntryNoteDetails | None = None
    calibration: Calibration | None


class EntriesResponse(StrictModel):
    entries: list[Entry]
    nextCursor: SafeInt | None
    revision: SafeInt


class CreateVendorRequest(WriteRequest):
    name: Name
    url: Annotated[str, StringConstraints(max_length=2048)] | None = None
    initialBalance: AmountStr | Literal[""] | None = None
    pastTopUp: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,9}(?:\.[0-9]{1,6})?$")] = "0"
    pastConsumption: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,9}(?:\.[0-9]{1,6})?$")] = "0"


class ScannedProviderName(StrictModel):
    id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    name: Name


class CreateScannedVendorsRequest(WriteRequest):
    taskId: Identifier
    providers: Annotated[list[ScannedProviderName], Field(min_length=1, max_length=200)]


class VendorRequest(WriteRequest):
    vendorId: Identifier


class WalletVisibilityRequest(VendorRequest):
    mode: Literal["both", "local", "remote"]


class RenameVendorRequest(VendorRequest):
    name: Name
    pastTopUp: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,9}(?:\.[0-9]{1,6})?$")] | None = None
    pastConsumption: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,9}(?:\.[0-9]{1,6})?$")] | None = None
    url: Annotated[str, StringConstraints(max_length=2048)] | None = None


class TopUpRequest(VendorRequest):
    amount: AmountStr
    walletScope: Literal["legacy", "local", "remote"] = "legacy"


class CalibrateRequest(VendorRequest):
    target: AmountStr
    expectedWalletRevision: SafeInt
    remoteFetchedAt: InstantStr | None = None
    walletScope: Literal["legacy", "remote"] = "legacy"


class AccountEntriesRequest(StrictModel):
    vendorId: Identifier
    cursor: str | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 50
    scope: Literal["legacy", "local", "remote"] = "legacy"
    kind: Literal["all", "top_up", "consumption", "adjustment"] = "all"
    startDay: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None = None
    endDay: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None = None


class AccountEntry(StrictModel):
    id: Identifier
    kind: str
    occurredAt: InstantStr
    R: PicoStr
    C: PicoStr
    A: PicoStr
    note: str
    originalId: Identifier | None
    intervalId: Identifier | None
    availablePico: PicoStr
    fromAt: InstantStr | None = None
    toAt: InstantStr | None = None
    postedAt: InstantStr | None = None
    beforeR: PicoStr | None = None
    beforeC: PicoStr | None = None
    beforeB: PicoStr | None = None
    afterR: PicoStr | None = None
    afterC: PicoStr | None = None
    afterB: PicoStr | None = None
    entryRevision: SafeInt = 1
    manuallyCalibrated: bool = False
    estimatedConsumptionPico: PicoStr | None = None
    noticeId: Identifier | None = None


class CorrectEntryRequest(VendorRequest):
    entryId: Identifier
    scope: Literal["local", "remote"]
    R: Annotated[PicoStr, StringConstraints(max_length=40)]
    C: Annotated[PicoStr, StringConstraints(max_length=40)]
    expectedEntryRevision: SafeInt
    note: Annotated[str, StringConstraints(max_length=500)] = ""


class DismissWalletNoticeRequest(VendorRequest):
    entryId: Identifier
    noticeId: Identifier


class AccountEntriesResponse(StrictModel):
    entries: list[AccountEntry]
    nextCursor: str | None
    revision: SafeInt


class ReclassifyRequest(VendorRequest):
    entryId: Identifier
    amount: AmountStr | None = None
    amountPico: Annotated[PicoStr, StringConstraints(max_length=40)] | None = None
    targetKind: Literal["top_up", "consumption", "adjustment"]
    expectedAccountRevision: SafeInt
    note: Annotated[str, StringConstraints(max_length=500)] = ""
    scope: Literal["legacy", "local", "remote"] = "legacy"


class SetLocalTotalsRequest(VendorRequest):
    R: Annotated[PicoStr, StringConstraints(max_length=40)] | None = None
    C: Annotated[PicoStr, StringConstraints(max_length=40)] | None = None
    B: Annotated[PicoStr, StringConstraints(max_length=40)] | None = None
    pastTopUpPico: Annotated[PicoStr, StringConstraints(max_length=40)] | None = None
    pastConsumptionPico: Annotated[PicoStr, StringConstraints(max_length=40)] | None = None
    expectedWalletRevision: SafeInt


class DailyBookRequest(StrictModel):
    vendorId: Identifier
    scope: Literal["local", "remote"]
    month: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")]


class DailyBookRow(StrictModel):
    day: str
    R: PicoStr
    C: PicoStr
    A: PicoStr


class DailyBookResponse(StrictModel):
    days: list[DailyBookRow]
    R: PicoStr
    C: PicoStr
    A: PicoStr
    pending: bool
    notice: str
    revision: SafeInt
    providerTodayPico: PicoStr | None = None
    providerTotalPico: PicoStr | None = None
    providerObservedAt: InstantStr | None = None


class CorrectConsumptionRequest(VendorRequest):
    target: AmountStr | None = None
    targetPico: Annotated[PicoStr, StringConstraints(max_length=40)] | None = None
    expectedAccountRevision: SafeInt
    note: Annotated[str, StringConstraints(max_length=500)] = ""
    scope: Literal["legacy", "remote"] = "legacy"


class LinkGroupsRequest(VendorRequest):
    groupIds: Annotated[list[Identifier], Field(max_length=100)]
    expectedRevision: SafeInt


class WalletChangeResponse(StrictModel):
    vendor: Vendor | None
    vendorId: Identifier
    action: Literal["archive", "delete"] | None
    changed: bool
    revision: SafeInt
    pricingRevision: SafeInt


class IntervalRequest(StrictModel):
    vendorId: Identifier
    intervalId: Identifier


class IntervalPreview(StrictModel):
    intervalId: Identifier
    fromAt: InstantStr
    toAt: InstantStr
    beforeR: PicoStr
    beforeC: PicoStr
    beforeA: PicoStr
    afterR: PicoStr
    afterC: PicoStr
    afterA: PicoStr
    localEstimatePico: PicoStr
    method: str
    intervalRevision: SafeInt
    accountRevision: SafeInt
    fingerprint: str


class ReconcileIntervalRequest(VendorRequest):
    intervalId: Identifier
    fingerprint: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    expectedAccountRevision: SafeInt
