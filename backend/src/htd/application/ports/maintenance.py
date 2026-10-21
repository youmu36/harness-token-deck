"""Snapshots and transaction ports for manual maintenance and historical import."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from htd.application.ports import CommandStore, RetentionBatch, RetentionPlan, UnitOfWork
from htd.domain.common.time import Instant
from htd.domain.pricing.catalog import Catalog
from htd.domain.pricing.model import ModelRate
from htd.domain.usage.model import Harness, UsageEvent
from htd.domain.wallet.model import Calibration, WalletEntry

type Retention = Literal[90, 180, 360, "forever"]


@dataclass(frozen=True, slots=True)
class UnpricedDecision:
    ignored: bool = False
    group_id: str | None = None
    version_id: str | None = None
    model: ModelRate | None = None
    multiplier: str = ""


@dataclass(frozen=True, slots=True)
class RecalcRow:
    event: UsageEvent
    old: dict[str, object]
    consumption: WalletEntry | None
    decision: UnpricedDecision | None = None
    local_override: bool = False


@dataclass(frozen=True, slots=True)
class RecalcSnapshot:
    catalog: Catalog
    rows: tuple[RecalcRow, ...]
    vendors: dict[str, str]
    calibrations: dict[str, tuple[Calibration, ...]]
    usage_revision: int = 0
    remote_vendors: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RecalcUpdate:
    row: RecalcRow
    new: dict[str, object]


class ManualMaintenanceStore(CommandStore, Protocol):
    def status(self, *, at: Instant, app_version: str) -> dict[str, object]: ...

    def preview_retention(self, retention: Retention, *, at: Instant) -> dict[str, object]: ...

    def save_retention(self, work: UnitOfWork, retention: Retention) -> None: ...

    def expire_inbox(self, work: UnitOfWork, plan: RetentionPlan, *, at: Instant) -> int: ...

    def delete_ledger(self, work: UnitOfWork, plan: RetentionPlan) -> RetentionBatch: ...

    def finish_retention(
        self, work: UnitOfWork, plan: RetentionPlan, *, deleted: int, expired: int, at: Instant
    ) -> None: ...

    def update_receipt(
        self, work: UnitOfWork, operation_id: str, parameters: dict[str, object], result: dict[str, object]
    ) -> None: ...

    def file_action(
        self, command: str, *, at: Instant, check_cancelled: Callable[[], None]
    ) -> dict[str, object]: ...

    def recalc_snapshot(
        self, harness: Harness, start: Instant, end: Instant, work: UnitOfWork | None = None
    ) -> RecalcSnapshot: ...

    def commit_recalc(
        self,
        work: UnitOfWork,
        parameters: dict[str, object],
        updates: tuple[RecalcUpdate, ...],
        operation_id: str,
        *,
        at: Instant,
    ) -> tuple[str, tuple[str, ...]]: ...


class HistoryStore(CommandStore, Protocol):
    def existing(self, events: Iterable[UsageEvent]) -> frozenset[tuple[Harness, str]]: ...

    def import_events(
        self, work: UnitOfWork, events: Iterable[UsageEvent], *, roots: Mapping[Harness, str], at: Instant
    ) -> dict[str, object]: ...


class UnpricedStore(ManualMaintenanceStore, Protocol):
    def freeze_unpriced(
        self, work: UnitOfWork, operation: str, pricing_revision: int, start: Instant, end: Instant
    ) -> dict[str, object]: ...
    def unpriced_progress(self, operation: str, work: UnitOfWork | None = None) -> dict[str, object]: ...
    def unpriced_batch(
        self, work: UnitOfWork, operation: str
    ) -> tuple[tuple[tuple[str, str], ...], tuple[RecalcSnapshot, ...]]: ...
    def advance_unpriced(
        self,
        work: UnitOfWork,
        operation: str,
        members: tuple[tuple[str, str], ...],
        completed: tuple[tuple[str, str], ...],
        delta: int,
    ) -> dict[str, object]: ...

    def unpriced_notice(self, at: Instant) -> dict[str, object]: ...

    def save_unpriced(
        self, work: UnitOfWork, rows: tuple[RecalcRow, ...], decision: UnpricedDecision, *, at: Instant
    ) -> None: ...

    def save_unpriced_catalog(self, work: UnitOfWork, old: Catalog, new: Catalog) -> None: ...

    def unpriced_snapshots(
        self, start: Instant, end: Instant, work: UnitOfWork | None = None
    ) -> tuple[RecalcSnapshot, ...]: ...

    def archived_unpriced(self, start: Instant, end: Instant) -> list[dict[str, object]]: ...
