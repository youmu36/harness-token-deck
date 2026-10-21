"""Independent wallet paths, quiet journals, financial corrections and bounded reads."""

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from htd.adapters.sqlite.account_book import AccountBook
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.local_wallet import remote_summary, set_totals, summary
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.wallet_book import read
from htd.adapters.sqlite.wallet_closing import close_days
from htd.adapters.sqlite.wallet_flows import correct, dismiss_notice, entries, flush
from htd.adapters.sqlite.wallet_flows import install as install_flows
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.bootstrap import Application
from htd.domain.common.errors import DomainError
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant
from htd.domain.usage.model import Harness
from htd.domain.wallet.model import UsageRef, WalletEntry
from htd.ipc.dto.maintenance import RecalcPreview, RecalcResponse
from htd.ipc.dto.wallet import AccountEntriesResponse, WalletChangeResponse, WalletListResponse

from backend.tests.contracts.command_helpers import failure, invoke
from backend.tests.contracts.management_helpers import vendor as create_vendor
from backend.tests.integration.collection_helpers import AT
from backend.tests.integration.test_account_book import setup
from backend.tests.integration.test_remote_wallet import ACCOUNT, balance
from backend.tests.integration.test_wallet_storage import USD, consumption, vendor


def at(minute: int) -> Instant:
    return balance(0, minute).fetched_at


def local(work: UnitOfWork, minute: int, amount: int, index: int = 1) -> None:
    repo = WalletRepository(work.connection)
    repo.append_with_offsets(
        work,
        WalletEntry(
            f"request-{index}",
            "vendor",
            "consumption",
            amount,
            at(minute),
            repo.next_seq(),
            usage_ref=UsageRef(Harness.CODEX, f"{index:064x}"),
        ),
        at=at(minute),
    )


def page(work: UnitOfWork, minute: int, scope: str = "local") -> dict[str, Any]:
    return entries(work.connection, "vendor", scope, None, 50, at(minute))


def reprice(work: UnitOfWork, amount: int, minute: int, index: int = 1) -> None:
    repo = WalletRepository(work.connection)
    original = work.connection.execute(
        "SELECT * FROM wallet_entry WHERE id=?", (f"request-{index}",)
    ).fetchone()
    from htd.domain.common.time import parse_instant

    repo.append_with_offsets(
        work,
        WalletEntry(
            str(uuid4()),
            "vendor",
            "consumption_delta",
            amount,
            parse_instant(original["occurred_at"]),
            repo.next_seq(),
            usage_ref=UsageRef(Harness.CODEX, f"{index:064x}"),
            related_entry_id=original["id"],
            recalc_batch_id="test-batch",
        ),
        at=at(minute),
    )


def calibrate_flow(work: UnitOfWork, row: dict[str, Any], amount: int, minute: int) -> None:
    correct(
        work.connection,
        work,
        "vendor",
        {
            "entryId": row["id"],
            "scope": "local",
            "expectedEntryRevision": row["entryRevision"],
            "R": "0",
            "C": str(amount),
            "note": "校准消费",
        },
        str(uuid4()),
        at(minute),
    )


