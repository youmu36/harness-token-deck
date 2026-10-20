"""Current Feedback: funding metadata, compact storage, recovery and financial parity."""

import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from htu.adapters.sqlite import compact_keys
from htu.adapters.sqlite.command_store import SqliteCommandStore
from htu.adapters.sqlite.connection import ReadPool, open_connection
from htu.adapters.sqlite.migrate import MIGRATIONS, migrate
from htu.adapters.sqlite.recalc_codec import audit
from htu.adapters.sqlite.recalc_repo import RecalcRepository
from htu.adapters.sqlite.wallet_command_store import SqliteWalletCommandStore
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import WriteExecutor
from htu.application.preferences import Preferences
from htu.bootstrap import Application
from htu.domain.common.errors import DomainError
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, format_instant
from htu.domain.usage.model import Harness

from .collection_helpers import AT
from .test_history_rollup import seed as history_seed
from .test_maintenance_storage import seed
from .test_recalc_storage import BATCH, plan
from .test_wallet_storage import USD, consumption, vendor


def test_past_funding_is_editable_persistent_and_never_a_voucher(tmp_path: Path) -> None:
    with Application(tmp_path / "data", start_scheduler=False) as app:
        result = app.router.call(
            "wallet.createVendor",
            {"operationId": str(uuid4()), "name": "Imported", "pastTopUp": "123.456789"},
        )
        assert result["ok"]
        value = result["data"]["vendor"]
        assert value["pastTopUpPico"] == "123456789000000"
        identifier = value["id"]
        before = tuple(value[k] for k in ("R", "C", "A", "B"))
        request = {
            "operationId": str(uuid4()),
            "vendorId": identifier,
            "name": "Imported",
            "pastTopUp": "999.000001",
        }
        changed = app.router.call("wallet.renameVendor", request)
        assert changed["ok"] and app.router.call("wallet.renameVendor", request) == changed
        assert tuple(changed["data"]["vendor"][k] for k in ("R", "C", "A", "B")) == before
        assert app.wallet.entries(identifier, None, 100)["entries"] == []
    with Application(tmp_path / "data", start_scheduler=False) as app:
        assert app.wallet.list(False)["vendors"][0]["pastTopUpPico"] == "999000001000000"


