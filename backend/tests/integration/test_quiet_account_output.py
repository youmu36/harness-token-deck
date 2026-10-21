"""Shared local-token quiet periods, durable batches, and bounded model throughput."""

import json
import shutil
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from htd.adapters.sqlite.account_book import AccountBook, AccountSummary
from htd.adapters.sqlite.connection import ReadPool, open_connection
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.migrate import MIGRATIONS, migrate
from htd.adapters.sqlite.output_speed import live, rate, record
from htd.adapters.sqlite.remote_repo import RemoteRepository
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.token_activity import mark
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.application.ports import BilledUsage
from htd.application.ports import UnitOfWork as WorkPort
from htd.domain.billing import CostEstimate
from htd.domain.common.time import format_instant
from htd.domain.remote.accounting import settle
from htd.domain.usage.model import Harness, Tokens, UsageEvent
from htd.domain.wallet.model import UsageRef, WalletEntry

from .collection_helpers import collect, usage
from .test_pricing_storage import create
from .test_remote_wallet import ACCOUNT, balance
from .test_wallet_storage import USD, vendor

CENT = USD // 100


def test_manual_funding_does_not_turn_micro_usage_into_an_automatic_posting() -> None:
    result = settle(10 * USD, 11 * USD - CENT + 1, CENT - 1, used=None, manual_r=USD)
    assert result.recharge == result.consumption == result.adjustment == 0
    assert result.unresolved == -CENT + 1


def setup(writer: WriteExecutor, readers: ReadPool, used: int | None = None) -> SqliteRemoteStore:
    writer.execute(vendor)
    store = SqliteRemoteStore(writer, readers)
    store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
    observe(store, 10 * USD, 0, used)
    return store


def observe(store: SqliteRemoteStore, amount: int, minute: int, used: int | None = None) -> None:
    value = replace(
        balance(amount, minute),
        cumulative_used_pico=used,
        usage_scope="newapi:user:used_quota:v1" if used is not None else None,
    )
    store.execute(lambda w: store.observe(w, value))


def summary(writer: WriteExecutor) -> AccountSummary:
    return writer.execute(lambda w: AccountBook(w.connection).summary("vendor"))


def speed(writer: WriteExecutor, minute: int, output: int | None, model: str = "model-a") -> None:
    at = balance(0, minute).fetched_at

    def append(work: UnitOfWork) -> None:
        record(
            work.connection,
            harness="codex",
            model_key=model,
            display_name=model,
            output=output,
            occurred_at=format_instant(at),
            at=at,
        )
        mark(work.connection, "codex", model, at)

    writer.execute(append)


def local(writer: WriteExecutor, minute: int, amount: int, index: int = 1) -> None:
    at = balance(0, minute).fetched_at

    def append(work: UnitOfWork) -> None:
        repo = WalletRepository(work.connection)
        entry = WalletEntry(
            f"consume-{index}",
            "vendor",
            "consumption",
            amount,
            at,
            repo.next_seq(),
            usage_ref=UsageRef(Harness.CODEX, f"{index:064x}"),
        )
        repo.append_with_offsets(work, entry, at=at)

    writer.execute(append)


def test_remote_five_minute_polls_do_not_reset_local_quiet_period(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers)
        local(writer, 2, 2 * USD)
        for minute in (5, 10):
            observe(store, 8 * USD, minute)
            store.flush_pending(balance(0, minute).fetched_at)
            assert summary(writer)["C"] == "0"
        store.flush_pending(balance(0, 12).fetched_at)
        assert summary(writer)["C"] == str(2 * USD)
        assert summary(writer)["B"] == str(8 * USD)
        # New remote data can settle immediately once the local monitor is quiet.
        observe(store, 7 * USD, 15)
        store.flush_pending(balance(0, 15).fetched_at)
        assert summary(writer)["C"] == str(3 * USD)
        for _ in range(3):
            store.flush_pending(balance(0, 20).fetched_at)
        assert readers.get().execute("SELECT COUNT(*) FROM account_interval").fetchone()[0] == 2


