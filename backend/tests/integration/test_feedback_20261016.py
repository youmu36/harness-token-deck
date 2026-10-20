"""Vendor websites, durable navigation and calibration notices on isolated ledgers."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from htu.adapters.sqlite.account_book import AccountBook
from htu.adapters.sqlite.collection_store import SqliteCollectionStore
from htu.adapters.sqlite.command_store import SqliteCommandStore
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.ledger_read import LedgerReader
from htu.adapters.sqlite.reconciliation import SqliteReconciliationStore
from htu.adapters.sqlite.remote_store import SqliteRemoteStore
from htu.adapters.sqlite.rollup_repo import RollupRepository
from htu.adapters.sqlite.wallet_closing import close_days
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.application.collection.billing import BillInbox
from htu.application.preferences import Preferences
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, parse_instant
from htu.domain.statistics import Aggregator
from htu.domain.usage.model import Harness, Tokens

from .collection_helpers import AT, collect, usage
from .scan_helpers import seed_prices
from .test_remote_wallet import ACCOUNT, balance
from .test_wallet_daily import entries, page
from .test_wallet_storage import USD, consumption, vendor


def test_saved_remote_website_remains_after_removing_account(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(vendor)
        store = SqliteRemoteStore(writer, readers)
        store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
        store.execute(lambda w: store.delete_account(w, ACCOUNT.id))
        assert WalletRepository(readers.get()).vendor("vendor").url == ACCOUNT.base_url


def test_remote_vendor_mismatch_is_durable_and_clears_on_correct_link(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    clock = FakeClock(balance(0, 1).fetched_at)
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        writer.execute(seed_prices)
        writer.execute(vendor)
        remote = SqliteRemoteStore(writer, readers)
        remote.execute(lambda w: remote.save_account(w, ACCOUNT, clear=True))
        remote.execute(lambda w: remote.observe(w, balance(100 * USD)))
        event = usage(1, at=Instant(balance(0).fetched_at.value + timedelta(seconds=30)), model="gpt-6-astra")
        collect(writer, (event,))
        BillInbox(SqliteCollectionStore(writer, readers), clock).run()
        remote.execute(lambda w: remote.observe(w, balance(90 * USD, 1)))
        remote.flush_pending(Instant(clock.now_utc().value + timedelta(minutes=10)))
        assert len(SqliteReconciliationStore(readers).notices(clock.now_utc())) == 1
        remote.execute(lambda w: remote.observe(w, balance(90 * USD, 6)))
        notices = SqliteReconciliationStore(readers).notices(clock.now_utc())
        assert len(notices) == 1 and notices[0]["harness"] == "codex"
        assert notices[0]["vendorId"] == "vendor" and notices[0]["selectedGroupId"] == "g"
        assert AccountBook(readers.get()).summary("vendor")["B"] == str(90 * USD)
        writer.execute(
            lambda w: RollupRepository(w.connection).compact(w, at=parse_instant("2026-12-03T00:00:00Z"))
        )
        assert SqliteReconciliationStore(readers).notices(clock.now_utc()) == notices
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        assert SqliteReconciliationStore(readers).notices(clock.now_utc()) == notices
        writer.execute(
            lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=clock.now_utc())
        )
        assert SqliteReconciliationStore(readers).notices(clock.now_utc()) == []


def test_daily_notes_distinguish_zero_calibrations_and_exact_nonzero_delta(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer:
        writer.execute(lambda w: vendor(w, initial="100"))
        writer.execute(consumption)
        writer.execute(lambda w: close_days(w, w.connection, at=parse_instant("2026-10-01T00:00:00Z")))
        assert "23:59" in entries(page(writer, kind="consumption_daily"))[0]["note"]

        def repo(w: UnitOfWork) -> WalletRepository:
            return WalletRepository(w.connection)

        writer.execute(
            lambda w: repo(w).calibrate(
                w, "vendor", "70", expected_revision=w.revision("wallet"), at=AT, operation_id="zero"
            )
        )
        assert "23:59" in entries(page(writer, kind="consumption_daily"))[0]["note"]
        writer.execute(
            lambda w: repo(w).calibrate(
                w, "vendor", "65.123456", expected_revision=w.revision("wallet"), at=AT, operation_id="delta"
            )
        )
        note = "-4876544000000"
        assert entries(page(writer))[0]["amountPico"] == note
        writer.execute(
            lambda w: RollupRepository(w.connection).compact(w, at=parse_instant("2026-10-10T00:00:00Z"))
        )
        assert entries(page(writer))[0]["amountPico"] == note


def test_preferences_remember_route_across_restart_and_drop_legacy_matching(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        service = Preferences(SqliteCommandStore(writer, readers), FakeClock(AT))
        service.update(
            {"lastPage": "wallet", "lastRoute": "/wallet/vendor", "hiddenHarnesses": ["claude"]},
            0,
            "navigate",
        )
        writer.execute(
            lambda w: w.connection.execute("INSERT INTO preference VALUES('autoMatchProvider','true')")
        )
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        saved = Preferences(SqliteCommandStore(writer, readers), FakeClock(AT)).get().values
        assert saved["lastRoute"] == "/wallet/vendor" and saved["hiddenHarnesses"] == ["claude"]
        assert "autoMatchProvider" not in saved


def test_cache_warnings_survive_compaction_without_previous_request_lookups(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(seed_prices)
        events = (
            usage(1, tokens=Tokens(50_000, 0, 0, 1)),
            replace(usage(2, tokens=Tokens(100_000, 10_000, 0, 1)), occurred_at="2026-09-30T00:01:00.000Z"),
        )
        collect(writer, events)
        BillInbox(SqliteCollectionStore(writer, readers), FakeClock(AT)).run()
        start, end = AT, parse_instant("2026-09-30T00:02:00Z")

        def summary():
            aggregate = Aggregator(0, "minute", start, end, harness=Harness.CODEX)
            for row in LedgerReader(readers.get()).iter_rows(start, end):
                aggregate.push(row)
            return aggregate.finish()

        before = summary()
        assert [(p.cache_bust, p.cache_loss) for p in before.trend] == [(True, False), (False, True)]
        writer.execute(
            lambda w: RollupRepository(w.connection).compact(w, at=parse_instant("2026-10-10T00:00:00Z"))
        )
        assert summary() == before