def test_startup_setting_rolls_back_with_failed_preference_receipt(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer, ReadPool(writer.path) as readers:
        changes: list[bool] = []
        preferences = Preferences(
            SqliteCommandStore(writer, readers), FakeClock(AT), set_startup=changes.append
        )
        assert preferences.get().values["closeToTray"] is True
        assert preferences.get().values["startAtLogin"] is False
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER reject_preference BEFORE INSERT ON operation BEGIN "
                "SELECT RAISE(ABORT,'receipt failed'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            preferences.update({"startAtLogin": True}, 0, "failed")
        assert changes == [True, False]
        assert preferences.get().values["startAtLogin"] is False
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER reject_preference"))
        result = preferences.update({"startAtLogin": True}, 0, "enabled")
        assert preferences.update({"startAtLogin": True}, 0, "enabled") == result
        assert changes == [True, False, True]


def test_retired_history_commands_are_not_advertised_or_executable(tmp_path: Path) -> None:
    with Application(tmp_path, start_scheduler=False) as app:
        hello = app.router.call("app.hello", {"clientProtocol": 1})
        assert hello["ok"]
        assert not any(name.startswith("history.") for name in hello["data"]["capabilities"])
        for command in ("history.scan", "history.import", "history.cancel"):
            assert not app.router.call(command, {"operationId": str(uuid4())})["ok"]
        assert "migration.run" in hello["data"]["capabilities"]
        assert "models.scan" in hello["data"]["capabilities"]
        assert "wallet.scanProviders" in hello["data"]["capabilities"]


def test_unchanged_recalculation_keeps_batch_evidence_without_duplicate_items(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(seed)
        change = writer.execute(plan)
        writer.execute(lambda w: RecalcRepository(w.connection).commit(w, BATCH, (change,), at=AT))
        new_time = Instant(AT.value + timedelta(seconds=1))
        unchanged = replace(change, old=change.new, new=change.new | {"billed_at": format_instant(new_time)})
        writer.execute(
            lambda w: RecalcRepository(w.connection).commit(
                w, replace(BATCH, id="unchanged", operation_id="unchanged"), (unchanged,), at=new_time
            )
        )

        def verify(w):
            batch = w.connection.execute("SELECT * FROM recalc_batch WHERE id='unchanged'").fetchone()
            assert (batch["record_count"], batch["changed_count"], batch["unchanged_count"]) == (1, 0, 1)
            assert len(batch["identity_fingerprint"]) == 64
            items = w.connection.execute("SELECT * FROM recalc_item").fetchall()
            assert len(items) == 1 and audit(items[0]) == (change.old, change.new)
            assert RecalcRepository(w.connection).snapshot(Harness.CODEX, change.dedupe_key) == change.new

        writer.execute(verify)


def old_key_database(tmp_path: Path, count: int = 1100) -> Path:
    scripts = tmp_path / "old-migrations"
    scripts.mkdir()
    for script in sorted(MIGRATIONS.glob("*.sql"))[:25]:
        shutil.copy2(script, scripts / script.name)
    path = tmp_path / "old.sqlite3"
    migrate(path, migrations=scripts)
    with closing(open_connection(path)) as db:
        db.execute("UPDATE meta SET value='1' WHERE key='storage_generation'")
        db.executemany(
            "INSERT INTO identity VALUES('codex',?,?,1,?,'live')",
            ((f"{i:064x}", format_instant(AT), format_instant(AT)) for i in range(count)),
        )
        key = "0" * 64
        db.execute(
            "INSERT INTO ledger(harness,dedupe_key,occurred_at,request_id,"
            "model_key,status,issues_json,rule_applied,pico,incomplete,unpriced,"
            "billed_at) VALUES('codex',?,?,'request','','reported','[]','none','3',"
            "0,0,?)",
            (key, format_instant(AT), format_instant(AT)),
        )
        db.execute(
            "INSERT INTO inbox VALUES('codex',?,?,?,?)",
            (f"{1:064x}", format_instant(AT), "{}", format_instant(AT)),
        )
        db.execute("INSERT INTO vendor VALUES('vendor','Shop','active',?,1)", (format_instant(AT),))
        db.execute("INSERT INTO wallet_total VALUES('vendor','100','5','-5',3)")
        db.execute(
            "INSERT INTO wallet_entry(id,vendor_id,kind,amount_pico,occurred_at,"
            "seq,usage_harness,usage_key) VALUES('original','vendor','consumption',"
            "'3',?,1,'codex',?)",
            (format_instant(AT), key),
        )
        db.execute(
            "INSERT INTO wallet_entry(id,vendor_id,kind,amount_pico,occurred_at,"
            "seq,usage_harness,usage_key,related_entry_id,recalc_batch_id) "
            "VALUES('delta','vendor','consumption_delta','2',?,2,'codex',?,"
            "'original','batch')",
            (format_instant(AT), key),
        )
        db.execute(
            "INSERT INTO wallet_entry(id,vendor_id,kind,amount_pico,occurred_at,"
            "seq,note) VALUES('calibration','vendor','adjustment','-5',?,3,"
            "'calibration')",
            (format_instant(AT),),
        )
        db.execute("INSERT INTO calibration VALUES('calibration','90','95',2,?)", (format_instant(AT),))
        db.execute(
            "INSERT INTO title_request VALUES(?,? ,NULL,'vendor','original')", ("f" * 64, format_instant(AT))
        )
        db.execute(
            "INSERT INTO recalc_batch(id,harness,start_at,end_at,group_id,"
            "version_id,record_count,old_pico,new_pico,unpriced_count,"
            "incomplete_count,fingerprint,committed_at,operation_id) "
            "VALUES('batch','codex',?,?,'g','v',1,'3','5',0,0,'fingerprint',?,"
            "'operation')",
            (format_instant(AT), format_instant(AT), format_instant(AT)),
        )
        db.execute(
            "INSERT INTO recalc_item(batch_id,harness,dedupe_key,old_json,"
            "new_json) VALUES('batch','codex',?,'{" + '"pico":"3"' + "}','{" + '"pico":"5"' + "}')",
            (key,),
        )
    return path


def test_key_backfill_resumes_after_interruption_and_keeps_the_original_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = old_key_database(tmp_path)
    with closing(open_connection(path)) as db:
        original_rows = {
            table: sorted(
                [dict(row) for row in db.execute(f"SELECT * FROM {table}")],
                key=lambda row: json.dumps(row, sort_keys=True),
            )
            for table in compact_keys.TABLES
        }
    original = compact_keys.key_bytes
    calls = 0

    def interrupt(value):
        nonlocal calls
        calls += 1
        if calls == 1100:
            raise RuntimeError("interrupted backfill")
        return original(value)

    with monkeypatch.context() as patch:
        patch.setattr(compact_keys, "key_bytes", interrupt)
        with pytest.raises(RuntimeError, match="interrupted backfill"):
            migrate(path)
    with closing(open_connection(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM identity").fetchone()[0] == 1100
        assert db.execute("SELECT COUNT(*) FROM _key_identity").fetchone()[0] == 500
        with pytest.raises(sqlite3.IntegrityError, match="storage_upgrade_in_progress"):
            db.execute("UPDATE identity SET billed=0")
    migrate(path)
    with closing(open_connection(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM identity").fetchone()[0] == 1100
        assert tuple(
            db.execute("SELECT typeof(dedupe_key),length(dedupe_key) FROM identity LIMIT 1").fetchone()
        ) == ("blob", 32)
        assert (
            db.execute("SELECT dedupe_key FROM identity ORDER BY dedupe_key LIMIT 1").fetchone()[0]
            == "0" * 64
        )
        assert all(
            not db.execute(f"PRAGMA foreign_key_check({table})").fetchall() for table in compact_keys.TABLES
        )
        assert {
            table: sorted(
                [
                    {key: row[key] for key in original_rows[table][0]}
                    for row in db.execute(f"SELECT * FROM {table}")
                ],
                key=lambda row: json.dumps(row, sort_keys=True),
            )
            for table in compact_keys.TABLES
        } == original_rows
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute(
            "SELECT name FROM sqlite_schema WHERE name LIKE '_key_%' OR name='key_upgrade_progress'"
        ).fetchall()
    assert list((tmp_path / "backups").glob("*.sqlite3"))


def test_insufficient_upgrade_space_leaves_original_tables_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = old_key_database(tmp_path, 3)
    with monkeypatch.context() as patch:
        patch.setattr(compact_keys.shutil, "disk_usage", lambda _: shutil._ntuple_diskusage(100, 100, 0))
        with pytest.raises(DomainError) as error:
            migrate(path)
        assert error.value.code == "MIGRATION_DISK_SPACE"
    with closing(open_connection(path)) as db:
        assert tuple(db.execute("SELECT COUNT(*),typeof(dedupe_key) FROM identity").fetchone()) == (3, "text")
        assert not db.execute("SELECT name FROM sqlite_schema WHERE name='key_upgrade_progress'").fetchall()


def test_cold_finance_preserves_paging_offsets_quality_and_monotonic_sequences(tmp_path: Path) -> None:
    from htu.adapters.sqlite.rollup_repo import RollupRepository
    from htu.adapters.sqlite.wallet_archive import compact

    path = tmp_path / "data.sqlite3"
    later = Instant(AT.value + timedelta(days=40))
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        history_seed(writer)
        store = SqliteWalletCommandStore(writer, readers)
        expected = {
            offset: store.wallet_entries("vendor", None, 100, offset_minutes=offset)
            for offset in (0, 480, 345, -210)
        }
        before = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later))
        while writer.execute(lambda w: compact(w.connection, at=later)):
            pass
        assert readers.get().execute("SELECT COUNT(*) FROM wallet_archive").fetchone()[0] > 0
        store.release_cache()
        for offset, old in expected.items():
            assert store.wallet_entries("vendor", None, 100, offset_minutes=offset) == old
        assert writer.execute(lambda w: WalletRepository(w.connection).validate_rebuild(w, at=later)) == ()
        after = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        assert after == before
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = SqliteWalletCommandStore(writer, readers)
        assert store.wallet_entries("vendor", None, 100, offset_minutes=345) == expected[345]
        assert writer.execute(lambda w: WalletRepository(w.connection).next_seq()) > before.watermark_seq
        # A new timezone builds its persisted directory from both hot and cold data.
        assert store.wallet_entries("vendor", None, 100, offset_minutes=330)["entries"]


def test_storage_sizes_do_not_count_wal_twice(tmp_path: Path) -> None:
    with Application(tmp_path, start_scheduler=False) as app:
        result = app.router.call("maintenance.status", {})
        assert result["ok"]
        size = result["data"]["storage"]
        assert size["allocatedBytes"] == size["usedPageBytes"] + size["freePageBytes"]
        assert result["data"]["databaseBytes"] == size["mainFileBytes"] + size["walBytes"] + size["shmBytes"]


def test_unpriced_minute_directory_survives_cold_archival(tmp_path: Path) -> None:
    from htu.adapters.sqlite.rollup_repo import RollupRepository
    from htu.adapters.sqlite.unpriced_store import SqliteUnpricedStore

    path = tmp_path / "data.sqlite3"
    end = Instant(AT.value + timedelta(days=1))
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        history_seed(writer)
        writer.execute(lambda w: w.connection.execute("UPDATE ledger SET unpriced=1"))
        # At 30 days the pending detail becomes a hot minute, then a cold block.
        at = Instant(AT.value + timedelta(days=30, hours=1))
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=at))
        store = SqliteUnpricedStore(writer, readers)
        before = store.archived_unpriced(AT, end)
        assert before and before[0]["buckets"] == 1
        at = Instant(AT.value + timedelta(days=32))
        while writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=at)):
            pass
        assert not readers.get().execute("SELECT 1 FROM usage_rollup").fetchone()
        assert store.archived_unpriced(AT, end) == before


