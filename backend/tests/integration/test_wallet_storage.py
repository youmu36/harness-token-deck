"""Wallet restart recovery, atomic vouchers, immutable history, and repair guarantees."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.application.ports import BilledUsage
from htd.application.ports import UnitOfWork as WorkPort
from htd.domain.billing import CostEstimate
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant
from htd.domain.usage.model import Harness
from htd.domain.wallet.model import UsageRef, WalletEntry

from backend.tests.integration.collection_helpers import AT, collect, usage
from backend.tests.integration.test_pricing_storage import create

USD = 10**12
LATER = Instant(AT.value + timedelta(hours=1))


def vendor(work: UnitOfWork, *, initial: str | None = None) -> None:
    WalletRepository(work.connection).create_vendor(
        work, vendor_id="vendor", name="  Shop  ", at=AT, operation_id="create", initial_balance=initial
    )


def recharge(work: UnitOfWork) -> WalletEntry:
    return WalletRepository(work.connection).top_up(work, "vendor", "100", at=AT, operation_id="top")


def consumption(
    work: UnitOfWork, index: int = 1, *, amount: int = 30 * USD, at: Instant = LATER
) -> WalletEntry:
    repo = WalletRepository(work.connection)
    record = WalletEntry(
        f"consume-{index}",
        "vendor",
        "consumption",
        amount,
        at,
        repo.next_seq(),
        usage_ref=UsageRef(Harness.CODEX, f"{index:064x}"),
    )
    return repo.append_with_offsets(work, record, at=LATER)


def calibrate(work: UnitOfWork, target: str = "65", *, operation: str = "calibrate") -> WalletEntry:
    repo = WalletRepository(work.connection)
    return repo.calibrate(
        work, "vendor", target, expected_revision=work.revision("wallet"), at=LATER, operation_id=operation
    )


def test_wallet_restart_receipts_totals_and_calibration(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        top = writer.execute(recharge)
        writer.execute(consumption)
        audit = writer.execute(calibrate)
        expected = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        assert (expected.r, expected.c, expected.a, expected.b) == (100 * USD, 30 * USD, -5 * USD, 65 * USD)
    with WriteExecutor(path) as writer:
        assert writer.execute(recharge) == top
        # Original revision is part of the command; replay succeeds despite later balance changes.
        replay = writer.execute(
            lambda w: WalletRepository(w.connection).calibrate(
                w, "vendor", "65", expected_revision=3, at=AT, operation_id="calibrate"
            )
        )
        assert replay == audit
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")) == expected
        assert (
            writer.execute(lambda w: WalletRepository(w.connection).calibrations("vendor"))[0].watermark_seq
            == 2
        )


@pytest.mark.parametrize("initial,expected", [("-0.5", -USD // 2), ("", 0), (None, 0), ("0", 0)])
def test_initial_balance_and_zero_calibration(tmp_path: Path, initial: str | None, expected: int) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(lambda w: vendor(w, initial=initial))
        totals = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        assert totals.b == expected
        assert (totals.last_calibrated_at is not None) == (initial is not None and initial != "")
        original = writer.execute(
            lambda w: WalletRepository(w.connection).create_vendor(
                w, vendor_id="vendor", name="Shop", at=LATER, operation_id="create", initial_balance=initial
            )
        )
        assert original.created_at == AT


@pytest.mark.parametrize(
    "table,verb",
    [
        ("wallet_entry", "UPDATE"),
        ("wallet_entry", "DELETE"),
        ("wallet_entry", "REPLACE"),
        ("calibration", "UPDATE"),
        ("calibration", "DELETE"),
        ("vendor_link", "UPDATE"),
        ("vendor_link", "DELETE"),
    ],
)
def test_wallet_append_protection(tmp_path: Path, table: str, verb: str) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(create)
        writer.execute(lambda w: vendor(w, initial="0"))
        writer.execute(lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=AT))
        statement = (
            f"DELETE FROM {table}"
            if verb == "DELETE"
            else f"UPDATE {table} SET " + ("seq=seq" if table != "calibration" else "at=at")
        )
        if verb == "REPLACE":
            statement = "INSERT OR REPLACE INTO wallet_entry SELECT * FROM wallet_entry"
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            writer.execute(lambda w: w.connection.execute(statement))


@pytest.mark.parametrize("failure", ["wallet_total", "calibration", "operation"])
def test_voucher_and_totals_rollback(tmp_path: Path, failure: str) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        before = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        event = "UPDATE" if failure == "wallet_total" else "INSERT"
        writer.execute(
            lambda w: w.connection.execute(
                f"CREATE TRIGGER fail BEFORE {event} ON {failure} BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            writer.execute(calibrate if failure == "calibration" else recharge)
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")) == before
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_entry").fetchone()[0])
            == 0
        )


def test_duplicate_consumption_unique_constraint(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        original = writer.execute(consumption)

        def duplicate(work: UnitOfWork) -> None:
            repo = WalletRepository(work.connection)
            repo.append(work, replace(original, id="duplicate", seq=repo.next_seq()))

        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            writer.execute(duplicate)
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")).c == 30 * USD


@pytest.mark.parametrize("damage", ["amount", "missing", "malformed"])
def test_rebuild_repairs_totals_from_vouchers_at_startup(tmp_path: Path, damage: str) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        writer.execute(recharge)
        writer.execute(consumption)
        statements = {
            "amount": "UPDATE wallet_total SET c='999'",
            "missing": "DELETE FROM wallet_total",
            "malformed": "UPDATE wallet_total SET a='--1'",
        }
        writer.execute(lambda w: w.connection.execute(statements[damage]))
    with WriteExecutor(path) as writer:
        total = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        assert total.b == 70 * USD
        assert writer.execute(lambda w: WalletRepository(w.connection).validate_rebuild(w, at=AT)) == ()
        assert (
            writer.execute(
                lambda w: w.connection.execute(
                    "SELECT COUNT(*) FROM diagnostic_log WHERE code='wallet_totals_rebuilt'"
                ).fetchone()[0]
            )
            == 1
        )


def test_archive_name_reuse_restore_conflict_and_read_only(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        writer.execute(recharge)
        assert (
            writer.execute(lambda w: WalletRepository(w.connection).delete_vendor(w, "vendor", at=LATER))
            == "archive"
        )
        writer.execute(
            lambda w: WalletRepository(w.connection).create_vendor(
                w, vendor_id="new", name="Shop", at=LATER, operation_id="new"
            )
        )
        with pytest.raises(DomainError) as conflict:
            writer.execute(lambda w: WalletRepository(w.connection).restore_vendor(w, "vendor"))
        assert conflict.value.code == "VENDOR_NAME_CONFLICT"
        with pytest.raises(DomainError) as archived:
            writer.execute(
                lambda w: WalletRepository(w.connection).top_up(
                    w, "vendor", "1", at=LATER, operation_id="another"
                )
            )
        assert archived.value.code == "VENDOR_ARCHIVED"
        assert writer.execute(recharge).amount_pico == 100 * USD


def test_physical_delete_preserves_history_and_unlinks(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        catalog = writer.execute(create)
        writer.execute(vendor)
        link = writer.execute(lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=AT))
        assert link.seq > catalog.sequence
        assert (
            writer.execute(lambda w: WalletRepository(w.connection).delete_vendor(w, "vendor", at=LATER))
            == "delete"
        )
        links = writer.execute(lambda w: WalletRepository(w.connection).links())
        assert links[0].vendor_id == "vendor" and links[1].vendor_id is None
        assert writer.execute(lambda w: w.connection.execute("PRAGMA foreign_key_check").fetchall()) == []
        assert (
            writer.execute(lambda w: WalletRepository(w.connection).list_vendors(include_archived=True)) == ()
        )
        writer.execute(
            lambda w: WalletRepository(w.connection).create_vendor(
                w, vendor_id="new", name="Shop", at=LATER, operation_id="new"
            )
        )


def test_late_consumption_and_delta_offset_quality_rebuild(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        writer.execute(lambda w: calibrate(w, "50"))
        original = writer.execute(lambda w: consumption(w, at=AT, amount=3 * USD))

        def delta(work: UnitOfWork) -> None:
            repo = WalletRepository(work.connection)
            entry = WalletEntry(
                "delta",
                "vendor",
                "consumption_delta",
                -USD,
                AT,
                repo.next_seq(),
                usage_ref=original.usage_ref,
                related_entry_id=original.id,
                recalc_batch_id="batch",
                unpriced=True,
                incomplete=True,
            )
            repo.append_with_offsets(work, entry, at=LATER)

        writer.execute(delta)
        totals = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        assert (totals.c, totals.a, totals.b) == (2 * USD, 52 * USD, 50 * USD)
        assert totals.unpriced_count == totals.incomplete_count == 1
        assert writer.execute(lambda w: WalletRepository(w.connection).validate_rebuild(w, at=LATER)) == ()
        assert len(writer.execute(lambda w: WalletRepository(w.connection).entries("vendor")).entries) == 5


def test_stale_calibration_and_operation_conflict(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        writer.execute(recharge)
        with pytest.raises(DomainError) as stale:
            writer.execute(
                lambda w: WalletRepository(w.connection).calibrate(
                    w, "vendor", "0", expected_revision=1, at=AT, operation_id="stale"
                )
            )
        assert stale.value.code == "WALLET_REVISION_CONFLICT"
        with pytest.raises(DomainError) as conflict:
            writer.execute(
                lambda w: WalletRepository(w.connection).top_up(w, "vendor", "101", at=AT, operation_id="top")
            )
        assert conflict.value.code == "OPERATION_ID_REUSED"
        with pytest.raises(DomainError) as cross:
            writer.execute(
                lambda w: WalletRepository(w.connection).top_up(
                    w, "vendor", "1", at=AT, operation_id="create"
                )
            )
        assert cross.value.code == "OPERATION_ID_REUSED"


def test_large_amounts_and_keyset_pagination(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        for index in range(7):
            writer.execute(lambda w, index=index: consumption(w, index + 1, amount=10**30))
        before = writer.execute(lambda w: WalletRepository(w.connection).entries("vendor", limit=3))
        writer.execute(lambda w: consumption(w, 100, amount=10**30))
        after = writer.execute(
            lambda w: WalletRepository(w.connection).entries("vendor", cursor=before.next_cursor, limit=3)
        )
        assert [entry.id for entry in before.entries + after.entries] == [
            f"consume-{i}" for i in range(7, 1, -1)
        ]
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")).c == 8 * 10**30
    with closing(open_connection(path, query_only=True)) as connection:
        assert WalletRepository(connection).totals("vendor").c == 8 * 10**30


def test_billing_callback_wallet_atomicity_and_attribution(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(create)
        writer.execute(vendor)
        writer.execute(lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=LATER))
        collect(writer, (usage(1, at=AT), usage(2, at=LATER)))

        def bill(work: UnitOfWork) -> None:
            def wallet(port: WorkPort, billed: BilledUsage) -> None:
                WalletRepository(work.connection).append_consumption(port, billed, at=LATER)

            IngestRepository(work.connection).bill_batch(
                work,
                lambda event: BilledUsage(event, CostEstimate(3 * USD, False, False, "base"), group_id="g"),
                at=LATER,
                on_billed=wallet,
            )

        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail BEFORE UPDATE ON wallet_total BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            writer.execute(bill)
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]) == 0
        )
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail"))
        writer.execute(bill)
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")).c == 3 * USD
        assert len(writer.execute(lambda w: WalletRepository(w.connection).entries("vendor")).entries) == 1


def test_receipt_expiration_does_not_duplicate_top_up_or_calibration(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        original = writer.execute(recharge)
        calibration = writer.execute(calibrate)
        before = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        future = Instant(AT.value + timedelta(days=31))
        writer.execute(lambda w: MaintenanceRepository(w.connection).rotate_logs(w, at=future))
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM operation").fetchone()[0])
            == 0
        )
        assert writer.execute(recharge) == original
        assert writer.execute(calibrate) == calibration
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")) == before
        with pytest.raises(DomainError) as reused:
            writer.execute(
                lambda w: WalletRepository(w.connection).top_up(
                    w, "vendor", "1", at=future, operation_id="top"
                )
            )
        assert reused.value.code == "OPERATION_ID_REUSED"


def test_wallet_read_snapshot_is_coherent_with_concurrent_commit(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        old = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        with closing(open_connection(path, query_only=True)) as reader:
            invoked = False

            def concurrent_write(sql: str) -> None:
                nonlocal invoked
                if not invoked and "SELECT value FROM revision" in sql:
                    invoked = True
                    writer.execute(recharge)

            reader.set_trace_callback(concurrent_write)
            snapshot = WalletRepository(reader).totals("vendor")
            assert invoked and snapshot == old
            reader.set_trace_callback(None)
            assert WalletRepository(reader).totals("vendor").r == 100 * USD


def test_daily_wallet_validation_reuses_vouchers(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        writer.execute(recharge)
        writer.execute(lambda w: WalletRepository(w.connection).validate_rebuild(w, at=AT))
        writer.execute(lambda w: w.connection.execute("UPDATE wallet_total SET r='1'"))
        assert writer.execute(lambda w: WalletRepository(w.connection).validate_if_due(w, at=LATER)) == ()
        tomorrow = Instant(AT.value + timedelta(days=1))
        assert writer.execute(lambda w: WalletRepository(w.connection).validate_if_due(w, at=tomorrow)) == (
            "vendor",
        )
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")).r == 100 * USD
