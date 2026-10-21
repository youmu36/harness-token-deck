"""Lossless archived counters across partitions, late imports, retention and rollback."""

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from htd.adapters.sqlite.codex_audit import read_audit_ledger
from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.ledger_read import LedgerReader
from htd.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htd.adapters.sqlite.migrate import MIGRATIONS, migrate
from htd.adapters.sqlite.rollup_repo import RollupRepository
from htd.adapters.sqlite.usage_archive import unpack
from htd.bootstrap import Application
from htd.domain.common.time import Instant, parse_instant
from htd.domain.statistics import Summary, Window
from htd.domain.usage.model import Harness

from backend.tests.contracts.command_helpers import seed_rows
from backend.tests.unit.statistics.helpers import row

START = parse_instant("2026-09-01T00:00:00Z")
END = parse_instant("2026-10-01T00:00:00Z")
AT = parse_instant("2026-11-03T12:00:00Z")


def test_upgrade_from_0015_preserves_values_and_creates_a_recoverable_backup(tmp_path: Path) -> None:
    old = tmp_path / "old-migrations"
    old.mkdir()
    for migration in MIGRATIONS.glob("*.sql"):
        if migration.name < "0016":
            (old / migration.name).write_bytes(migration.read_bytes())
    path = tmp_path / "htd.sqlite3"
    migrate(path, migrations=old)
    with closing(open_connection(path)) as db:
        db.execute("INSERT INTO preference VALUES('theme','\"dark\"')")
    migrate(path)
    with closing(open_connection(path)) as db:
        assert db.execute("SELECT value_json FROM preference WHERE key='theme'").fetchone()[0] == '"dark"'
        assert db.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0] == len(
            list(MIGRATIONS.glob("*.sql"))
        )
        assert db.execute("SELECT COUNT(*) FROM usage_archive").fetchone()[0] == 0
    backup = next((tmp_path / "backups").glob("*.sqlite3"))
    with closing(sqlite3.connect(backup)) as db:
        assert db.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0] == 15
        assert db.execute("SELECT value_json FROM preference WHERE key='theme'").fetchone()[0] == '"dark"'


def compact(app: Application) -> None:
    while app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=AT)) == 500:
        pass


def summary(app: Application, harness: Harness | str = "all", offset: int = 0) -> Summary:
    return app.statistics.summary(
        "all" if harness == "all" else Harness(harness), Window(START, END, offset, "hour")
    )[0]


def test_month_partitions_preserve_all_statistics_and_late_rows_after_restart(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    with Application(directory, start_scheduler=False) as app:
        seed_rows(
            app,
            (
                row(
                    i + 1,
                    (i // 4) % 60,
                    hour=(i // 4) // 60,
                    day=13,
                    harness=list(Harness)[i % 4],
                    model=f"model-{i % 3}",
                    input_count=70_000,
                    cache=0 if i % 3 else 50_000,
                    pico=9_007_199_254_740_993,
                    unpriced=i % 7 == 0,
                )
                for i in range(1200)
            ),
        )
        before = {
            (h, offset): summary(app, h, offset) for h in ("all", *Harness) for offset in (0, 345, -210)
        }
        compact(app)
        for (h, offset), expected in before.items():
            assert summary(app, h, offset) == expected
        with app.readers.connection() as db:
            blocks = db.execute("SELECT month,payload FROM usage_archive").fetchall()
            assert len(blocks) < 40  # Bounded blocks partition by model as well as Harness/day.
            assert all(
                block["month"] == "2026-09" and len(unpack(block["payload"])) <= 500 for block in blocks
            )
            assert db.execute("SELECT COUNT(*) FROM usage_rollup").fetchone()[0] == 0
            assert sum(c.ledger_count for c in LedgerReader(db).source_counts()) == 1200
            assert len(tuple(LedgerReader(db).observed_models())) == 3
        late = row(1201, 0, hour=1, day=13, harness=Harness.CODEX, model="model-0", pico=123)
        seed_rows(app, [late])
        expected = summary(app)
        compact(app)
        assert summary(app) == expected
        assert summary(app).metrics.record_count == 1201
    with Application(directory, start_scheduler=False) as app:
        assert summary(app) == expected
        assert len(summary(app).trend) == 720
        with app.readers.connection() as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    audit = read_audit_ledger(directory / "htd.sqlite3", START, END)
    assert audit.compacted_records == 301
    assert audit.compacted_pico == before[Harness.CODEX, 0].metrics.record_count * 9_007_199_254_740_993 + 123


def test_archive_delete_failure_rolls_back_then_retry_and_retention_cut_exact_minutes(tmp_path: Path) -> None:
    with Application(tmp_path / "data", start_scheduler=False) as app:
        seed_rows(app, [row(i + 1, i, day=13, pico=2**63 + i) for i in range(5)])
        at = Instant(START.value + timedelta(days=20))
        app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=at))
        expected = summary(app)
        app.writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail_archive BEFORE DELETE ON usage_rollup "
                "BEGIN SELECT RAISE(ABORT,'archive'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="archive"):
            compact(app)
        assert summary(app) == expected
        with app.readers.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM usage_archive").fetchone()[0] == 0
        app.writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail_archive"))
        compact(app)
        cutoff = parse_instant("2026-09-13T00:02:00Z")
        preview = app.writer.execute(
            lambda w: MaintenanceRepository(w.connection).preview(
                90, at=Instant(cutoff.value + timedelta(days=90))
            )
        )
        assert preview.ledger_count == 2
        assert (
            app.writer.execute(
                lambda w: MaintenanceRepository(w.connection).delete_ledger_batch(w, cutoff, limit=1)
            )
            == 2
        )
        assert (
            app.writer.execute(lambda w: MaintenanceRepository(w.connection).delete_ledger_batch(w, cutoff))
            == 0
        )
        result = summary(app)
        assert result.metrics == expected.metrics
        assert result.history_notice and result.trend == result.hourly == ()
        exact = app.statistics.long_term(START, END, "all")
        assert exact["totals"]["records"] == "2"
        assert exact["totals"]["pico"] == str(2**64 + 1)


def test_30_day_boundary_is_complete_utc_day_and_custom_minute_query_is_exact(tmp_path: Path) -> None:
    with Application(tmp_path / "data", start_scheduler=False) as app:
        seed_rows(app, [row(1, 1, day=13), row(2, 2, day=13), row(3, day=14)])
        boundary = parse_instant("2026-10-13T12:00:00Z")
        app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=boundary))
        with app.readers.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM usage_archive").fetchone()[0] == 0
        compact(app)
        window = Window(
            parse_instant("2026-09-13T00:02:00Z"), parse_instant("2026-09-13T00:03:00Z"), 345, "hour"
        )
        result, _ = app.statistics.summary(Harness.CODEX, window)
        assert result.metrics.record_count == 1 and result.hourly[0].label == "09-13 05:00"