def test_key_final_switch_failure_rolls_back_all_tables_and_can_resume(tmp_path: Path) -> None:
    path = old_key_database(tmp_path, 3)
    with closing(open_connection(path)) as db:

        def deny_switch(action: int, first: str | None, second: str | None, *_: object) -> int:
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ALTER_TABLE else sqlite3.SQLITE_OK

        db.set_authorizer(deny_switch)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            compact_keys.migrate_keys(
                db, migration="0026_compact_request_keys", at=format_instant(AT), checksum="test"
            )
        db.set_authorizer(None)
        assert db.execute("SELECT typeof(dedupe_key) FROM identity LIMIT 1").fetchone()[0] == "text"
        assert all(
            db.execute(f"SELECT 1 FROM _key_{table} LIMIT 1").fetchone() for table in compact_keys.TABLES
        )
        assert db.execute("SELECT COUNT(*) FROM key_upgrade_progress WHERE finished=1").fetchone()[0] == 6
        assert all(
            not db.execute(f"PRAGMA foreign_key_check({table})").fetchall() for table in compact_keys.TABLES
        )
    migrate(path)
    with closing(open_connection(path)) as db:
        assert db.execute("SELECT typeof(dedupe_key) FROM identity LIMIT 1").fetchone()[0] == "blob"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_audit_conversion_preserves_evidence_and_rejects_business_rewrites(tmp_path: Path) -> None:
    from htu.adapters.sqlite.recalc_codec import compact_audits, pack

    path = old_key_database(tmp_path, 3)
    migrate(path)
    with closing(open_connection(path)) as db:
        original = audit(db.execute("SELECT * FROM recalc_item").fetchone())
        db.execute("BEGIN IMMEDIATE")
        assert compact_audits(db) == 1
        assert audit(db.execute("SELECT * FROM recalc_item").fetchone()) == original
        db.execute("INSERT INTO temp.allow_compaction VALUES(1)")
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            db.execute(
                "UPDATE recalc_item SET payload=?", (pack(original[0], original[1] | {"pico": "999"}),)
            )
        db.rollback()


