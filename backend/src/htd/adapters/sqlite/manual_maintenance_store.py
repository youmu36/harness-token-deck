"""Manual maintenance snapshots, resumable receipts and exclusive file operations."""

from collections.abc import Callable

from htd.adapters.sqlite.auto_maintenance_store import SqliteMaintenanceStore
from htd.adapters.sqlite.command_store import SqliteCommandStore, connection, read_snapshot
from htd.adapters.sqlite.maintenance_files import backup, vacuum
from htd.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htd.adapters.sqlite.storage_size import footprint
from htd.adapters.sqlite.usage_codec import dumps
from htd.adapters.sqlite.writer import UnitOfWork as SqlWork
from htd.application.ports import RetentionBatch, RetentionPlan, UnitOfWork
from htd.application.ports.maintenance import Retention
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant


class SqliteManualMaintenanceStore(SqliteCommandStore):
    def status(self, *, at: Instant, app_version: str) -> dict[str, object]:
        with read_snapshot(self.readers) as db:
            state = MaintenanceRepository(db).status(at=at, app_version=app_version)
            return {
                "paused": state.paused,
                "retention": state.retention,
                "lastRetentionAt": format_instant(state.last_retention_at)
                if state.last_retention_at
                else None,
                "lastVacuumAt": format_instant(state.last_vacuum_at) if state.last_vacuum_at else None,
                "lastLogRotationAt": format_instant(state.last_log_rotation_at)
                if state.last_log_rotation_at
                else None,
                "pendingExpired": state.pending_expired,
                "logCount": state.log_count,
                "databaseBytes": state.database_bytes,
                "storage": footprint(db, at=at),
                "appVersion": state.app_version,
                "dataDir": str(state.data_dir),
            }

    def preview_retention(self, retention: Retention, *, at: Instant) -> dict[str, object]:
        with read_snapshot(self.readers) as db:
            preview = MaintenanceRepository(db).preview(retention, at=at)
            return {
                "retention": retention,
                "cutoff": format_instant(preview.cutoff) if preview.cutoff else None,
                "ledgerCount": preview.ledger_count,
                "earliest": format_instant(preview.earliest) if preview.earliest else None,
                "latest": format_instant(preview.latest) if preview.latest else None,
                "inboxCount": preview.inbox_count,
                "identityCount": preview.identity_count,
            }

    def save_retention(self, work: UnitOfWork, retention: Retention) -> None:
        if not isinstance(work, SqlWork):
            raise TypeError("Requires SQLite UnitOfWork")
        MaintenanceRepository(connection(work)).save_retention(work, retention)

    def expire_inbox(self, work: UnitOfWork, plan: RetentionPlan, *, at: Instant) -> int:
        return SqliteMaintenanceStore(self.writer).expire_inbox(work, plan, at=at)

    def delete_ledger(self, work: UnitOfWork, plan: RetentionPlan) -> RetentionBatch:
        return SqliteMaintenanceStore(self.writer).delete_ledger(work, plan)

    def finish_retention(
        self, work: UnitOfWork, plan: RetentionPlan, *, deleted: int, expired: int, at: Instant
    ) -> None:
        SqliteMaintenanceStore(self.writer).finish_retention(
            work, plan, deleted=deleted, expired=expired, at=at
        )

    def update_receipt(
        self, work: UnitOfWork, operation_id: str, parameters: dict[str, object], result: dict[str, object]
    ) -> None:
        if self.replay(work, operation_id, "maintenance.setRetention", parameters) is None:
            raise DomainError("OPERATION_EXPIRED", "维护回执已过期，请使用新的操作标识重试")
        connection(work).execute(
            "UPDATE operation SET result_json=? WHERE id=?",
            (dumps({"parameters": parameters, "result": result}), operation_id),
        )

    def file_action(
        self, command: str, *, at: Instant, check_cancelled: Callable[[], None]
    ) -> dict[str, object]:
        if command not in ("maintenance.vacuum", "maintenance.backup"):
            raise ValueError("Unknown file maintenance action")

        def action(work: SqlWork) -> dict[str, object]:
            check_cancelled()
            if command == "maintenance.vacuum":
                vacuum(work, at=at)
                return {"completedAt": format_instant(at), "path": None}
            path = backup(work, at=at)
            return {"completedAt": format_instant(at), "path": str(path)}

        return self.writer.submit_idle(action).result()
