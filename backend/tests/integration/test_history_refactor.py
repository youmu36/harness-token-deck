"""Retention, old shared-layout writers and full backups preserve the new financial evidence."""

# pyright: reportPrivateUsage=false

from contextlib import closing
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from htd.adapters.sqlite.auto_maintenance_store import SqliteMaintenanceStore
from htd.adapters.sqlite.connection import ReadPool, open_connection
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.maintenance_files import run_backup
from htd.adapters.sqlite.maintenance_repo import MaintenanceRepository, run_retention
from htd.adapters.sqlite.recalc_repo import RecalcBatch, RecalcChange, RecalcRepository
from htd.adapters.sqlite.rollup_repo import RollupRepository
from htd.adapters.sqlite.usage_daily import cutoff
from htd.adapters.sqlite.writer import WriteExecutor
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import format_instant, parse_instant
from htd.domain.usage.model import Tokens
from htd.ipc.dto.explorer import ExploreResponse
from htd.ipc.dto.reconciliation import CalibrationPreview, CalibrationResponse
from htd.migration.native import restore, verify_native

from backend.tests.contracts.command_helpers import invoke

from .collection_helpers import collect, quote, usage
from .test_calibration_targets import balance, proposal
from .test_explorer_043 import AT, bill, setup
from .test_reconciliation_batches import USD, configured, receive


def test_daily_authority_cannot_retire_a_sixty_one_day_old_minute(tmp_path: Path) -> None:
    now = parse_instant("2026-05-01T00:00:00Z")
    recent = parse_instant("2026-03-01T00:00:00Z")
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        setup(writer, at=recent)
        bill(writer, (usage(1, at=recent, model="gpt-6-astra"),), at=recent)
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=now))
        with readers.connection() as db:
            assert parse_instant(cutoff(db, now)).value <= now.value - timedelta(days=62)
            assert not db.execute("SELECT 1 FROM usage_daily").fetchone()
            assert db.execute("SELECT COUNT(*) FROM usage_recent").fetchone()[0] == 1


def test_old_writer_below_completed_cursor_resumes_backfill_once(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer:
        setup(writer)
        bill(writer, (usage(1000, at=AT, model="gpt-6-astra"),))
        store = SqliteMaintenanceStore(writer)
        writer.execute(lambda w: store.compact_history(w, at=AT))
    event = usage(1, at=AT, model="gpt-6-astra")
    with closing(open_connection(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        # Released positional layout and shared revision only: no new projection hooks.
        db.execute(
            "INSERT INTO identity VALUES(?,?,?,1,?,'live')",
            (event.harness.value, key_bytes(event.dedupe_key), event.occurred_at, format_instant(AT)),
        )
        IngestRepository(db)._insert_ledger(quote(event), AT)
        db.execute("UPDATE revision SET value=value+1 WHERE scope='usage'")
        db.commit()
    with WriteExecutor(path) as writer:
        store = SqliteMaintenanceStore(writer)
        writer.execute(lambda w: store.compact_history(w, at=AT))
        writer.execute(lambda w: store.compact_history(w, at=AT))
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM session_request").fetchone()[0]
            )
            == 2
        )
        assert (
            writer.execute(
                lambda w: sum(
                    int(row[0]) for row in w.connection.execute("SELECT records FROM session_total")
                )
            )
            == 2
        )
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM reconciliation_member").fetchone()[0]
            )
            == 2
        )