def test_cold_corrections_deep_paging_and_late_original_spending(tmp_path: Path) -> None:
    from htu.adapters.sqlite.rollup_repo import RollupRepository
    from htu.domain.wallet.model import WalletEntry

    path = tmp_path / "data.sqlite3"
    later = Instant(AT.value + timedelta(days=80))
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        writer.execute(lambda w: vendor(w, initial="100"))
        originals = []
        for i in range(18):
            stamp = Instant(AT.value + timedelta(days=i, hours=23, minutes=40))
            original = writer.execute(lambda w, i=i, stamp=stamp: consumption(w, i, amount=USD, at=stamp))
            originals.append(original)

            def correct(w, original=original, i=i):
                repo = WalletRepository(w.connection)
                repo.append_with_offsets(
                    w,
                    WalletEntry(
                        f"delta-{i}",
                        "vendor",
                        "consumption_delta",
                        -USD // 3,
                        original.occurred_at,
                        repo.next_seq(),
                        usage_ref=original.usage_ref,
                        related_entry_id=original.id,
                        recalc_batch_id=f"batch-{i}",
                    ),
                    at=original.occurred_at,
                )

            writer.execute(correct)
        store = SqliteWalletCommandStore(writer, readers)

        def pages(offset):
            result, cursor = [], None
            while True:
                page = store.wallet_entries("vendor", cursor, 3, offset_minutes=offset)
                result.extend(page["entries"])
                cursor = page["nextCursor"]
                if cursor is None:
                    return result

        from htu.adapters.sqlite.wallet_closing import close_days

        writer.execute(lambda w: close_days(w, w.connection, at=later))
        expected = {offset: pages(offset) for offset in (0, 345, -210)}
        assert len(expected[0]) > 30
        while writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later)):
            pass
        store.release_cache()
        assert {offset: pages(offset) for offset in expected} == expected
        assert readers.get().execute("SELECT SUM(correction_count) FROM wallet_archive").fetchone()[0] == 18
        # A new request from an already archived day keeps its own identity and adds money once.
        writer.execute(lambda w: consumption(w, 100, amount=2 * USD, at=originals[0].occurred_at))
        before = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        late_pages = pages(345)
        before = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        while writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later)):
            pass
        store.release_cache()
        assert pages(345) == late_pages
        assert writer.execute(lambda w: WalletRepository(w.connection).validate_rebuild(w, at=later)) == ()
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")) == before


