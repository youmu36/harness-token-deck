"""Bounded retention transactions, diagnostic rotation, and persisted maintenance status."""

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.transaction import require_transaction
from htd.adapters.sqlite.usage_archive import archive_preview
from htd.adapters.sqlite.usage_codec import dumps, loads
from htd.adapters.sqlite.writer import MAINTENANCE, UnitOfWork, WriteExecutor
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.version import app_version as current_version

type Retention = Literal[90, 180, 360, "forever"]


def cutoff_for(retention: Retention, at: Instant) -> Instant | None:
    if retention == "forever":
        return None
    if type(retention) is not int or retention not in (90, 180, 360):
        raise ValueError("Invalid retention")
    return Instant(at.value - timedelta(days=retention))


@dataclass(frozen=True, slots=True)
class RetentionPreview:
    cutoff: Instant | None
    ledger_count: int
    earliest: Instant | None
    latest: Instant | None
    inbox_count: int
    identity_count: int


@dataclass(frozen=True, slots=True)
class RetentionResult:
    run_id: str
    deleted_ledger: int
    expired_inbox: int


@dataclass(frozen=True, slots=True)
class MaintenanceStatus:
    paused: bool
    retention: Retention
    last_retention_at: Instant | None
    last_vacuum_at: Instant | None
    last_log_rotation_at: Instant | None
    pending_expired: int
    log_count: int
    database_bytes: int
    app_version: str
    data_dir: Path


class MaintenanceRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def state(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, work: UnitOfWork, key: str, value: str) -> None:
        require_transaction(work, self.connection)
        if type(key) is not str or not key or type(value) is not str:
            raise ValueError("Invalid runtime state")
        self.connection.execute(
            "INSERT INTO runtime_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def retention(self) -> Retention:
        row = self.connection.execute("SELECT value_json FROM preference WHERE key='retention'").fetchone()
        value = loads(row[0]) if row else "forever"
        if value == "forever":
            return "forever"
        if type(value) is int and value in (90, 180, 360):
            return 90 if value == 90 else 180 if value == 180 else 360
        raise ValueError("Invalid stored retention")

    def save_retention(self, work: UnitOfWork, retention: Retention) -> None:
        require_transaction(work, self.connection)
        cutoff_for(retention, parse_instant("2000-01-01T00:00:00Z"))
        if self.retention() != retention:
            self.connection.execute(
                "INSERT INTO preference VALUES ('retention',?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (dumps(retention),),
            )
            work.touch("preferences")

    def preview(self, retention: Retention, *, at: Instant) -> RetentionPreview:
        cutoff = cutoff_for(retention, at)
        stamp = format_instant(cutoff) if cutoff else ""
        row = self.connection.execute(
            "SELECT COALESCE(SUM(records),0),MIN(occurred_at),MAX(occurred_at) FROM "
            "(SELECT 1 records,occurred_at FROM ledger UNION ALL SELECT records,bucket FROM usage_rollup) "
            "WHERE occurred_at<?",
            (stamp,),
        ).fetchone()
        inbox = self.connection.execute(
            "SELECT COUNT(*) FROM inbox i WHERE occurred_at<? AND NOT EXISTS(SELECT 1 FROM "
            "session_request s WHERE s.harness=i.harness AND s.dedupe_key=i.dedupe_key "
            "AND s.state='accepted' AND s.occurred_at=i.occurred_at)",
            (stamp,),
        ).fetchone()[0]
        identities = self.connection.execute("SELECT COUNT(*) FROM identity").fetchone()[0]
        count, earliest, latest = archive_preview(self.connection, stamp)
        starts = [value for value in (row[1], earliest) if value]
        ends = [value for value in (row[2], latest) if value]
        return RetentionPreview(
            cutoff,
            row[0] + count,
            parse_instant(min(starts)) if starts else None,
            parse_instant(max(ends)) if ends else None,
            inbox,
            identities,
        )

    def expire_inbox(self, work: UnitOfWork, cutoff: Instant, *, at: Instant, limit: int = 500) -> int:
        require_transaction(work, self.connection)
        self._limit(limit)
        rows = self.connection.execute(
            "SELECT harness,dedupe_key FROM inbox i WHERE occurred_at<? AND NOT EXISTS(SELECT 1 FROM "
            "session_request s WHERE s.harness=i.harness AND s.dedupe_key=i.dedupe_key "
            "AND s.state='accepted' AND s.occurred_at=i.occurred_at) "
            "ORDER BY occurred_at,harness,dedupe_key LIMIT ?",
            (format_instant(cutoff), limit),
        ).fetchall()
        identities = [(row[0], key_bytes(row[1])) for row in rows]
        if identities:
            self.connection.executemany(
                "UPDATE identity SET billed=1 WHERE harness=? AND dedupe_key=?", identities
            )
            self.connection.executemany("DELETE FROM inbox WHERE harness=? AND dedupe_key=?", identities)
            self.log(work, "retention_inbox_expired", f"到期未入账请求 {len(rows)} 条，身份已保留", at=at)
            work.touch("sources")
        return len(rows)

    def delete_ledger_batch(self, work: UnitOfWork, cutoff: Instant, *, limit: int = 500) -> int:
        require_transaction(work, self.connection)
        self._limit(limit)
        # Retention now retires fine detail only after a verified authoritative
        # publication. Rollup and daily publication each have bounded batches.
        from htd.adapters.sqlite.rollup_repo import RollupRepository
        from htd.adapters.sqlite.usage_daily import publish

        synthetic_now = Instant(cutoff.value + timedelta(days=90))
        RollupRepository(self.connection).compact(work, at=synthetic_now, limit=limit, publish_daily=False)
        published = publish(
            self.connection, work, at=synthetic_now, before=format_instant(cutoff), limit=limit
        )
        return published

    def has_expired_usage(self, cutoff: Instant) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM ledger WHERE occurred_at<? UNION ALL "
                "SELECT 1 FROM usage_rollup WHERE bucket<? UNION ALL "
                "SELECT 1 FROM usage_archive WHERE start_at<? LIMIT 1",
                (format_instant(cutoff),) * 3,
            ).fetchone()
            is not None
        )

    @staticmethod
    def _limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("Batch limit must be 1..500")

    def finish_retention(
        self, work: UnitOfWork, result: RetentionResult, retention: Retention, *, at: Instant
    ) -> None:
        require_transaction(work, self.connection)
        cutoff = cutoff_for(retention, at)
        self.connection.execute(
            "INSERT INTO retention_run VALUES (?,?,?,?,?,?)",
            (
                result.run_id,
                str(retention),
                format_instant(cutoff) if cutoff else None,
                result.deleted_ledger,
                result.expired_inbox,
                format_instant(at),
            ),
        )
        self.set_state(work, "last_retention_at", format_instant(at))
        self.log(
            work,
            "retention_committed",
            f"删除账本 {result.deleted_ledger} 条，过期入箱 {result.expired_inbox} 条",
            at=at,
        )

    def log(self, work: UnitOfWork, code: str, detail: str, *, at: Instant) -> None:
        require_transaction(work, self.connection)
        if type(detail) is not str or len(detail) > 500 or type(code) is not str or not code:
            raise ValueError("Invalid diagnostic message")
        self.connection.execute(
            "INSERT INTO diagnostic_log VALUES(NULL,?,NULL,?,?)", (format_instant(at), code, detail)
        )

    def rotate_logs(self, work: UnitOfWork, *, at: Instant) -> int:
        require_transaction(work, self.connection)
        previous = self.state("last_log_rotation_at")
        if previous is not None and previous[:10] == format_instant(at)[:10]:
            return 0
        cutoff = format_instant(Instant(at.value - timedelta(days=14)))
        first = self.connection.execute("DELETE FROM diagnostic_log WHERE occurred_at<?", (cutoff,)).rowcount
        second = self.connection.execute(
            "DELETE FROM diagnostic_log WHERE id IN (SELECT id FROM diagnostic_log "
            "ORDER BY occurred_at DESC,id DESC LIMIT -1 OFFSET 2000)"
        ).rowcount
        # Receipts have a separate 30-day horizon; vouchers retain their operation IDs.
        receipt_cutoff = format_instant(Instant(at.value - timedelta(days=30)))
        pricing_receipts = self.connection.execute(
            "SELECT 1 FROM operation WHERE command='pricing.switch' AND at<? LIMIT 1", (receipt_cutoff,)
        ).fetchone()
        self.connection.execute(
            "DELETE FROM operation WHERE at<? AND command NOT IN "
            "('wallet.reclassify','wallet.correctConsumption','wallet.reconcil"
            "eInterval','pricing.recalculateUnpriced','remote.save','remote.de"
            "lete') "
            "AND command<>'reconciliation.commitCalibration' "
            "AND NOT (command='wallet.calibrate' AND EXISTS(SELECT 1 FROM account_posting p "
            "WHERE p.operation_id=operation.id||':account'))",
            (receipt_cutoff,),
        )
        if pricing_receipts is not None:
            work.touch("pricing")
        self.set_state(work, "last_log_rotation_at", format_instant(at))
        return first + second

    def status(self, *, at: Instant, app_version: str | None = None) -> MaintenanceStatus:
        retention = self.retention()
        preview = self.preview(retention, at=at)
        raw = self.connection.execute("PRAGMA database_list").fetchone()[2]
        path = Path(raw)
        database_bytes = sum(
            p.stat().st_size
            for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
            if p.is_file()
        )

        def instant(key: str) -> Instant | None:
            text = self.state(key)
            return parse_instant(text) if text else None

        return MaintenanceStatus(
            self.state("collection_paused") == "true",
            retention,
            instant("last_retention_at"),
            instant("last_vacuum_at"),
            instant("last_log_rotation_at"),
            preview.ledger_count + preview.inbox_count,
            self.connection.execute("SELECT COUNT(*) FROM diagnostic_log").fetchone()[0],
            database_bytes,
            app_version if app_version is not None else current_version(),
            path.parent,
        )