@pytest.mark.parametrize("reliable_used", [False, True])
def test_pending_local_and_remote_amounts_move_to_posted_totals_once(
    tmp_path: Path, reliable_used: bool
) -> None:
    from htd.adapters.sqlite.wallet_pending import display

    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = setup(writer, readers, "10", used=0 if reliable_used else None)
        writer.execute(
            lambda w: WalletRepository(w.connection).top_up(w, "vendor", "3", at=at(1), operation_id="top")
        )
        writer.execute(lambda w: local(w, 1, 2 * USD))
        store.execute(
            lambda w: store.observe(
                w,
                replace(
                    balance(11 * USD, 5),
                    cumulative_used_pico=2 * USD if reliable_used else None,
                    usage_scope="newapi:user:used_quota:v1" if reliable_used else None,
                ),
            )
        )
        for scope, current in (("local", summary), ("remote", remote_summary)):
            result = display(readers.get(), "vendor", scope, current(readers.get(), "vendor"))
            assert result is not None
            assert result["postedR"] == result["postedC"] == "0"
            assert result["pendingTopUpPico"] == str(3 * USD)
            assert result["pendingConsumptionPico"] == str(2 * USD)
        # Refreshing the panel reads the small staging records, not request history.
        statements: list[str] = []
        readers.get().set_trace_callback(statements.append)
        display(readers.get(), "vendor", "remote", remote_summary(readers.get(), "vendor"))
        readers.get().set_trace_callback(None)
        assert not any("wallet_entry" in s or "wallet_archive" in s for s in statements)
        writer.execute(lambda w: w.connection.execute("DELETE FROM remote_wallet_pending_estimate"))
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = SqliteRemoteStore(writer, readers)
        store.begin_run(at(7))
        pending = display(readers.get(), "vendor", "remote", remote_summary(readers.get(), "vendor"))
        assert pending and pending["pendingConsumptionPico"] == str(2 * USD)
        store.flush_pending(at(11))
        for scope, current in (("local", summary), ("remote", remote_summary)):
            result = display(readers.get(), "vendor", scope, current(readers.get(), "vendor"))
            assert result is not None
            assert result["postedR"] == str(3 * USD) and result["postedC"] == str(2 * USD)
            assert result["pendingTopUpPico"] == result["pendingConsumptionPico"] == "0"
        store.flush_pending(at(20))
        remote = remote_summary(readers.get(), "vendor")
        assert remote is not None and remote["C"] == str(2 * USD)


