"""Retention preserves financial/history data and commits bounded, retryable batches."""

import shutil
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.history_repo import HistoryRepository
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.maintenance_files import run_backup, run_vacuum
from htd.adapters.sqlite.maintenance_repo import MaintenanceRepository, RetentionFailure, run_retention
from htd.adapters.sqlite.migrate import MIGRATIONS, migrate
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.usage_codec import encode_event
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.domain.common.time import Instant, format_instant
from htd.domain.usage.model import Harness

from backend.tests.integration.collection_helpers import AT, collect, ensure_source, quote, usage
from backend.tests.integration.test_pricing_storage import create
from backend.tests.integration.test_wallet_storage import consumption, recharge, vendor

OLD = Instant(AT.value - timedelta(days=400))
RECENT = Instant(AT.value - timedelta(minutes=5))


def seed(work: UnitOfWork, count: int = 1) -> None:
    MaintenanceRepository(work.connection).save_retention(work, 360)
    repo = IngestRepository(work.connection)
    for index in range(count + 1):
        at = OLD if index < count else RECENT
        event = usage(index, at=at)
        work.connection.execute(
            "INSERT INTO identity VALUES (?,?,?,0,?,'live')",
            (event.harness.value, key_bytes(event.dedupe_key), event.occurred_at, format_instant(AT)),
        )
        work.connection.execute(
            "INSERT INTO inbox VALUES (?,?,?,?,?)",
            (
                event.harness.value,
                key_bytes(event.dedupe_key),
                event.occurred_at,
                encode_event(event),
                format_instant(AT),
            ),
        )
    while repo.bill_batch(work, quote, at=AT).more:
        pass


def test_M05_M06_retention_preserves_identity_pricing_and_wallet(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(create)
        writer.execute(vendor)
        writer.execute(recharge)
        writer.execute(consumption)
        writer.execute(seed)
        root = ensure_source(writer)
        writer.execute(lambda w: SourcesRepository(w.connection).begin_scan(w, Harness.CODEX, root, at=OLD))
        collect(writer, (usage(100, at=OLD),), at=AT)
        before = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        preview = writer.execute(lambda w: MaintenanceRepository(w.connection).preview(360, at=AT))
        assert (preview.ledger_count, preview.inbox_count, preview.identity_count) == (1, 0, 3)
        assert preview.earliest == preview.latest == OLD
        result = run_retention(writer, 360, at=AT)
        assert (result.deleted_ledger, result.expired_inbox) == (1, 0)
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")) == before
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM identity").fetchone()[0]) == 3
        )
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM price_revision").fetchone()[0]
            )
            == 1
        )
        assert (
            writer.execute(
                lambda w: w.connection.execute(
                    "SELECT billed FROM identity WHERE dedupe_key=?", (key_bytes(usage(100).dedupe_key),)
                ).fetchone()[0]
            )
            == 0
        )
        assert collect(writer, (usage(100, at=OLD),), at=AT).duplicates == 1
        status = writer.execute(lambda w: MaintenanceRepository(w.connection).status(at=AT))
        assert status.pending_expired == 0 and status.last_retention_at == AT
        assert status.database_bytes > 0


def test_each_retention_batch_commits_and_partial_failure_can_retry(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(lambda w: seed(w, 1001))

        def fail_second(work: UnitOfWork) -> None:
            key = usage(500).dedupe_key
            work.connection.execute(
                f"CREATE TRIGGER fail BEFORE DELETE ON ledger WHEN OLD.dedupe_key=X'{key}' "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )

        writer.execute(fail_second)
        with pytest.raises(RetentionFailure) as failure:
            run_retention(writer, 90, at=AT, save_setting=True)
        assert failure.value.partial.deleted_ledger == 500
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]) == 502
        )
        assert writer.execute(lambda w: MaintenanceRepository(w.connection).retention()) == 90
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail"))
        retry = run_retention(writer, 90, at=AT)
        assert retry.deleted_ledger == 501
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM identity").fetchone()[0])
            == 1002
        )


def test_setting_save_failure_prevents_cleanup(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(seed)
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail BEFORE INSERT ON preference BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            run_retention(writer, 90, at=AT, save_setting=True)
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]) == 2
        )


@pytest.mark.parametrize("failure", ["identity", "inbox"])
def test_expire_inbox_atomicity(tmp_path: Path, failure: str) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        root = ensure_source(writer)
        writer.execute(lambda w: SourcesRepository(w.connection).begin_scan(w, Harness.CODEX, root, at=OLD))
        collect(writer, (usage(100, at=OLD),), at=AT)
        writer.execute(lambda w: w.connection.execute("UPDATE session_request SET state='corrupt'"))
        action = "UPDATE" if failure == "identity" else "DELETE"
        writer.execute(
            lambda w: w.connection.execute(
                f"CREATE TRIGGER fail BEFORE {action} ON {failure} BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(RetentionFailure):
            run_retention(writer, 360, at=AT)
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT billed FROM identity").fetchone()[0]) == 0
        )
        assert writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]) == 1
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM diagnostic_log").fetchone()[0]
            )
            == 0
        )


