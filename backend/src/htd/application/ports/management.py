"""Command receipts, wallet snapshots and background task ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from htd.domain.common.time import Instant
from htd.domain.usage.model import Harness
from htd.domain.wallet.model import Totals, Vendor

if TYPE_CHECKING:
    from htd.application.ports import PreferenceSnapshot, SourcesSnapshot, UnitOfWork


class CommandStore(Protocol):
    def set_scan_enabled(self, work: UnitOfWork, harness: Harness, enabled: bool, at: Instant) -> None: ...
    def execute[T](self, action: Callable[[UnitOfWork], T]) -> T: ...

    def preferences(self, work: UnitOfWork | None = None) -> PreferenceSnapshot: ...

    def save_preferences(self, work: UnitOfWork, values: dict[str, object]) -> None: ...

    def sources(self, work: UnitOfWork | None = None, *, details: bool = True) -> SourcesSnapshot: ...

    def set_path(self, work: UnitOfWork, harness: Harness, path: str, *, available: bool) -> None: ...

    def save_paused(self, work: UnitOfWork, paused: bool) -> None: ...

    def replay(
        self, work: UnitOfWork, operation_id: str, command: str, parameters: dict[str, object]
    ) -> dict[str, object] | None: ...

    def record(
        self,
        work: UnitOfWork,
        operation_id: str,
        command: str,
        parameters: dict[str, object],
        result: dict[str, object],
        *,
        at: Instant,
    ) -> None: ...


class TaskContext(Protocol):
    def check_cancelled(self) -> None: ...

    def progress(self, completed: int, total: int) -> None: ...


class TaskControl(Protocol):
    def reserve(self) -> str: ...

    def discard(self, task_id: str) -> None: ...

    def start(self, task_id: str, action: Callable[[TaskContext], dict[str, object]]) -> None: ...

    def get(self, task_id: str) -> dict[str, object]: ...

    def cancel(self, task_id: str) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class TaskProgress:
    task_id: str
    completed: int
    total: int


@dataclass(frozen=True, slots=True)
class TaskDone:
    task_id: str
    ok: bool
    result: dict[str, object] | None
    error: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class VendorSnapshot:
    vendor: Vendor
    totals: Totals
    linked_groups: tuple[str, ...]
    history_coverage_start: Instant | None
    account: Mapping[str, object] | None = None
    local_wallet: Mapping[str, object] | None = None
    remote_wallet: Mapping[str, object] | None = None
    wallet_visibility: str = "both"


class WalletCommandStore(CommandStore, Protocol):
    def wallets(
        self,
        *,
        at: Instant,
        include_archived: bool,
        work: UnitOfWork | None = None,
        vendor_id: str | None = None,
    ) -> tuple[tuple[VendorSnapshot, ...], int]: ...

    def wallet_entries(
        self,
        vendor_id: str,
        cursor: int | None,
        limit: int,
        kind: str = "all",
        offset_minutes: int = 0,
        *,
        at: Instant | None = None,
    ) -> dict[str, object]: ...

    def preview_interval(self, vendor_id: str, interval_id: str) -> dict[str, object]: ...

    def account_entries(
        self,
        vendor_id: str,
        cursor: str | None,
        limit: int,
        scope: str = "legacy",
        *,
        at: Instant | None = None,
        kind: str = "all",
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> dict[str, object]: ...

    def daily_book(self, vendor_id: str, scope: str, month: str, at: Instant) -> dict[str, object]: ...

    def wallet_command(
        self, work: UnitOfWork, command: str, parameters: dict[str, object], operation_id: str, *, at: Instant
    ) -> tuple[str, str | None, tuple[str, ...]]: ...
