"""Exclusive writer-thread VACUUM and consistent online SQLite backups."""

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from htd.adapters.sqlite.backup_names import reserve_backup
from htd.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant


def _require_idle(work: UnitOfWork) -> sqlite3.Connection:
    if work.connection.in_transaction:
        raise RuntimeError("Use writer.submit_idle for VACUUM and backup")
    return work.connection


def vacuum(work: UnitOfWork, *, at: Instant) -> None:
    connection = _require_idle(work)
    path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    required = (
        connection.execute("PRAGMA page_count").fetchone()[0]
        * connection.execute("PRAGMA page_size").fetchone()[0]
        * 2
    )
    if shutil.disk_usage(path.parent).free < required:
        raise DomainError("VACUUM_DISK_SPACE", "磁盘空间不足，无法安全回收空闲空间")
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
    finally:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("BEGIN IMMEDIATE")
    try:
        MaintenanceRepository(connection).set_state(work, "last_vacuum_at", format_instant(at))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def backup(work: UnitOfWork, *, at: Instant) -> Path:
    connection = _require_idle(work)
    source = Path(connection.execute("PRAGMA database_list").fetchone()[2])
    directory = source.parent / "backups"
    directory.mkdir(exist_ok=True)
    target = reserve_backup(directory, at.value)
    try:
        with closing(sqlite3.connect(target)) as destination:
            connection.backup(destination)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    older = sorted(
        (p for p in directory.glob("htd-*.sqlite3") if p != target),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
    )
    for old in older[:-9]:
        old.unlink()
    return target


def run_vacuum(writer: WriteExecutor, *, at: Instant) -> None:
    writer.submit_idle(lambda work: vacuum(work, at=at)).result(timeout=300)


def run_backup(writer: WriteExecutor, *, at: Instant) -> Path:
    return writer.submit_idle(lambda work: backup(work, at=at)).result(timeout=300)


def idle_checkpoint(db: sqlite3.Connection, *, idle_seconds: float) -> None:
    if db.in_transaction or idle_seconds < 30:
        return
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    wal = Path(str(path) + "-wal")
    if not wal.is_file() or wal.stat().st_size < 4 * 1024**2:
        return
    busy, frames, copied = db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    if busy or frames != copied or idle_seconds < 60 or wal.stat().st_size < 8 * 1024**2:
        return
    # A reader can start after PASSIVE; never wait for it or interrupt its snapshot.
    db.execute("PRAGMA busy_timeout=0")
    try:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        db.execute("PRAGMA busy_timeout=5000")
