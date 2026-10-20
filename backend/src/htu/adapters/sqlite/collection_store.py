"""Wire collection repository ports to the existing serialized writer and read pool."""

from collections.abc import Callable, Iterable

from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.ingest_repo import IngestRepository
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.sources_repo import SourcesRepository
from htu.adapters.sqlite.title_repo import TitleRepository
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import BILLING, WriteExecutor
from htu.adapters.sqlite.writer import UnitOfWork as SqlWork
from htu.application.ports import (
    BillBatchResult,
    BilledUsage,
    CheckpointRecord,
    CommitBatchResult,
    SourceRecord,
    SourceStatus,
    TitleRequest,
    UnitOfWork,
    UnmeteredRequest,
)
from htu.domain.common.time import Instant
from htu.domain.pricing.catalog import Catalog
from htu.domain.usage.model import Harness, UsageEvent


def _work(work: UnitOfWork) -> SqlWork:
    if not isinstance(work, SqlWork):
        raise TypeError("Requires SQLite UnitOfWork")
    return work


class SqliteCollectionStore:
    def __init__(self, writer: WriteExecutor, readers: ReadPool) -> None:
        self.writer = writer
        self.readers = readers

    def execute[T](self, function: Callable[[UnitOfWork], T]) -> T:
        return self.writer.execute(function, priority=BILLING)

    def source(self, harness: Harness) -> SourceRecord | None:
        with self.readers.connection() as db:
            return SourcesRepository(db).get(harness)

    def checkpoint(self, harness: Harness, path: str) -> CheckpointRecord:
        with self.readers.connection() as db:
            return SourcesRepository(db).checkpoint(harness, path)

    def set_path(self, work: UnitOfWork, harness: Harness, path: str, *, available: bool) -> SourceRecord:
        return SourcesRepository(_work(work).connection).set_path(work, harness, path, available=available)

    def begin_scan(self, work: UnitOfWork, harness: Harness, root: str, *, at: Instant) -> SourceRecord:
        return SourcesRepository(_work(work).connection).begin_scan(work, harness, root, at=at)

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
    ) -> SourceRecord:
        db = _work(work).connection
        source = SourcesRepository(db).finish_scan(
            work,
            harness,
            root,
            at=at,
            status=status,
            diagnostics=diagnostics,
            unmetered_requests=unmetered_requests,
        )
        if harness == Harness.CODEX:
            TitleRepository(db).collect(work, title_requests, at=at)
        return source

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
    ) -> CommitBatchResult:
        return IngestRepository(_work(work).connection).commit_batch(
            work, harness, root, path, checkpoint, events, at=at, compaction_keys=compaction_keys
        )

    def catalog(self, work: UnitOfWork) -> Catalog:
        return PricingRepository(_work(work).connection).load()

    def has_inbox(self, work: UnitOfWork) -> bool:
        return _work(work).connection.execute("SELECT 1 FROM inbox LIMIT 1").fetchone() is not None

    def bill_batch(
        self,
        work: UnitOfWork,
        calculate: Callable[[UsageEvent], BilledUsage | None],
        *,
        at: Instant,
        limit: int,
        on_billed: Callable[[UnitOfWork, BilledUsage], None],
    ) -> BillBatchResult:
        return IngestRepository(_work(work).connection).bill_batch(
            work, calculate, at=at, limit=limit, on_billed=on_billed
        )

    def append_consumption(self, work: UnitOfWork, billed: BilledUsage, *, at: Instant) -> str | None:
        entry = WalletRepository(_work(work).connection).append_consumption(work, billed, at=at)
        return entry.vendor_id if entry is not None else None
