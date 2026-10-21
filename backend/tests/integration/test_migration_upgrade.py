"""Upgrade populated audit history without weakening append-only protection."""

# pyright: reportPrivateUsage=false

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from htd.adapters.sqlite import migrate as migration
from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.writer import WriteExecutor

AT = "2026-10-17T09:00:00.000Z"


def old_database(tmp_path: Path) -> Path:
    scripts = tmp_path / "migrations"
    scripts.mkdir()
    for script in sorted(migration.MIGRATIONS.glob("*.sql"))[:24]:
        shutil.copyfile(script, scripts / script.name)
    path = tmp_path / "htd.sqlite3"
    migration.migrate(path, migrations=scripts)
    with closing(open_connection(path)) as db:
        db.execute("UPDATE meta SET value='1' WHERE key='storage_generation'")
        for count in range(3):
            db.execute(
                "INSERT INTO recalc_batch VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(count),
                    "codex",
                    AT,
                    AT,
                    "group",
                    "version",
                    count,
                    "3",
                    "5",
                    0,
                    0,
                    "fp",
                    AT,
                    str(count),
                ),
            )
            for item in range(count):
                db.execute(
                    "INSERT INTO recalc_item VALUES(?,?,?,?,?)",
                    (str(count), "codex", f"{item:064x}", '{"pico":"3"}', '{"pico":"5"}'),
                )
    return path


def assert_append_only(db: sqlite3.Connection) -> None:
    for sql in (
        "UPDATE recalc_batch SET new_pico='999' WHERE id='1'",
        "UPDATE recalc_batch SET changed_count=999 WHERE id='1'",
        "DELETE FROM recalc_batch WHERE id='0'",
        "UPDATE recalc_item SET new_json='{}'",
        "DELETE FROM recalc_item",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            db.execute(sql)


def test_populated_audit_upgrade_preserves_history_and_starts_writer(tmp_path: Path) -> None:
    path = old_database(tmp_path)
    with closing(open_connection(path)) as db:
        before = [dict(row) for row in db.execute("SELECT * FROM recalc_batch ORDER BY id")]
        items = [dict(row) for row in db.execute("SELECT * FROM recalc_item ORDER BY batch_id,dedupe_key")]
        guard = db.execute("SELECT sql FROM sqlite_schema WHERE name='recalc_batch_update'").fetchone()[0]

    with WriteExecutor(path):
        pass
    migration.migrate(path)
    with closing(open_connection(path)) as db:
        after = [dict(row) for row in db.execute("SELECT * FROM recalc_batch ORDER BY id")]
        assert [{name: row[name] for name in old} for old, row in zip(before, after, strict=True)] == before
        assert [(row["changed_count"], row["unchanged_count"]) for row in after] == [(0, 0), (1, 0), (2, 0)]
        assert all(row["identity_fingerprint"] == "" for row in after)
        converted = [
            dict(row) for row in db.execute("SELECT * FROM recalc_item ORDER BY batch_id,dedupe_key")
        ]
        assert [{name: row[name] for name in old} for old, row in zip(items, converted, strict=True)] == items
        assert (
            db.execute("SELECT sql FROM sqlite_schema WHERE name='recalc_batch_update'").fetchone()[0]
            == guard
        )
        assert db.execute("SELECT count(*) FROM schema_migration").fetchone()[0] == len(
            list(migration.MIGRATIONS.glob("*.sql"))
        )
        assert db.execute("SELECT value FROM meta WHERE key='storage_generation'").fetchone()[0] == "3"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert_append_only(db)

    backups = list((tmp_path / "backups").glob("htd-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as backup:
        assert backup.execute("SELECT count(*) FROM schema_migration").fetchone()[0] == 24
        assert backup.execute("SELECT count(*) FROM recalc_batch").fetchone()[0] == 3


def test_failed_audit_upgrade_restores_guard_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = old_database(tmp_path)
    execute = migration._execute_script

    def fail(db: sqlite3.Connection, sql: str) -> None:
        execute(db, sql)
        if "UPDATE recalc_batch SET changed_count=record_count;" in sql:
            raise RuntimeError("upgrade interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(migration, "_execute_script", fail)
        with pytest.raises(RuntimeError, match="upgrade interrupted"):
            migration.migrate(path)

    with closing(open_connection(path)) as db:
        assert db.execute("SELECT count(*) FROM schema_migration").fetchone()[0] == 24
        assert "changed_count" not in {row[1] for row in db.execute("PRAGMA table_info(recalc_batch)")}
        assert not db.execute("SELECT 1 FROM sqlite_schema WHERE name='wallet_archive'").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            db.execute("UPDATE recalc_batch SET new_pico='999'")
        assert db.execute("SELECT count(*) FROM recalc_item").fetchone()[0] == 3

    with WriteExecutor(path):
        pass
    with closing(open_connection(path)) as db:
        assert_append_only(db)
