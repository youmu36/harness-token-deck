"""Provide automatic maintenance ports on the serialized writer at maintenance priority."""

from collections.abc import Callable, Generator
from uuid import uuid4

from htu.adapters.sqlite.ledger_read import LedgerReader
from htu.adapters.sqlite.maintenance_repo import MaintenanceRepository, RetentionResult, cutoff_for
from htu.adapters.sqlite.models_repo import ModelsRepository
from htu.adapters.sqlite.rollup_repo import RollupRepository
from htu.adapters.sqlite.wallet_store import WalletStore
from htu.adapters.sqlite.writer import MAINTENANCE, WriteExecutor
from htu.adapters.sqlite.writer import UnitOfWork as SqlWork
from htu.application.ports import RetentionBatch, RetentionPlan, UnitOfWork
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, format_instant
from htu.domain.pricing.model_pool import ModelPool
from htu.domain.usage.model import Harness


def _work(work: UnitOfWork) -> SqlWork:
    if not isinstance(work, SqlWork):
        raise TypeError("Requires SQLite UnitOfWork")
    return work


class SqliteMaintenanceStore:
    def compact_history(self, work: UnitOfWork, *, at: Instant) -> int:
        return RollupRepository(_work(work).connection).compact(work, at=at)

    def __init__(self, writer: WriteExecutor) -> None:
        self.writer = writer

    def execute[T](self, function: Callable[[UnitOfWork], T]) -> T:
        return self.writer.execute(function, priority=MAINTENANCE)

    def model_pool(self, work: UnitOfWork) -> ModelPool:
        return ModelsRepository(_work(work).connection).load()

    def observed_models(self, work: UnitOfWork) -> Generator[str, None, None]:
        return LedgerReader(_work(work).connection).observed_models()

    def save_models(self, work: UnitOfWork, old: ModelPool, new: ModelPool, *, at: Instant) -> None:
        ModelsRepository(_work(work).connection).save(_work(work), old, new, at=at)

    def validate_wallet(self, work: UnitOfWork, *, at: Instant) -> tuple[str, ...]:
        from htu.adapters.sqlite.dsh_repair import repair

        repair(_work(work).connection, work, at)
        return WalletStore(_work(work).connection).validate_if_due(work, at=at)

    def retention_plan(self, work: UnitOfWork, *, at: Instant) -> RetentionPlan:
        repo = MaintenanceRepository(_work(work).connection)
        retention = repo.retention()
        if retention == "forever":
            return RetentionPlan(retention, None, False)
        cutoff = cutoff_for(retention, at)
        assert cutoff is not None
        stamp = format_instant(cutoff)
        due = _work(work).connection.execute(
            "SELECT EXISTS(SELECT 1 FROM ledger WHERE occurred_at<?) "
            "OR EXISTS(SELECT 1 FROM inbox WHERE occurred_at<?) "
            "OR EXISTS(SELECT 1 FROM usage_rollup WHERE bucket<?) "
            "OR EXISTS(SELECT 1 FROM usage_archive WHERE start_at<?)",
            (stamp, stamp, stamp, stamp),
        )
        return RetentionPlan(retention, cutoff, bool(due.fetchone()[0]))

    def _retention_repo(self, work: UnitOfWork, plan: RetentionPlan) -> MaintenanceRepository:
        repo = MaintenanceRepository(_work(work).connection)
        if repo.retention() != plan.retention:
            raise DomainError("RETENTION_CHANGED", "保留期限已变更，等待下轮维护")
        return repo

    def expire_inbox(self, work: UnitOfWork, plan: RetentionPlan, *, at: Instant) -> int:
        assert plan.cutoff is not None
        return self._retention_repo(work, plan).expire_inbox(_work(work), plan.cutoff, at=at)

    def delete_ledger(self, work: UnitOfWork, plan: RetentionPlan) -> RetentionBatch:
        assert plan.cutoff is not None
        repo = self._retention_repo(work, plan)
        rows = (
            _work(work)
            .connection.execute(
                "SELECT DISTINCT harness FROM (SELECT harness FROM ledger WHERE occurred_at<? "
                "UNION ALL SELECT harness FROM usage_rollup WHERE bucket<? "
                "UNION ALL SELECT harness FROM usage_archive WHERE start_at<?)",
                (format_instant(plan.cutoff),) * 3,
            )
            .fetchall()
        )
        changed = {Harness(row[0]) for row in rows}
        count = repo.delete_ledger_batch(_work(work), plan.cutoff)
        return RetentionBatch(
            count, tuple(h for h in Harness if h in changed), repo.has_expired_usage(plan.cutoff)
        )

    def finish_retention(
        self, work: UnitOfWork, plan: RetentionPlan, *, deleted: int, expired: int, at: Instant
    ) -> None:
        self._retention_repo(work, plan).finish_retention(
            _work(work), RetentionResult(str(uuid4()), deleted, expired), plan.retention, at=at
        )

    def rotate_logs(self, work: UnitOfWork, *, at: Instant) -> int:
        return MaintenanceRepository(_work(work).connection).rotate_logs(_work(work), at=at)

    def log(
        self, work: UnitOfWork, code: str, detail: str, *, at: Instant, once_per_day: bool = False
    ) -> None:
        connection = _work(work).connection
        day = Instant(at.value.replace(hour=0, minute=0, second=0, microsecond=0))
        if (
            once_per_day
            and connection.execute(
                "SELECT 1 FROM diagnostic_log WHERE code=? AND occurred_at>=? AND occurred_at<=? LIMIT 1",
                (code, format_instant(day), format_instant(at)),
            ).fetchone()
        ):
            return
        MaintenanceRepository(connection).log(_work(work), code, detail, at=at)
