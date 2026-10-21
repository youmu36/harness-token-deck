"""Storage-independent collection records, reader ports and transaction contracts."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date as calendar_date
from typing import Literal, Protocol

from htd.application.ports.management import (
    CommandStore as CommandStore,
)
from htd.application.ports.management import (
    TaskContext as TaskContext,
)
from htd.application.ports.management import (
    TaskControl as TaskControl,
)
from htd.application.ports.management import (
    TaskDone as TaskDone,
)
from htd.application.ports.management import (
    TaskProgress as TaskProgress,
)
from htd.application.ports.management import (
    VendorSnapshot as VendorSnapshot,
)
from htd.application.ports.management import (
    WalletCommandStore as WalletCommandStore,
)
from htd.domain.billing import CostEstimate
from htd.domain.common.time import Instant
from htd.domain.pricing.catalog import Catalog
from htd.domain.pricing.model_pool import ModelPool
from htd.domain.statistics.analysis import AnalysisQuery
from htd.domain.statistics.model import LedgerRow
from htd.domain.usage.model import Harness, UsageEvent
from htd.domain.wallet.model import VendorLink

type SourceStatus = Literal["pending", "unavailable", "baselining", "watching", "degraded"]
type ScanTrigger = Literal["startup", "periodic", "notification", "manual", "resume", "baseline"]
type SchedulerState = Literal["idle", "scanning", "paused", "stopping"]


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    state: SchedulerState
    next_periodic_at: Instant
    last_completed_at: Instant | None
    trigger: ScanTrigger | None
    last_error: str | None


class ScanReceipt(Protocol):
    @property
    def completed_at(self) -> Instant: ...

    @property
    def next_periodic_at(self) -> Instant: ...


class ScanFuture(Protocol):
    def result(self, timeout: float | None = None) -> ScanReceipt: ...


class SchedulerControl(Protocol):
    def status(self) -> SchedulerStatus: ...

    def scan_now(self) -> ScanFuture: ...

    def set_paused(self, paused: bool, *, persist: Callable[[], None] | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class PreferenceSnapshot:
    values: dict[str, object]
    revision: int


@dataclass(frozen=True, slots=True)
class SourcesSnapshot:
    sources: tuple[SourceRecord, ...]
    counts: tuple[SourceCounts, ...]
    revision: int
    paused: bool


class StatisticsSnapshot(Protocol):
    @property
    def revision(self) -> int: ...

    def has_daily_history(self, start: Instant, end: Instant, harness: Harness | Literal["all"]) -> bool: ...

    def signature(
        self, start: Instant, end: Instant, harness: Harness | Literal["all"]
    ) -> tuple[tuple[str, str, int], ...]: ...

    def rows(
        self,
        start: Instant,
        end: Instant,
        harness: Harness | Literal["all"],
        *,
        model_key: str | None = None,
        coarse: bool = False,
    ) -> Generator[LedgerRow, None, None]: ...


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    catalog: Catalog
    ledger_groups: frozenset[str]
    recalc_groups: frozenset[str]
    links: tuple[VendorLink, ...]


class PricingStore(CommandStore, Protocol):
    def pricing(self, work: UnitOfWork | None = None) -> PricingSnapshot: ...

    def save_catalog(self, work: UnitOfWork, old: Catalog, new: Catalog) -> None: ...

    def models(self, work: UnitOfWork | None = None) -> tuple[ModelPool, int]: ...

    def save_models(self, work: UnitOfWork, old: ModelPool, new: ModelPool, *, at: Instant) -> None: ...


class StatisticsStore(Protocol):
    def acknowledge_quality(self, items: list[dict[str, str]], now: Instant) -> dict[str, object]: ...

    def explore(self, query: dict[str, object], now: Instant) -> dict[str, object]: ...

    def explore_options(self) -> dict[str, object]: ...

    def output_references(self, query: dict[str, object], now: Instant) -> dict[str, object]: ...

    def monitor_data(self, harness: str, now: Instant) -> dict[str, list[dict[str, object]]]: ...

    def analytics(self, query: AnalysisQuery, now: Instant) -> dict[str, object]: ...

    def long_term(self, start: Instant, end: Instant, harness: str) -> dict[str, object]: ...

    def auxiliary(self, start: Instant, end: Instant, harness: str) -> dict[str, object]: ...

    def snapshot(self) -> AbstractContextManager[StatisticsSnapshot]: ...


class EventBus(Protocol):
    def publish(self, event: object) -> None: ...

    def subscribe(self, callback: Callable[[object], None]) -> Callable[[], None]: ...


class UnitOfWork(Protocol):
    def revision(self, scope: str) -> int: ...

    def touch(self, scope: str) -> int: ...

    def emit(self, event: object) -> None: ...


@dataclass(frozen=True, slots=True)
class UnmeteredRequest:
    date: str
    count: int
    kind: Literal["title_generation"] = "title_generation"

    def __post_init__(self) -> None:
        if type(self.date) is not str or calendar_date.fromisoformat(self.date).isoformat() != self.date:
            raise ValueError("Invalid unmetered request date")
        if type(self.count) is not int or self.count <= 0 or self.kind != "title_generation":
            raise ValueError("Invalid unmetered request count or kind")

    def to_dict(self) -> dict[str, object]:
        return {"date": self.date, "kind": self.kind, "count": self.count}


class UnmeteredReader(Protocol):
    def read(self, root: str, start: Instant, end: Instant) -> tuple[UnmeteredRequest, ...] | None: ...


@dataclass(frozen=True, slots=True)
class TitleRequest:
    dedupe_key: str
    occurred_at: Instant
    model_id: str | None = None


class TitleReader(Protocol):
    def read_titles(self, root: str, start: Instant, end: Instant) -> tuple[TitleRequest, ...] | None: ...


@dataclass(frozen=True, slots=True)
class SourceRecord:
    harness: Harness
    path: str
    started_at: Instant | None
    last_scan_at: Instant | None
    status: SourceStatus
    diagnostics: tuple[str, ...]
    unmetered_requests: tuple[UnmeteredRequest, ...] | None = None


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    state: dict[str, object] | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CommitBatchResult:
    inserted: int
    duplicates: int
    before_start: int


@dataclass(frozen=True, slots=True)
class BilledUsage:
    event: UsageEvent
    cost: CostEstimate
    group_id: str | None = None
    version_id: str | None = None
    revision_id: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if type(self.event) is not UsageEvent or type(self.cost) is not CostEstimate:
            raise ValueError("Billing requires validated usage and cost values")
        if (
            type(self.cost.incomplete) is not bool
            or type(self.cost.unpriced) is not bool
            or self.cost.rule_applied not in ("none", "base", "peak", "tier")
        ):
            raise ValueError("Invalid billing quality or rule")
        for value in (self.group_id, self.version_id, self.revision_id):
            if value is not None and (type(value) is not str or not value):
                raise ValueError("Invalid pricing identity")
        if self.version_id is not None and self.group_id is None:
            raise ValueError("Price version requires a group")
        if self.revision_id is not None and self.version_id is None:
            raise ValueError("Price revision requires a version")
        if self.display_name is not None and type(self.display_name) is not str:
            raise ValueError("Invalid display name")


@dataclass(frozen=True, slots=True)
class BillBatchResult:
    billed: int
    corrupt: int
    duplicates: int
    more: bool
    harnesses: tuple[Harness, ...]

    @property
    def processed(self) -> int:
        return self.billed + self.corrupt + self.duplicates


@dataclass(frozen=True, slots=True)
class SourceCounts:
    harness: Harness
    identity_count: int
    processed_count: int
    inbox_count: int
    ledger_count: int


@dataclass(frozen=True, slots=True)
class ReadBatch:
    checkpoint: dict[str, object]
    events: tuple[UsageEvent, ...] = ()
    diagnostics: tuple[str, ...] = ()
    more: bool = False
    available: bool = True
    retry: bool = False
    compaction_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveryBatch:
    paths: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()
    available: bool = True


@dataclass(frozen=True, slots=True)
class UsageChanged:
    harnesses: tuple[Harness, ...]
    revision: int


@dataclass(frozen=True, slots=True)
class WalletChanged:
    vendor_ids: tuple[str, ...]
    revision: int


@dataclass(frozen=True, slots=True)
class CollectionCompleted:
    trigger: ScanTrigger
    revision: int


@dataclass(frozen=True, slots=True)
class PricingChanged:
    revision: int


@dataclass(frozen=True, slots=True)
class QualityChanged:
    revision: int


@dataclass(frozen=True, slots=True)
class ModelsChanged:
    revision: int


@dataclass(frozen=True, slots=True)
class OutputChanged:
    revision: int


@dataclass(frozen=True, slots=True)
class SourcesChanged:
    revision: int


@dataclass(frozen=True, slots=True)
class PreferencesChanged:
    revision: int


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    retention: Literal[90, 180, 360, "forever"]
    cutoff: Instant | None
    due: bool


@dataclass(frozen=True, slots=True)
class RetentionBatch:
    count: int
    harnesses: tuple[Harness, ...]
    more: bool | None = None


class MaintenanceStore(Protocol):
    def compact_history(self, work: UnitOfWork, *, at: Instant) -> int: ...

    def execute[T](self, function: Callable[[UnitOfWork], T]) -> T: ...

    def model_pool(self, work: UnitOfWork) -> ModelPool: ...

    def observed_models(self, work: UnitOfWork) -> Generator[str, None, None]: ...

    def save_models(self, work: UnitOfWork, old: ModelPool, new: ModelPool, *, at: Instant) -> None: ...

    def validate_wallet(self, work: UnitOfWork, *, at: Instant) -> tuple[str, ...]: ...

    def retention_plan(self, work: UnitOfWork, *, at: Instant) -> RetentionPlan: ...

    def expire_inbox(self, work: UnitOfWork, plan: RetentionPlan, *, at: Instant) -> int: ...

    def delete_ledger(self, work: UnitOfWork, plan: RetentionPlan) -> RetentionBatch: ...

    def finish_retention(
        self, work: UnitOfWork, plan: RetentionPlan, *, deleted: int, expired: int, at: Instant
    ) -> None: ...

    def rotate_logs(self, work: UnitOfWork, *, at: Instant) -> int: ...

    def log(
        self, work: UnitOfWork, code: str, detail: str, *, at: Instant, once_per_day: bool = False
    ) -> None: ...


class SchedulerStore(Protocol):
    def paused(self) -> bool: ...

    def set_paused(self, paused: bool) -> None: ...

    def sources(self) -> tuple[SourceRecord, ...]: ...

    def watcher_status(self, available: bool) -> None: ...

    def completed(self, trigger: ScanTrigger, *, watcher_available: bool) -> int: ...


class SourceWatcher(Protocol):
    def refresh(self, sources: tuple[SourceRecord, ...], notify: Callable[[], None]) -> bool: ...

    def close(self, timeout: float = 1) -> None: ...


class SourceReader(Protocol):
    def read(self, path: str, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch: ...


class SourceDiscovery(Protocol):
    def default_path(self, harness: Harness) -> str: ...

    def available(self, harness: Harness, path: str) -> bool: ...

    def discover(self, harness: Harness, path: str) -> DiscoveryBatch: ...

    def signature(self, harness: Harness, path: str) -> tuple[int, int] | None: ...


class CollectionStore(Protocol):
    def execute[T](self, function: Callable[[UnitOfWork], T]) -> T: ...

    def source(self, harness: Harness) -> SourceRecord | None: ...

    def checkpoint(self, harness: Harness, path: str) -> CheckpointRecord: ...

    def set_path(self, work: UnitOfWork, harness: Harness, path: str, *, available: bool) -> SourceRecord: ...

    def begin_scan(self, work: UnitOfWork, harness: Harness, root: str, *, at: Instant) -> SourceRecord: ...

    def finish_scan(
        self,
        work: UnitOfWork,
        harness: Harness,
        root: str,
        *,
        at: Instant,
        status: SourceStatus,
        diagnostics: tuple[str, ...],
        unmetered_requests: tuple[UnmeteredRequest, ...] | None = None,
        title_requests: tuple[TitleRequest, ...] = (),
    ) -> SourceRecord: ...

    def commit_batch(
        self,
        work: UnitOfWork,
        harness: Harness,
        root: str,
        path: str,
        checkpoint: dict[str, object],
        events: Iterable[UsageEvent],
        *,
        at: Instant,
        compaction_keys: tuple[str, ...] = (),
    ) -> CommitBatchResult: ...

    def catalog(self, work: UnitOfWork) -> Catalog: ...

    def has_inbox(self, work: UnitOfWork) -> bool: ...

    def bill_batch(
        self,
        work: UnitOfWork,
        calculate: Callable[[UsageEvent], BilledUsage | None],
        *,
        at: Instant,
        limit: int,
        on_billed: Callable[[UnitOfWork, BilledUsage], None],
    ) -> BillBatchResult: ...

    def append_consumption(self, work: UnitOfWork, billed: BilledUsage, *, at: Instant) -> str | None: ...