class RetentionFailure(RuntimeError):
    def __init__(self, partial: RetentionResult) -> None:
        super().__init__("Retention interrupted; committed batches remain and cleanup can be retried")
        self.partial = partial


def run_retention(
    writer: WriteExecutor, retention: Retention, *, at: Instant, save_setting: bool = False
) -> RetentionResult:
    """Each executor submission is one short transaction; failures expose committed counts."""
    cutoff = cutoff_for(retention, at)
    if save_setting:
        writer.execute(lambda work: MaintenanceRepository(work.connection).save_retention(work, retention))
    expired = deleted = 0
    run_id = str(uuid4())
    try:
        if cutoff is not None:
            while True:
                count = writer.execute(
                    lambda work: MaintenanceRepository(work.connection).expire_inbox(work, cutoff, at=at),
                    priority=MAINTENANCE,
                )
                expired += count
                if count < 500:
                    break
            while True:
                count = writer.execute(
                    lambda work: MaintenanceRepository(work.connection).delete_ledger_batch(work, cutoff),
                    priority=MAINTENANCE,
                )
                deleted += count
                if not writer.execute(
                    lambda work: MaintenanceRepository(work.connection).has_expired_usage(cutoff),
                    priority=MAINTENANCE,
                ):
                    break
        result = RetentionResult(run_id, deleted, expired)
        writer.execute(
            lambda work: MaintenanceRepository(work.connection).finish_retention(
                work, result, retention, at=at
            ),
            priority=MAINTENANCE,
        )
        return result
    except Exception as error:
        raise RetentionFailure(RetentionResult(run_id, deleted, expired)) from error