def test_remote_pending_estimate_absorbs_late_billing_once_per_batch(tmp_path: Path) -> None:
    from htd.adapters.sqlite.wallet_pending import display

    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10")
        store.execute(lambda w: store.observe(w, balance(11 * USD, 5)))
        statements: list[str] = []
        writer.execute(lambda w: w.connection.set_trace_callback(statements.append))

        def bill(work: UnitOfWork) -> None:
            for index in range(1, 501):
                local(work, 1, USD // 250, index)

        writer.execute(bill)
        result = display(readers.get(), "vendor", "remote", remote_summary(readers.get(), "vendor"))
        assert result and result["pendingConsumptionPico"] == str(2 * USD)
        assert result["pendingTopUpPico"] == str(3 * USD)
        assert (
            len(
                [s for s in statements if s.startswith("UPDATE remote_wallet_pending_estimate SET estimate=")]
            )
            == 1
        )
        writer.execute(lambda w: w.connection.set_trace_callback(None))


@pytest.mark.parametrize("note_only", [False, True])
def test_reprice_restates_the_original_flow_before_manual_calibration(
    tmp_path: Path, note_only: bool
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(vendor)
        writer.execute(lambda w: local(w, 1, 2 * USD))
        writer.execute(lambda w: flush(w, w.connection, at(11)))
        row = writer.execute(lambda w: page(w, 11))["entries"][0]
        if note_only:
            writer.execute(lambda w: calibrate_flow(w, row, 2 * USD, 12))
        writer.execute(lambda w: reprice(w, USD, 13))
        row = next(r for r in writer.execute(lambda w: page(w, 13))["entries"] if r["id"] == row["id"])
        assert row["C"] == row["estimatedConsumptionPico"] == str(3 * USD)
        assert not row["manuallyCalibrated"] and row["noticeId"] is None
        writer.execute(lambda w: calibrate_flow(w, row, 2 * USD, 14))
        current = writer.execute(lambda w: summary(w.connection, "vendor"))
        assert current["C"] == str(2 * USD) and current["B"] == str(-2 * USD)


@pytest.mark.parametrize("delta", [USD, -USD])
def test_manual_flow_amount_survives_reprice_restart_and_journal_retention(
    tmp_path: Path, delta: int
) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        writer.execute(lambda w: local(w, 1, 2 * USD))
        writer.execute(lambda w: flush(w, w.connection, at(11)))
        row = writer.execute(lambda w: page(w, 11))["entries"][0]
        writer.execute(lambda w: calibrate_flow(w, row, USD + 123, 12))
        writer.execute(lambda w: flush(w, w.connection, at(22)))
        before = writer.execute(lambda w: summary(w.connection, "vendor"))
        count = writer.execute(
            lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_flow").fetchone()[0]
        )
    with WriteExecutor(path) as writer:
        writer.execute(lambda w: reprice(w, delta, 23))
        after = writer.execute(lambda w: summary(w.connection, "vendor"))
        assert after == before
        changed = next(r for r in writer.execute(lambda w: page(w, 23))["entries"] if r["id"] == row["id"])
        assert changed["C"] == str(USD + 123)
        assert changed["estimatedConsumptionPico"] == str(2 * USD + delta)
        assert not writer.execute(
            lambda w: w.connection.execute("SELECT 1 FROM wallet_flow_pending").fetchone()
        )
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_flow").fetchone()[0])
            == count
        )
        writer.execute(lambda w: close_days(w, w.connection, at=at(3000)))
        assert writer.execute(lambda w: read(w.connection, "vendor", "local", "2026-09", at(3000)))[
            "C"
        ] == str(USD + 123)
        writer.execute(lambda w: flush(w, w.connection, at(31 * 1440)))
        writer.execute(lambda w: reprice(w, -delta, 31 * 1440 + 1))
        assert writer.execute(lambda w: summary(w.connection, "vendor")) == before
        assert not writer.execute(
            lambda w: w.connection.execute("SELECT 1 FROM wallet_flow_pending").fetchone()
        )


def test_pending_reprice_is_folded_into_one_flow_and_waits_for_actual_receipt_time(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(vendor)
        writer.execute(lambda w: local(w, 1, 2 * USD))
        writer.execute(lambda w: reprice(w, USD, 5))
        writer.execute(lambda w: flush(w, w.connection, at(11)))
        assert not writer.execute(lambda w: page(w, 11))["entries"]
        writer.execute(lambda w: flush(w, w.connection, at(15)))
        rows = writer.execute(lambda w: page(w, 15))["entries"]
        assert len(rows) == 1 and rows[0]["kind"] == "consumption" and rows[0]["C"] == str(3 * USD)


def test_manual_override_does_not_capture_interleaved_requests_on_other_days(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(vendor)
        writer.execute(lambda w: local(w, 1, USD, 1))
        writer.execute(lambda w: local(w, 1441, USD, 2))
        writer.execute(lambda w: local(w, 2, USD, 3))
        writer.execute(lambda w: flush(w, w.connection, at(1451)))
        row = next(r for r in writer.execute(lambda w: page(w, 1451))["entries"] if r["C"] == str(2 * USD))
        writer.execute(lambda w: calibrate_flow(w, row, 3 * USD, 1452))
        writer.execute(lambda w: reprice(w, USD, 1453, 2))
        assert writer.execute(lambda w: summary(w.connection, "vendor"))["C"] == str(5 * USD)


def test_dismissed_notice_is_destroyed_and_only_new_calibration_creates_a_new_notice(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        writer.execute(lambda w: local(w, 1, 2 * USD))
        writer.execute(lambda w: flush(w, w.connection, at(11)))
        row = writer.execute(lambda w: page(w, 11))["entries"][0]
        writer.execute(lambda w: calibrate_flow(w, row, 3 * USD, 12))
        row = next(r for r in writer.execute(lambda w: page(w, 12))["entries"] if r["id"] == row["id"])
        first_notice = row["noticeId"]
        assert first_notice
        before = writer.execute(lambda w: summary(w.connection, "vendor"))
        writer.execute(lambda w: dismiss_notice(w.connection, w, "vendor", row["id"], first_notice))
        assert writer.execute(lambda w: summary(w.connection, "vendor")) == before
        assert not writer.execute(
            lambda w: w.connection.execute("SELECT 1 FROM wallet_flow_notice").fetchone()
        )
    with WriteExecutor(path) as writer:
        writer.execute(lambda w: reprice(w, USD // 2, 13))
        row = next(r for r in writer.execute(lambda w: page(w, 13))["entries"] if r["id"] == row["id"])
        assert row["noticeId"] is None and row["estimatedConsumptionPico"] != row["C"]
        writer.execute(lambda w: calibrate_flow(w, row, 4 * USD, 14))
        row = next(r for r in writer.execute(lambda w: page(w, 14))["entries"] if r["id"] == row["id"])
        assert row["noticeId"] and row["noticeId"] != first_notice
        writer.execute(lambda w: dismiss_notice(w.connection, w, "vendor", row["id"], first_notice))
        assert (
            next(r for r in writer.execute(lambda w: page(w, 14))["entries"] if r["id"] == row["id"])[
                "noticeId"
            ]
            == row["noticeId"]
        )
        writer.execute(lambda w: reprice(w, 3 * USD // 2, 15))
        row = next(r for r in writer.execute(lambda w: page(w, 15))["entries"] if r["id"] == row["id"])
        assert row["C"] == row["estimatedConsumptionPico"] == str(4 * USD) and row["noticeId"] is None


def test_recalc_preview_and_notice_command_preserve_manually_calibrated_money(tmp_path: Path) -> None:
    from backend.tests.contracts.test_recalc_commands import setup as setup_recalc

    clock = FakeClock(AT)
    with Application(tmp_path / "data", clock=clock, start_scheduler=False) as app:
        params, supplier = setup_recalc(app)
        clock.advance(600_000)
        SqliteRemoteStore(app.writer, app.readers).flush_pending(clock.now_utc())
        request = {"vendorId": supplier, "scope": "local"}
        row = next(
            r
            for r in invoke(app, "wallet.accountEntries", request, AccountEntriesResponse).entries
            if r.kind == "consumption"
        )
        invoke(
            app,
            "wallet.correctEntry",
            {
                **request,
                "entryId": row.id,
                "R": "0",
                "C": str(2 * USD),
                "expectedEntryRevision": row.entryRevision,
                "operationId": str(uuid4()),
            },
            WalletChangeResponse,
        )
        preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
        assert preview.oldTotal == str(USD) and preview.newTotal == str(4 * USD)
        assert preview.vendors[0].consumptionDelta == preview.vendors[0].balanceDelta == "0"
        before = app.writer.execute(lambda w: summary(w.connection, supplier))
        invoke(
            app,
            "maintenance.commitRecalc",
            {
                **params,
                "fingerprint": preview.fingerprint,
                "operationId": str(uuid4()),
            },
            RecalcResponse,
        )
        assert app.writer.execute(lambda w: summary(w.connection, supplier)) == before
        row = next(
            r
            for r in invoke(app, "wallet.accountEntries", request, AccountEntriesResponse).entries
            if r.id == row.id
        )
        assert str(2 * USD) == row.C and row.estimatedConsumptionPico == str(4 * USD) and row.noticeId
        dismiss = {
            "vendorId": supplier,
            "entryId": row.id,
            "noticeId": row.noticeId,
            "operationId": str(uuid4()),
        }
        result = invoke(app, "wallet.dismissNotice", dismiss, WalletChangeResponse)
        assert result.changed and invoke(app, "wallet.dismissNotice", dismiss, WalletChangeResponse) == result
        assert app.writer.execute(lambda w: summary(w.connection, supplier)) == before
    with Application(tmp_path / "data", clock=clock, start_scheduler=False) as app:
        rows = invoke(app, "wallet.accountEntries", request, AccountEntriesResponse).entries
        assert next(r for r in rows if r.id == row.id).noticeId is None


def test_unpriced_recovery_preserves_a_calibrated_partial_consumption_flow(tmp_path: Path) -> None:
    from htd.domain.usage.model import Tokens

    from backend.tests.contracts.maintenance_helpers import add_usage
    from backend.tests.contracts.management_helpers import group, price, wallet
    from backend.tests.contracts.test_unpriced_commands import fill, recalculate
    from backend.tests.integration.collection_helpers import usage

    clock = FakeClock(AT)
    with Application(tmp_path / "data", clock=clock, start_scheduler=False) as app:
        g, version = group(app)
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=version,
            models=[
                {
                    "modelId": "Model",
                    "input": None,
                    "cache": "0",
                    "output": "0",
                    "cacheWrite": "3",
                }
            ],
        )
        price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=version)
        supplier = create_vendor(app)
        wallet(
            app,
            "wallet.linkGroups",
            vendorId=supplier.id,
            groupIds=[g],
            expectedRevision=app.revisions()["wallet"],
        )
        clock.advance(60_000)
        add_usage(app, [usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 100_000, 0, 0))])
        clock.advance(600_000)
        store = SqliteRemoteStore(app.writer, app.readers)
        store.flush_pending(clock.now_utc())
        request = {"vendorId": supplier.id, "scope": "local"}
        rows = invoke(app, "wallet.accountEntries", request, AccountEntriesResponse).entries
        row = next(r for r in rows if r.kind == "consumption")
        assert str(3 * USD // 10) == row.C
        invoke(
            app,
            "wallet.correctEntry",
            {
                **request,
                "entryId": row.id,
                "R": "0",
                "C": str(2 * USD),
                "expectedEntryRevision": row.entryRevision,
                "operationId": str(uuid4()),
            },
            WalletChangeResponse,
        )
        clock.advance(600_000)
        store.flush_pending(clock.now_utc())
        before = app.writer.execute(lambda w: summary(w.connection, supplier.id))
        with app.readers.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM wallet_flow").fetchone()[0]
        fill(app, version)
        assert recalculate(app).recalculated == 1
        assert app.writer.execute(lambda w: summary(w.connection, supplier.id)) == before
        rows = invoke(app, "wallet.accountEntries", request, AccountEntriesResponse).entries
        row = next(r for r in rows if r.id == row.id)
        # Cache creation is removed from ordinary input before pricing: 0.9*2 + 0.1*3.
        assert str(2 * USD) == row.C and row.estimatedConsumptionPico == str(21 * USD // 10)
        with app.readers.connection() as db:
            assert not db.execute("SELECT 1 FROM wallet_flow_pending").fetchone()
            assert db.execute("SELECT COUNT(*) FROM wallet_flow").fetchone()[0] == count


def test_local_burst_is_coalesced_after_ten_minutes_without_remote_poll_delay(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10", used=0)
        writer.execute(lambda w: local(w, 1, USD, 1))
        writer.execute(lambda w: local(w, 2, USD, 2))
        store.execute(
            lambda w: store.observe(
                w,
                replace(
                    balance(8 * USD, 5), cumulative_used_pico=2 * USD, usage_scope="newapi:user:used_quota:v1"
                ),
            )
        )
        store.flush_pending(at(11))
        assert not any(
            row["kind"] == "consumption" for row in writer.execute(lambda w: page(w, 11))["entries"]
        )
        store.flush_pending(at(12))
        local_rows = writer.execute(lambda w: page(w, 12))["entries"]
        remote_rows = writer.execute(lambda w: page(w, 12, "remote"))["entries"]
        assert len([row for row in local_rows if row["kind"] == "consumption"]) == 1
        assert remote_rows[0]["C"] == str(2 * USD)
        assert (remote_rows[0]["beforeB"], remote_rows[0]["afterB"]) == (str(10 * USD), str(8 * USD))
        AccountEntriesResponse.model_validate({"entries": remote_rows, "nextCursor": None, "revision": 0})
        counts = writer.execute(
            lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_flow").fetchone()[0]
        )
        store.flush_pending(at(30))
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_flow").fetchone()[0])
            == counts
        )


def test_local_top_up_never_creates_or_matches_remote_funding(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10", used=0)
        before = writer.execute(lambda w: remote_summary(w.connection, "vendor"))
        writer.execute(
            lambda w: WalletRepository(w.connection).top_up(
                w, "vendor", "3", at=at(1), operation_id="local-topup"
            )
        )
        assert writer.execute(lambda w: remote_summary(w.connection, "vendor")) == before
        assert (
            writer.execute(
                lambda w: w.connection.execute(
                    "SELECT COUNT(*) FROM account_posting WHERE kind='manual_top_up'"
                ).fetchone()[0]
            )
            == 0
        )
        store.execute(
            lambda w: store.observe(
                w,
                replace(
                    balance(13 * USD, 5), cumulative_used_pico=0, usage_scope="newapi:user:used_quota:v1"
                ),
            )
        )
        store.flush_pending(at(15))
        remote = writer.execute(lambda w: remote_summary(w.connection, "vendor"))
        assert remote is not None and remote["R"] == str(3 * USD)
        assert writer.execute(lambda w: summary(w.connection, "vendor"))["R"] == str(3 * USD)


@pytest.mark.parametrize("scope", ["local", "remote"])
def test_correcting_a_flow_restates_original_day_and_preserves_other_wallet(
    tmp_path: Path, scope: str
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10", used=0)
        writer.execute(lambda w: local(w, 1, 2 * USD))
        store.execute(
            lambda w: store.observe(
                w,
                replace(
                    balance(8 * USD, 5), cumulative_used_pico=2 * USD, usage_scope="newapi:user:used_quota:v1"
                ),
            )
        )
        store.flush_pending(at(15))
        row = next(
            row
            for row in writer.execute(lambda w: page(w, 15, scope))["entries"]
            if row["kind"] == "consumption"
        )
        local_before = writer.execute(lambda w: summary(w.connection, "vendor"))
        remote_before = writer.execute(lambda w: remote_summary(w.connection, "vendor"))
        assert remote_before is not None
        values: dict[str, object] = {
            "entryId": row["id"],
            "scope": scope,
            "expectedEntryRevision": 1,
            "R": "0",
            "C": str(USD + 123),
            "note": "校准为准确消费",
        }
        writer.execute(lambda w: correct(w.connection, w, "vendor", values, "fix", at(1500)))
        if scope == "local":
            assert writer.execute(lambda w: remote_summary(w.connection, "vendor")) == remote_before
            result = writer.execute(lambda w: summary(w.connection, "vendor"))
            assert int(result["B"]) - int(local_before["B"]) == USD - 123
        else:
            assert writer.execute(lambda w: summary(w.connection, "vendor")) == local_before
            result = writer.execute(lambda w: remote_summary(w.connection, "vendor"))
            assert result is not None
            assert result["B"] == remote_before["B"]
        assert result["C"] == str(USD + 123)
        with pytest.raises(DomainError, match="已变化"):
            writer.execute(lambda w: correct(w.connection, w, "vendor", values, "stale", at(1501)))
        for _ in range(3):
            writer.execute(lambda w: close_days(w, w.connection, at=at(3000)))
        book = writer.execute(lambda w: read(w.connection, "vendor", scope, "2026-09", at(3000)))
        assert book["C"] == str(USD + 123) and book["A"] == "0"
        assert len(book["days"]) == 1  # correction did not invent spending on the following day.


def test_reprice_after_local_calibration_changes_local_balance_with_or_without_remote(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(lambda w: vendor(w, initial="100"))
        original = writer.execute(lambda w: consumption(w, amount=2 * USD))
        writer.execute(
            lambda w: set_totals(
                w.connection,
                w,
                "vendor",
                {"B": str(90 * USD), "expectedWalletRevision": w.revision("wallet")},
                "cal",
                at(65),
            )
        )
        before = writer.execute(lambda w: summary(w.connection, "vendor"))

        def reprice(work: UnitOfWork) -> None:
            repo = WalletRepository(work.connection)
            repo.append_with_offsets(
                work,
                WalletEntry(
                    "delta",
                    "vendor",
                    "consumption_delta",
                    USD,
                    original.occurred_at,
                    repo.next_seq(),
                    usage_ref=original.usage_ref,
                    related_entry_id=original.id,
                    recalc_batch_id="test-batch",
                ),
                at=at(70),
            )

        writer.execute(reprice)
        after = writer.execute(lambda w: summary(w.connection, "vendor"))
        assert int(after["C"]) - int(before["C"]) == USD
        assert int(after["B"]) - int(before["B"]) == -USD


def test_retention_restart_and_read_queries_do_not_change_money_or_daily_books(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        writer.execute(
            lambda w: WalletRepository(w.connection).top_up(
                w, "vendor", "10.123456", at=at(0), operation_id="top"
            )
        )
        writer.execute(lambda w: local(w, 1, USD))
    with WriteExecutor(path) as writer:
        writer.execute(lambda w: flush(w, w.connection, at(11)))
        assert len(writer.execute(lambda w: page(w, 11))["entries"]) == 2
        for _ in range(3):
            writer.execute(lambda w: close_days(w, w.connection, at=at(3000)))
        before = writer.execute(lambda w: summary(w.connection, "vendor"))
        day_book = writer.execute(lambda w: read(w.connection, "vendor", "local", "2026-09", at(3000)))
        writer.execute(lambda w: flush(w, w.connection, at(31 * 1440)))
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_flow").fetchone()[0])
            == 0
        )
        assert writer.execute(lambda w: summary(w.connection, "vendor")) == before
        assert (
            writer.execute(lambda w: read(w.connection, "vendor", "local", "2026-09", at(31 * 1440)))["days"]
            == day_book["days"]
        )


def test_filters_precision_atomic_replay_and_top_up_contract(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path / "data", clock=clock, start_scheduler=False) as app:
        model = create_vendor(app)
        assert (
            failure(
                app,
                "wallet.setLocalTotals",
                {
                    "vendorId": model.id,
                    "R": "1",
                    "expectedWalletRevision": app.revisions()["wallet"],
                    "operationId": str(uuid4()),
                },
            ).code
            == "LOCAL_TOP_UP_REQUIRED"
        )
        top_up = invoke(
            app,
            "wallet.topUp",
            {"vendorId": model.id, "walletScope": "local", "amount": "10", "operationId": str(uuid4())},
            WalletChangeResponse,
        )
        assert top_up.vendor and top_up.vendor.localWallet
        assert top_up.vendor.localWallet.postedR == "0"
        assert top_up.vendor.localWallet.pendingTopUpPico == str(10 * USD)
        clock.advance(600_000)
        SqliteRemoteStore(app.writer, app.readers).flush_pending(clock.now_utc())
        request = {
            "vendorId": model.id,
            "scope": "local",
            "kind": "top_up",
            "startDay": "2026-09-30",
            "endDay": "2026-09-30",
        }
        result = invoke(app, "wallet.accountEntries", request, AccountEntriesResponse)
        assert len(result.entries) == 1
        row = result.entries[0]
        correction = {
            "vendorId": model.id,
            "scope": "local",
            "entryId": row.id,
            "R": "11234567890123",
            "C": "0",
            "expectedEntryRevision": row.entryRevision,
            "operationId": str(uuid4()),
        }
        first = invoke(app, "wallet.correctEntry", correction, WalletChangeResponse)
        assert invoke(app, "wallet.correctEntry", correction, WalletChangeResponse) == first
        assert first.vendor is not None and first.vendor.localWallet is not None
        assert first.vendor.localWallet.R == "11234567890123"
        assert not invoke(
            app, "wallet.accountEntries", {**request, "kind": "consumption"}, AccountEntriesResponse
        ).entries
        assert (
            failure(app, "wallet.accountEntries", {**request, "startDay": "2026-02-30"}).code
            == "ACCOUNT_DATE_INVALID"
        )
        with app.readers.connection() as db:
            changes = db.total_changes
            for _ in range(20):
                entries(db, model.id, "local", None, 50, clock.now_utc())
            assert db.total_changes == changes


def test_visibility_is_per_vendor_persistent_and_financially_neutral(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path / "data", clock=clock, start_scheduler=False) as app:
        model = create_vendor(app)
        invoke(
            app,
            "wallet.topUp",
            {"vendorId": model.id, "walletScope": "local", "amount": "10", "operationId": str(uuid4())},
            WalletChangeResponse,
        )
        before = WalletListResponse.model_validate(app.wallet.list(True)).vendors[0]
        args = {"vendorId": model.id, "mode": "remote", "operationId": str(uuid4())}
        first = invoke(app, "wallet.setVisibility", args, WalletChangeResponse)
        assert invoke(app, "wallet.setVisibility", args, WalletChangeResponse) == first
        assert (
            first.vendor is not None
            and first.vendor.localWallet is not None
            and before.localWallet is not None
        )
        assert first.vendor.walletVisibility == "remote"
        assert first.vendor.localWallet == before.localWallet
    with Application(tmp_path / "data", clock=clock, start_scheduler=False) as app:
        assert (
            WalletListResponse.model_validate(app.wallet.list(True)).vendors[0].walletVisibility == "remote"
        )


@pytest.mark.parametrize("unpriced", [False, True])
def test_repricing_changes_local_wallet_but_never_settled_remote_money(
    tmp_path: Path, unpriced: bool
) -> None:
    from backend.tests.contracts.test_recalc_commands import setup as setup_recalc
    from backend.tests.contracts.test_unpriced_commands import fill, recalculate
    from backend.tests.contracts.test_unpriced_commands import setup as setup_unpriced

    clock = FakeClock(AT)
    with Application(tmp_path / "data", clock=clock, start_scheduler=False) as app:
        if unpriced:
            _, version, supplier = setup_unpriced(app, clock)
            fill(app, version)
            params = {}
        else:
            params, supplier = setup_recalc(app)
        store = SqliteRemoteStore(app.writer, app.readers)
        store.execute(lambda w: store.save_account(w, replace(ACCOUNT, vendor_id=supplier), clear=True))
        store.execute(
            lambda w: store.observe(
                w,
                replace(
                    balance(10 * USD),
                    fetched_at=clock.now_utc(),
                    cumulative_used_pico=0,
                    usage_scope="newapi:user:used_quota:v1",
                ),
            )
        )
        clock.advance(300_000)
        store.execute(
            lambda w: store.observe(
                w,
                replace(
                    balance(9 * USD),
                    fetched_at=clock.now_utc(),
                    cumulative_used_pico=USD,
                    usage_scope="newapi:user:used_quota:v1",
                ),
            )
        )
        clock.advance(600_000)
        store.flush_pending(clock.now_utc())
        before = app.writer.execute(lambda w: remote_summary(w.connection, supplier))
        local_before = app.writer.execute(lambda w: summary(w.connection, supplier))
        if unpriced:
            assert recalculate(app).recalculated == 1
        else:
            preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
            assert preview.vendors[0].balanceDelta == "-3000000000000"
            invoke(
                app,
                "maintenance.commitRecalc",
                {**params, "fingerprint": preview.fingerprint, "operationId": str(uuid4())},
                RecalcResponse,
            )
        local_after = app.writer.execute(lambda w: summary(w.connection, supplier))
        assert int(local_after["C"]) > int(local_before["C"])
        assert int(local_after["B"]) - int(local_before["B"]) == int(local_before["C"]) - int(
            local_after["C"]
        )
        assert app.writer.execute(lambda w: remote_summary(w.connection, supplier)) == before


def test_a_billing_batch_writes_one_coalesced_pending_row_and_failed_batches_leave_none(
    tmp_path: Path,
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(vendor)
        statements: list[str] = []
        writer.execute(lambda w: w.connection.set_trace_callback(statements.append))

        def batch(work: UnitOfWork) -> None:
            for index in range(1, 501):
                local(work, 1, 123, index)

        writer.execute(batch)
        assert len([sql for sql in statements if sql.startswith("INSERT INTO wallet_flow_pending")]) == 1
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_flow_pending").fetchone()[0]
            )
            == 1
        )
        writer.execute(lambda w: flush(w, w.connection, at(11)))
        assert writer.execute(lambda w: page(w, 11))["entries"][0]["C"] == str(500 * 123)

        def fail_batch(work: UnitOfWork) -> None:
            local(work, 20, USD, 501)
            raise ValueError("rollback")

        with pytest.raises(ValueError):
            writer.execute(fail_batch)
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_flow_pending").fetchone()[0]
            )
            == 0
        )


def test_remote_correction_projection_catches_up_after_older_binary_writes_once(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        setup(writer, readers, "10", used=0)
        before = writer.execute(lambda w: remote_summary(w.connection, "vendor"))

        def old_write(work: UnitOfWork) -> None:
            segment = AccountBook(work.connection).active("vendor")
            assert segment is not None
            work.connection.execute(
                "INSERT INTO account_posting(id,vendor_id,segment_id,occurred_at,posted_at,kind,r,c,a,"
                "note,operation_id) VALUES('old','vendor',?,?,?,'consumption_correction',"
                "'0',? ,?,'old','old')",
                (segment["id"], "2026-09-30T01:00:00.000Z", "2026-09-30T01:00:00.000Z", str(USD), str(USD)),
            )
            install_flows(work.connection)

        writer.execute(old_write)
        after = writer.execute(lambda w: remote_summary(w.connection, "vendor"))
        assert after is not None and before is not None
        assert int(after["C"]) - int(before["C"]) == USD
        writer.execute(lambda w: install_flows(w.connection))
        assert writer.execute(lambda w: remote_summary(w.connection, "vendor")) == after