def test_forever_and_cutoff_boundary(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(seed)
        result = run_retention(writer, "forever", at=AT)
        assert result.deleted_ledger == result.expired_inbox == 0
        boundary = Instant(AT.value - timedelta(days=360))
        assert (
            writer.execute(
                lambda w: MaintenanceRepository(w.connection).preview(
                    360, at=Instant(OLD.value + timedelta(days=360))
                )
            ).ledger_count
            == 0
        )
        assert boundary < AT


def test_logs_rotation_maximum_age_and_state(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:

        def logs(work: UnitOfWork) -> None:
            repo = MaintenanceRepository(work.connection)
            repo.log(work, "old", "expired", at=OLD)
            for index in range(2010):
                repo.log(work, "current", str(index), at=AT)
            repo.set_state(work, "collection_paused", "true")

        writer.execute(logs)
        assert writer.execute(lambda w: MaintenanceRepository(w.connection).rotate_logs(w, at=AT)) == 11
        status = writer.execute(lambda w: MaintenanceRepository(w.connection).status(at=AT))
        assert status.paused and status.log_count == 2000 and status.last_log_rotation_at == AT
        assert (
            writer.execute(
                lambda w: w.connection.execute(
                    "SELECT detail FROM diagnostic_log ORDER BY id LIMIT 1"
                ).fetchone()[0]
            )
            == "10"
        )


def test_vacuum_backup_openable_retention_and_writer_continues(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(seed)
        for index in range(12):
            run_backup(writer, at=Instant(AT.value + timedelta(seconds=index)))
        backups = sorted((tmp_path / "backups").glob("htd-*.sqlite3"))
        assert len(backups) == 10
        with closing(sqlite3.connect(backups[-1])) as copy:
            assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert copy.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2
        run_vacuum(writer, at=AT)
        assert writer.execute(lambda w: w.connection.execute("PRAGMA journal_mode").fetchone()[0]) == "wal"
        assert writer.execute(
            lambda w: MaintenanceRepository(w.connection).state("last_vacuum_at")
        ) == format_instant(AT)
        writer.execute(lambda w: MaintenanceRepository(w.connection).set_state(w, "after_vacuum", "yes"))
    with closing(open_connection(path)) as connection:
        assert MaintenanceRepository(connection).state("after_vacuum") == "yes"


def test_history_dedup_filter_and_source_boundaries_unchanged(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        root = ensure_source(writer)
        collect(writer, (usage(1),))
        checkpoint = writer.execute(
            lambda w: SourcesRepository(w.connection).checkpoint(Harness.CODEX, root + "/session.jsonl")
        )
        result = writer.execute(
            lambda w: HistoryRepository(w.connection).import_events(
                w,
                (usage(1), usage(2, at=RECENT), usage(2, at=RECENT), usage(3, at=OLD)),
                roots={Harness.CODEX: root},
                at=AT,
            )
        )
        assert (result.inserted, result.duplicates, result.too_old) == (1, 2, 1)
        assert (
            writer.execute(
                lambda w: SourcesRepository(w.connection).checkpoint(Harness.CODEX, root + "/session.jsonl")
            )
            == checkpoint
        )
        source = writer.execute(lambda w: SourcesRepository(w.connection).get(Harness.CODEX))
        assert source is not None and source.started_at == AT
        assert (
            writer.execute(
                lambda w: w.connection.execute(
                    "SELECT origin FROM identity WHERE dedupe_key=?", (key_bytes(usage(2).dedupe_key),)
                ).fetchone()[0]
            )
            == "history"
        )


@pytest.mark.parametrize("failure", ["inbox", "diagnostic_log"])
def test_history_import_rolls_back_all_rows(tmp_path: Path, failure: str) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        root = ensure_source(writer)
        writer.execute(
            lambda w: w.connection.execute(
                f"CREATE TRIGGER fail BEFORE INSERT ON {failure} BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            writer.execute(
                lambda w: HistoryRepository(w.connection).import_events(
                    w, (usage(1), usage(2)), roots={Harness.CODEX: root}, at=AT
                )
            )
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM identity").fetchone()[0]) == 0
        )


def test_history_source_path_changed(tmp_path: Path) -> None:
    from htd.domain.common.errors import DomainError

    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        ensure_source(writer)
        with pytest.raises(DomainError) as changed:
            writer.execute(
                lambda w: HistoryRepository(w.connection).import_events(
                    w, (usage(1),), roots={Harness.CODEX: "old"}, at=AT
                )
            )
        assert changed.value.code == "SOURCE_PATH_CHANGED"


def test_P23_upgrade_preserves_data_and_backs_up(tmp_path: Path) -> None:
    old_scripts = tmp_path / "old-migrations"
    old_scripts.mkdir()
    for source in sorted(MIGRATIONS.glob("*.sql"))[:3]:
        shutil.copy2(source, old_scripts / source.name)
    path = tmp_path / "data.sqlite3"
    migrate(path, migrations=old_scripts, now=AT)
    with closing(open_connection(path)) as connection:
        connection.execute("INSERT INTO preference VALUES ('retention','180')")
    migrate(path, now=AT)
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0] == len(
            list(MIGRATIONS.glob("*.sql"))
        )
        assert MaintenanceRepository(connection).retention() == 180
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    copies = list((tmp_path / "backups").glob("htd-*.sqlite3"))
    assert len(copies) == 1
    with closing(sqlite3.connect(copies[0])) as before:
        assert before.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0] == 3
    migrate(path, now=AT)
    assert len(list((tmp_path / "backups").glob("htd-*.sqlite3"))) == 1


def test_backups_with_identical_timestamp_keep_returned_files(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        stem = f"htd-{AT.value.astimezone().strftime('%Y%m%d-%H%M%S')}"
        for index in range(12):
            result = run_backup(writer, at=AT)
            assert result.is_file()
            if index < 10:
                assert result.name == f"{stem}{'' if index == 0 else f'-{index + 1}'}.sqlite3"
        assert len(list((tmp_path / "backups").glob("htd-*.sqlite3"))) == 10
