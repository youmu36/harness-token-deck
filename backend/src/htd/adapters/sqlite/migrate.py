"""Apply checksummed, atomic schema migrations after read-only compatibility inspection."""

import hashlib
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from htd.adapters.sqlite.compact_keys import migrate_keys
from htd.adapters.sqlite.connection import open_connection
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.version import app_version

MIGRATIONS = Path(__file__).with_name("migrations")
APP_ID = "com.harness-token-deck.ledger"
STORAGE_GENERATION = 3
_LOG = logging.getLogger(__name__)


def _inspect(path: Path) -> dict[str, str]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "meta" not in tables:
            if tables:
                raise DomainError("STORAGE_NOT_HTD", "现有数据库不是新版账本")
            return {}
        meta = dict(connection.execute("SELECT key,value FROM meta"))
        try:
            generation = int(meta["storage_generation"])
        except (KeyError, ValueError) as error:
            raise DomainError("STORAGE_METADATA_INVALID", "数据库代际信息无效") from error
        if generation > STORAGE_GENERATION:
            raise DomainError(
                "STORAGE_GENERATION_UNSUPPORTED", "数据库来自较新版本，请升级程序，只允许只读诊断"
            )
        if generation not in (1, 2, STORAGE_GENERATION) or meta.get("app_id") != APP_ID:
            raise DomainError("STORAGE_NOT_HTD", "数据库标识或代际不受支持")
        return dict(connection.execute("SELECT id,checksum FROM schema_migration"))


def _execute_script(connection: sqlite3.Connection, sql: str) -> None:
    # executescript() implicitly commits; execute complete statements individually instead.
    statement = ""
    for character in sql:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        connection.execute(statement)


def migrate(
    path: Path, *, migrations: Path = MIGRATIONS, now: Instant | None = None, version: str | None = None
) -> None:
    path = path.resolve()
    applied = _inspect(path)
    scripts = sorted(migrations.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    checksums = {script.name: hashlib.sha256(script.read_bytes()).hexdigest() for script in scripts}
    if not scripts:
        raise DomainError("MIGRATION_MISSING", "未找到数据库迁移")
    for name, checksum in applied.items():
        if checksums.get(name) != checksum:
            raise DomainError("MIGRATION_CHECKSUM_MISMATCH", "已应用迁移校验失败，请恢复原始迁移文件", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    at = format_instant(now or Instant(datetime.now(UTC)))
    with closing(open_connection(path)) as connection:
        pending = [script for script in scripts if script.name not in applied]
        full_schema = "0036_remote_refresh_interval.sql" in checksums
        extension_missing = full_schema and (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='wallet_inheritance'"
            ).fetchone()
            is None
            or connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='session_hit_rate'"
            ).fetchone()
            is None
            or connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='wallet_flow'"
            ).fetchone()
            is None
        )
        if full_schema and any(
            connection.execute("SELECT 1 FROM sqlite_schema WHERE name=?", (table,)).fetchone() is None
            for table in (
                "usage_dimensions",
                "wallet_recalc_pending",
                "session_directory",
                "session_request",
                "reconciliation_member",
                "calibration_target",
                "usage_recent",
                "session_monitor_revision",
            )
        ):
            extension_missing = True
        if applied and (pending or extension_missing):
            backups = path.parent / "backups"
            backups.mkdir(exist_ok=True)
            from htd.adapters.sqlite.backup_names import reserve_backup

            backup = reserve_backup(backups, datetime.now(UTC))
            with closing(sqlite3.connect(backup)) as target:
                connection.backup(target)
            older = sorted(
                (item for item in backups.glob("htd-*.sqlite3") if item != backup),
                key=lambda item: (item.stat().st_mtime_ns, item.name),
            )
            for old in older[:-9]:
                old.unlink()
        for script in pending:
            _LOG.info("数据库迁移: %s", script.name)
            if script.name == "0026_compact_request_keys.sql":
                migrate_keys(connection, migration=script.name, at=at, checksum=checksums[script.name])
                continue
            # SQLite retains deferred DROP-parent violations even after an equivalent
            # parent is recreated. Disable enforcement outside this one rebuild;
            # foreign_key_check still validates every reference before committing.
            rebuild = script.name in (
                "0012_claude_providers.sql",
                "0015_vendor_sites_remote.sql",
                "0036_remote_refresh_interval.sql",
            )
            if rebuild:
                connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                # 0025 backfills new counters on existing append-only audit rows.
                # Keep the released SQL/checksum intact; restore the exact guard
                # within this exclusive transaction, including on rollback.
                audit_guard = None
                if script.name == "0025_compact_projections.sql":
                    audit_guard = connection.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name='recalc_batch_update'"
                    ).fetchone()[0]
                    connection.execute("DROP TRIGGER recalc_batch_update")
                _execute_script(connection, script.read_text(encoding="utf-8"))
                if audit_guard is not None:
                    connection.execute(audit_guard)
                if script.name == "0001_core.sql":
                    connection.executemany(
                        "INSERT INTO meta VALUES (?,?)",
                        (
                            ("app_id", APP_ID),
                            ("storage_generation", str(STORAGE_GENERATION)),
                            ("created_at", at),
                            ("created_by_version", version if version is not None else app_version()),
                        ),
                    )
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise DomainError("MIGRATION_FOREIGN_KEY_FAILED", "迁移外键检查失败")
                connection.execute(
                    "INSERT INTO schema_migration VALUES (?,?,?)",
                    (
                        script.name,
                        at,
                        checksums[script.name],
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                _LOG.exception("数据库迁移失败，当前事务已回滚: %s", script.name)
                raise
            finally:
                if rebuild:
                    connection.execute("PRAGMA foreign_keys=ON")
        # Keep the released migration list readable by 0.4.1. Feature tables are
        # additive and atomic, and never alter shared positional INSERT layouts.
        if not full_schema:
            return
        from htd.adapters.sqlite.extensions_042 import install

        connection.execute("BEGIN IMMEDIATE")
        try:
            install(connection, at=parse_instant(at))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