@pytest.mark.parametrize("reliable", [False, True])
def test_sub_cent_noise_stays_staged_and_accumulates_from_original_baseline(
    tmp_path: Path, reliable: bool
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, used=0 if reliable else None)
        observe(store, 10 * USD - CENT + 1, 5, CENT - 1 if reliable else None)
        store.flush_pending(balance(0, 10).fetched_at)
        assert summary(writer)["C"] == "0"
        assert readers.get().execute("SELECT COUNT(*) FROM account_posting").fetchone()[0] == 0
        assert readers.get().execute("SELECT COUNT(*) FROM account_interval").fetchone()[0] == 0
        observe(store, 10 * USD - CENT, 15, CENT if reliable else None)
        store.flush_pending(balance(0, 15).fetched_at)
        assert summary(writer)["C"] == str(CENT)


@pytest.mark.parametrize("inflow,expected", [(USD - 1, 0), (USD, USD), (USD + 123, USD + 123)])
def test_reliable_usage_also_requires_numeric_one_for_automatic_recharge(
    tmp_path: Path, inflow: int, expected: int
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, used=0)
        observe(store, 10 * USD + inflow, 5, 0)
        store.flush_pending(balance(0, 10).fetched_at)
        assert summary(writer)["R"] == str(expected)


def test_micro_local_remote_difference_does_not_make_adjustment_or_recharge(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, used=0)
        local(writer, 1, USD)
        observe(store, 9 * USD + CENT - 1, 5, USD)
        store.flush_pending(balance(0, 11).fetched_at)
        actual = summary(writer)
        assert (actual["R"], actual["C"], actual["A"]) == ("0", str(USD), "0")
        interval = readers.get().execute("SELECT * FROM account_interval").fetchone()
        assert interval["unresolved"] == str(CENT - 1)


