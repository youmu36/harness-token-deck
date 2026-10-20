"""Restartable shadow copy, verified in bounded batches before an atomic switch."""

import json
import logging
import re
import shutil
import sqlite3
from pathlib import Path

from htu.adapters.sqlite.key_codec import key_bytes
from htu.domain.common.errors import DomainError

TABLES = ("identity", "inbox", "ledger", "wallet_entry", "recalc_item", "title_request")
_LOG = logging.getLogger(__name__)


def migrate_keys(
    db: sqlite3.Connection, *, migration: str, at: str, checksum: str, batch_size: int = 500
) -> None:
    path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    required = (
        db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0] * 2
        + 16 * 1024**2
    )
    if shutil.disk_usage(path.parent).free < required:
        raise DomainError("MIGRATION_DISK_SPACE", "升级所需磁盘空间不足，原始数据已保留")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS key_upgrade_progress(name TEXT PRIMARY KEY,"
            "last_key TEXT,finished INTEGER NOT NULL DEFAULT 0) STRICT"
        )
        for table in TABLES:
            exists = db.execute("SELECT 1 FROM key_upgrade_progress WHERE name=?", (table,)).fetchone()
            if exists:
                continue
            sql = db.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            sql = re.sub(r"^CREATE TABLE\s+[^\s(]+", "CREATE TABLE _key_" + table, sql, count=1, flags=re.I)
            sql = sql.replace("dedupe_key TEXT", "dedupe_key BLOB CHECK(length(dedupe_key)=32)")
            sql = sql.replace(
                "usage_key TEXT", "usage_key BLOB CHECK(usage_key IS NULL OR length(usage_key)=32)"
            )
            sql = sql.replace(
                "length(dedupe_key)=64 AND dedupe_key NOT GLOB '*[^0-9a-f]*'", "length(dedupe_key)=32"
            )
            sql = sql.replace("length(dedupe_key)=64", "length(dedupe_key)=32")
            db.execute(sql)
            db.execute("INSERT INTO key_upgrade_progress(name) VALUES(?)", (table,))
            # An older process cannot write into a table while it is being copied.
            for action in ("INSERT", "UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER _key_guard_{table}_{action} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'storage_upgrade_in_progress'); END"
                )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    # Shadows retain the original FK names, which resolve to the new tables at switch.
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in TABLES:
            info = db.execute(f"PRAGMA table_info({table})").fetchall()
            columns = [row[1] for row in info]
            primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            order = ",".join(primary)
            keys = [name for name in columns if name in ("dedupe_key", "usage_key")]
            while True:
                db.execute("BEGIN IMMEDIATE")
                try:
                    progress = db.execute(
                        "SELECT last_key,finished FROM key_upgrade_progress WHERE name=?", (table,)
                    ).fetchone()
                    if progress[1]:
                        db.rollback()
                        break
                    last = json.loads(progress[0]) if progress[0] else None
                    clause = f" WHERE ({order})>({','.join('?' for _ in primary)})" if last else ""
                    rows = db.execute(
                        f"SELECT * FROM {table}" + clause + f" ORDER BY {order} LIMIT ?",
                        (*last, batch_size) if last else (batch_size,),
                    ).fetchall()
                    if not rows:
                        db.execute("UPDATE key_upgrade_progress SET finished=1 WHERE name=?", (table,))
                        db.commit()
                        break
                    for row in rows:
                        values = tuple(
                            key_bytes(row[name]) if name in keys and row[name] is not None else row[name]
                            for name in columns
                        )
                        db.execute(
                            f"INSERT INTO _key_{table} VALUES({','.join('?' for _ in columns)})", values
                        )
                        # Verify the whole row; codecs expose exactly the former strings.
                        where = " AND ".join(name + "=?" for name in primary)
                        params = tuple(
                            key_bytes(row[name]) if name in keys else row[name] for name in primary
                        )
                        copied = db.execute(f"SELECT * FROM _key_{table} WHERE " + where, params).fetchone()
                        if copied is None or dict(copied) != dict(row):
                            raise DomainError("MIGRATION_VERIFY_FAILED", "身份转换校验失败，原始数据已保留")
                    last = [rows[-1][name] for name in primary]
                    db.execute(
                        "UPDATE key_upgrade_progress SET last_key=? WHERE name=?", (json.dumps(last), table)
                    )
                    db.commit()
                    _LOG.info("数据库身份升级: %s，本批 %s 条", table, len(rows))
                except BaseException:
                    db.rollback()
                    raise
        db.execute("BEGIN IMMEDIATE")
        try:
            schema = db.execute(
                "SELECT type,name,sql FROM sqlite_schema WHERE tbl_name IN ("
                + ",".join("?" for _ in TABLES)
                + ") AND type IN ('index','trigger') AND sql IS NOT NULL AND name NOT LIKE '_key_guard_%'",
                TABLES,
            ).fetchall()
            for table in TABLES:
                old = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                new = db.execute(f"SELECT COUNT(*) FROM _key_{table}").fetchone()[0]
                if old != new:
                    raise DomainError("MIGRATION_VERIFY_FAILED", "身份转换数量校验失败")
                db.execute(f"DROP TABLE {table}")
            for table in TABLES:
                db.execute(f"ALTER TABLE _key_{table} RENAME TO {table}")
            for item in schema:
                db.execute(item["sql"])
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise DomainError("MIGRATION_FOREIGN_KEY_FAILED", "身份转换外键校验失败")
            db.execute("UPDATE meta SET value='2' WHERE key='storage_generation'")
            db.execute("INSERT INTO schema_migration VALUES(?,?,?)", (migration, at, checksum))
            db.execute("DROP TABLE key_upgrade_progress")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")