def test_0023_audit_reads_legacy_counters_without_migrating_the_database(tmp_path: Path) -> None:
    from htu.adapters.sqlite.codex_audit import read_audit_ledger
    from htu.adapters.sqlite.counter_codec import compress
    from htu.adapters.sqlite.rollup_repo import RollupRepository

    with WriteExecutor(tmp_path / "current.sqlite3") as writer:
        history_seed(writer)
        writer.execute(
            lambda w: RollupRepository(w.connection).compact(w, at=Instant(AT.value + timedelta(days=4)))
        )
        row = writer.execute(lambda w: dict(w.connection.execute("SELECT * FROM usage_rollup").fetchone()))
    scripts = tmp_path / "0023"
    scripts.mkdir()
    for script in sorted(MIGRATIONS.glob("*.sql"))[:23]:
        shutil.copy2(script, scripts / script.name)
    path = tmp_path / "legacy.sqlite3"
    migrate(path, migrations=scripts)
    with closing(open_connection(path)) as db:
        db.execute(
            "INSERT INTO usage_archive(month,harness,start_at,end_at,records,models_json,payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                row["bucket"][:7],
                row["harness"],
                row["bucket"],
                row["bucket"],
                row["records"],
                "[]",
                compress({"v": 1, "rows": [row]}),
            ),
        )
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    original = path.read_bytes()
    report = read_audit_ledger(path, AT, Instant(AT.value + timedelta(days=1)))
    assert report.compacted_records == 100
    assert report.compacted_pico == int(row["pico"])
    assert path.read_bytes() == original


def test_startup_access_error_is_localized_and_does_not_save_preferences(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer, ReadPool(writer.path) as readers:
        changes: list[bool] = []

        def fail_enable(enabled: bool) -> None:
            changes.append(enabled)
            if enabled:
                raise PermissionError("denied")

        preferences = Preferences(SqliteCommandStore(writer, readers), FakeClock(AT), set_startup=fail_enable)
        with pytest.raises(DomainError) as error:
            preferences.update({"startAtLogin": True}, 0, "denied")
        assert error.value.code == "STARTUP_SETTING_FAILED"
        assert changes == [True, False]
        assert preferences.get().values["startAtLogin"] is False
        assert not readers.get().execute("SELECT 1 FROM operation WHERE id='denied'").fetchone()