def test_restart_seals_old_account_and_output_batches_independent_of_new_tokens(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = setup(writer, readers)
        local(writer, 1, 2 * USD)
        observe(store, 8 * USD, 5)
        speed(writer, 1, 600)
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = SqliteRemoteStore(writer, readers)
        store.begin_run(balance(0, 6).fetched_at)
        assert readers.get().execute("SELECT COUNT(*) FROM account_pending_batch").fetchone()[0] == 1
        local(writer, 7, USD, index=2)
        observe(store, 7 * USD, 8)
        speed(writer, 7, 1200)
        store.flush_pending(balance(0, 11).fetched_at)
        assert summary(writer)["C"] == str(2 * USD)
        reference = readers.get().execute("SELECT * FROM output_reference").fetchone()
        assert reference["samples"] == 1 and reference["output_tokens"] == "600"
        assert live(readers.get(), "codex", balance(0, 11).fetched_at)[0]["tokensPerSecond"] == "20.0"
        assert not live(readers.get(), "codex", balance(0, 11).fetched_at)[0]["frozen"]
        store.flush_pending(balance(0, 17).fetched_at)
        assert summary(writer)["C"] == str(3 * USD)
        assert readers.get().execute("SELECT samples FROM output_reference").fetchone()[0] == 2
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = SqliteRemoteStore(writer, readers)
        store.begin_run(balance(0, 30).fetched_at)
        store.flush_pending(balance(0, 30).fetched_at)
        assert summary(writer)["C"] == str(3 * USD)
        assert readers.get().execute("SELECT samples FROM output_reference").fetchone()[0] == 2


def test_failed_deferred_commit_keeps_durable_batch_for_one_retry(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers)
        observe(store, 8 * USD, 5)
        store.begin_run(balance(0, 6).fetched_at)
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail_interval BEFORE INSERT ON account_interval "
                "BEGIN SELECT RAISE(ABORT,'test failure'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="test failure"):
            store.flush_pending(balance(0, 10).fetched_at)
        assert summary(writer)["C"] == "0"
        assert readers.get().execute("SELECT COUNT(*) FROM account_pending_batch").fetchone()[0] == 1
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail_interval"))
        store.flush_pending(balance(0, 10).fetched_at)
        store.flush_pending(balance(0, 10).fetched_at)
        assert summary(writer)["C"] == str(2 * USD)


def test_detaching_remote_preserves_its_pending_batch(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers)
        observe(store, 8 * USD, 5)
        store.execute(lambda work: store.delete_account(work, ACCOUNT.id))
        assert readers.get().execute("SELECT COUNT(*) FROM account_pending_batch").fetchone()[0] == 1
        store.flush_pending(balance(0, 10).fetched_at)
        assert summary(writer)["C"] == str(2 * USD)
        assert not summary(writer)["remote"]
        assert readers.get().execute("SELECT COUNT(*) FROM account_pending_batch").fetchone()[0] == 0


def test_positive_minutes_only_round_half_up_and_shared_activity_delays_freeze(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = SqliteRemoteStore(writer, readers)
        speed(writer, 0, 600)
        speed(writer, 1, 0)
        speed(writer, 1, 1200, "model-b")
        store.flush_pending(balance(0, 10).fetched_at)
        assert readers.get().execute("SELECT COUNT(*) FROM output_reference").fetchone()[0] == 0
        values = {row["modelKey"]: row for row in live(readers.get(), "codex", balance(0, 10).fetched_at)}
        assert values["model-a"]["tokensPerSecond"] == "10.0"
        assert values["model-b"]["tokensPerSecond"] == "20.0"
        store.flush_pending(balance(0, 11).fetched_at)
        assert all(row["frozen"] for row in live(readers.get(), "codex", balance(0, 11).fetched_at))
        assert rate(3, 1) == "0.1"
        assert rate(0, 1) is None


def test_live_window_keeps_only_sixty_minute_buckets(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        for minute in range(62):
            speed(writer, minute, 60)
        row = readers.get().execute("SELECT * FROM output_live").fetchone()
        assert len(json.loads(row["buckets_json"])) == row["active_minutes"] == 60
        assert row["output_tokens"] == "3600"


def test_billing_batches_model_window_once_and_replays_do_not_add_output(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        events = tuple(usage(i, at=balance(0, 1).fetched_at, tokens=Tokens(output=60)) for i in range(1, 51))
        collect(writer, events, at=balance(0, 1).fetched_at)
        statements: list[str] = []

        def bill(work: UnitOfWork) -> None:
            work.connection.set_trace_callback(statements.append)
            try:
                IngestRepository(work.connection).bill_batch(
                    work,
                    lambda event: BilledUsage(event, CostEstimate(0, True, True, "none")),
                    at=balance(0, 1).fetched_at,
                )
            finally:
                work.connection.set_trace_callback(None)

        writer.execute(bill)
        assert len([sql for sql in statements if sql.startswith("INSERT INTO output_live")]) == 1
        assert live(readers.get(), "codex", balance(0, 1).fetched_at)[0]["outputTokens"] == "3000"
        collect(writer, events, at=balance(0, 2).fetched_at)
        writer.execute(bill)
        assert live(readers.get(), "codex", balance(0, 2).fetched_at)[0]["outputTokens"] == "3000"


def test_restarted_batch_waits_only_for_its_preexisting_inbox_members(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    old = usage(1, at=balance(0, 1).fetched_at)
    new = usage(2, at=balance(0, 2).fetched_at)
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = setup(writer, readers)
        writer.execute(create)
        writer.execute(
            lambda work: WalletRepository(work.connection).link_group(
                work, "g", "vendor", at=balance(0).fetched_at
            )
        )
        writer.execute(
            lambda work: work.connection.execute(
                "INSERT INTO harness_selection VALUES('codex','g',?,999)",
                (format_instant(balance(0).fetched_at),),
            )
        )
        collect(writer, (old,), at=balance(0, 1).fetched_at)
        observe(store, 11 * USD, 5)
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = SqliteRemoteStore(writer, readers)
        store.begin_run(balance(0, 6).fetched_at)
        collect(writer, (new,), at=balance(0, 7).fetched_at)
        store.flush_pending(balance(0, 11).fetched_at)
        assert summary(writer)["R"] == "0"  # Old member must finish first.

        def quote(event: UsageEvent) -> BilledUsage:
            return BilledUsage(event, CostEstimate(2 * USD, False, False, "base"), group_id="g")

        def bill(work: UnitOfWork) -> None:
            def append(current: WorkPort, billed: BilledUsage) -> None:
                WalletRepository(work.connection).append_consumption(
                    current, billed, at=balance(0, 11).fetched_at
                )

            IngestRepository(work.connection).bill_batch(
                work,
                quote,
                at=balance(0, 11).fetched_at,
                limit=1,
                on_billed=append,
            )

        writer.execute(bill)
        assert readers.get().execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
        store.flush_pending(balance(0, 11).fetched_at)
        assert summary(writer)["R"] == str(3 * USD)
        assert summary(writer)["C"] == str(2 * USD)
        assert readers.get().execute("SELECT COUNT(*) FROM account_pending_batch").fetchone()[0] == 0


def make_previous_database(path: Path, migration_dir: Path) -> None:
    migration_dir.mkdir(parents=True)
    for source in MIGRATIONS.glob("*.sql"):
        if source.name < "0034":
            shutil.copyfile(source, migration_dir / source.name)
    migrate(path, migrations=migration_dir)
    with closing(open_connection(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        work = UnitOfWork(db)
        vendor(work)
        RemoteRepository(db).save(work, replace(ACCOUNT, enabled=False))
        start = format_instant(balance(0).fetched_at)
        end = format_instant(balance(0, 5).fetched_at)
        db.execute(
            "INSERT INTO account_segment(id,vendor_id,kind,identity,started_at,anchor_r,anchor_c,anchor_a,"
            "initial_balance,c,methods) VALUES('old-segment','vendor','remote','old-identity',"
            "?,'0','0','0',?,?,'[\"remote_usage\"]')",
            (start, str(10 * USD), str(USD)),
        )
        unit = json.dumps(["USD", None, None, "USD", "native-USD-v1"])
        db.executemany(
            "INSERT INTO account_endpoint VALUES(?,?,?,?,?,?,?)",
            [
                ("old-start", "old-segment", start, str(10 * USD), "0", unit, "synthetic"),
                ("old-end", "old-segment", end, str(9 * USD), str(USD), unit, "synthetic"),
            ],
        )
        db.execute(
            "INSERT INTO account_interval(id,segment_id,start_endpoint,end_endpoint,e,e_watermark,r,c,a,"
            "raw_inflow,unresolved,method) VALUES('old-interval','old-segment','old-start','old-end',"
            "?,0,'0',?,'0','0','0','remote_usage')",
            (str(USD), str(USD)),
        )
        db.execute("COMMIT")


def test_upgrade_keeps_settled_evidence_and_starts_staging_at_latest_endpoint(tmp_path: Path) -> None:
    path = tmp_path / "htd.sqlite3"
    make_previous_database(path, tmp_path / "old-migrations")
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = SqliteRemoteStore(writer, readers)
        store.begin_run(balance(0, 30).fetched_at)
        assert summary(writer)["C"] == str(USD)
        assert readers.get().execute("SELECT COUNT(*) FROM account_interval").fetchone()[0] == 1
        pending = readers.get().execute("SELECT * FROM account_pending").fetchone()
        assert pending["start_endpoint"] == pending["end_endpoint"] == "old-end"
        assert readers.get().execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert readers.get().execute("PRAGMA foreign_key_check").fetchall() == []
        assert list((tmp_path / "backups").glob("htd-*.sqlite3"))