def test_locked_target_receipt_backup_and_pending_input_survive_long_retention(tmp_path: Path) -> None:
    clock = FakeClock(parse_instant("2026-09-30T00:00:00Z"))
    with Application(tmp_path / "source", clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(11 * 60_000)
        parameters = proposal(app, supplier, 10 * USD)
        preview = invoke(app, "reconciliation.previewCalibration", parameters, CalibrationPreview)
        operation = str(uuid4())
        commit = dict(parameters, fingerprint=preview.fingerprint)
        first = invoke(
            app, "reconciliation.commitCalibration", dict(commit, operationId=operation), CalibrationResponse
        )
        before = balance(app, supplier)
        # An accepted pending input remains recoverable even beyond a retention cutoff.
        collect(app.writer, (usage(2, at=clock.now_utc()),), at=clock.now_utc())
        clock.advance(400 * 86400000)
        for _ in range(8):
            count = app.writer.execute(
                lambda w: RollupRepository(w.connection).compact(w, at=clock.now_utc())
            )
            if count == 0:
                break
        run_retention(app.writer, 90, at=clock.now_utc())
        app.writer.execute(lambda w: MaintenanceRepository(w.connection).rotate_logs(w, at=clock.now_utc()))
        assert (
            invoke(
                app,
                "reconciliation.commitCalibration",
                dict(commit, operationId=operation),
                CalibrationResponse,
            )
            == first
        )
        assert balance(app, supplier) == before
        with app.readers.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM calibration_member").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM reconciliation_member").fetchone()[0] == 1
        backup = run_backup(app.writer, at=clock.now_utc())
        assert backup.parent == app.data_dir / "backups"
    restored = tmp_path / "restored.db"
    restore(backup, restored)
    result = verify_native(backup, restored)
    assert result.ok and result.checks["calibration_target"] and result.checks["session_request"]


def test_old_recalculation_retired_before_return_preserves_locked_effective_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from htd.adapters.sqlite import explorer_storage, session_core

    clock = FakeClock(parse_instant("2026-09-30T00:00:00Z"))
    root = tmp_path / "source"
    with Application(root, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        event = usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0))
        receive(app, event)
        clock.advance(11 * 60_000)
        parameters = proposal(app, supplier, 10 * USD)
        preview = invoke(app, "reconciliation.previewCalibration", parameters, CalibrationPreview)
        invoke(
            app,
            "reconciliation.commitCalibration",
            dict(parameters, fingerprint=preview.fingerprint, operationId=str(uuid4())),
            CalibrationResponse,
        )
        with app.readers.connection() as db:
            marker = db.execute(
                "SELECT value FROM runtime_state WHERE key='session_writer_revision'"
            ).fetchone()[0]
            cursor = db.execute(
                "SELECT value FROM runtime_state WHERE key='session_recalc_cursor'"
            ).fetchone()[0]

        def old_recalc(work):
            repo = RecalcRepository(work.connection)
            before = repo.snapshot(event.harness, event.dedupe_key)
            after = dict(before, pico=str(9 * USD))
            batch = RecalcBatch(
                str(uuid4()),
                event.harness,
                parse_instant(parameters["start"]),
                parse_instant(parameters["end"]),
                before["price_group_id"],
                before["price_version_id"],
                "legacy",
                str(uuid4()),
            )
            repo.commit(
                work,
                batch,
                (RecalcChange(event.harness, event.dedupe_key, before, after),),
                at=clock.now_utc(),
            )

        app.writer.execute(old_recalc)
        clock.advance(4 * 86400000)
        with monkeypatch.context() as patch:
            patch.setattr(session_core, "remember_ledger", lambda *args: None)
            patch.setattr(explorer_storage, "retire", lambda *args: None)
            app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=clock.now_utc()))
        app.writer.execute(
            lambda w: w.connection.executemany(
                "UPDATE runtime_state SET value=? WHERE key=?",
                ((marker, "session_writer_revision"), (cursor, "session_recalc_cursor")),
            )
        )
    with Application(root, clock=clock, start_scheduler=False) as app:
        store = SqliteMaintenanceStore(app.writer)
        app.writer.execute(lambda w: store.compact_history(w, at=clock.now_utc()))
        app.writer.execute(lambda w: store.compact_history(w, at=clock.now_utc()))
        data = invoke(
            app, "stats.explore", {"start": parameters["start"], "end": parameters["end"]}, ExploreResponse
        )
        assert data.totals.estimatePico == str(9 * USD)
        assert data.totals.correctionPico == str(USD) and data.totals.picoUsd == str(10 * USD)
