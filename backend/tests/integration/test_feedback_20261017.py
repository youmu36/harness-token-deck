"""Settled billing periods and exact, rebuildable hourly history projections."""

from pathlib import Path

import pytest
from htd.adapters.sqlite import usage_archive
from htd.adapters.sqlite.account_book import AccountBook
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ledger_read import LedgerReader
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.rollup_repo import RollupRepository
from htd.adapters.sqlite.usage_hours import refresh_hours
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.bootstrap import Application
from htd.domain.common.time import format_instant, parse_instant
from htd.domain.statistics import Aggregator, Window

from backend.tests.contracts.command_helpers import seed_rows
from backend.tests.unit.statistics.helpers import row

from .test_remote_wallet import ACCOUNT, balance
from .test_wallet_storage import USD, consumption, vendor


def activity(writer: WriteExecutor, minute: int) -> None:
    writer.execute(
        lambda w: w.connection.execute(
            "UPDATE collection_activity SET generation=generation+1,last_at=? WHERE id=1",
            (format_instant(balance(0, minute).fetched_at),),
        )
    )


def test_remote_baseline_and_later_local_estimate_remain_separate(tmp_path: Path):
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(lambda w: vendor(w, initial="10"))
        store = SqliteRemoteStore(writer, readers)
        store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
        store.execute(lambda w: store.observe(w, balance(10 * USD)))
        writer.execute(lambda w: consumption(w, amount=3 * USD, at=balance(0, 2).fetched_at))
        assert AccountBook(readers.get()).summary("vendor")["B"] == str(10 * USD)
        assert WalletRepository(readers.get()).totals("vendor").b == 7 * USD
        assert AccountBook(readers.get()).summary("vendor")["R"] == "0"


@pytest.mark.parametrize("decrease", [USD // 10 - 1, USD // 10, USD // 10 + 1])
def test_account_consumption_advances_independently_of_collection_and_restart(tmp_path: Path, decrease: int):
    path = tmp_path / "ledger.sqlite3"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        writer.execute(lambda w: vendor(w, initial="10"))
        store = SqliteRemoteStore(writer, readers)
        store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
        store.execute(lambda w: store.observe(w, balance(10 * USD)))
        activity(writer, 1)
        store.execute(lambda w: store.observe(w, balance(10 * USD - decrease, 1)))
        assert AccountBook(readers.get()).summary("vendor")["C"] == "0"
        activity(writer, 2)
        for minute in (3, 7, 8):
            store.execute(lambda w, minute=minute: store.observe(w, balance(10 * USD - decrease, minute)))
        assert AccountBook(readers.get()).summary("vendor")["C"] == "0"
        store.flush_pending(balance(0, 10).fetched_at)
        assert AccountBook(readers.get()).summary("vendor")["C"] == str(decrease)
        assert len(WalletRepository(readers.get()).calibrations("vendor")) == 1
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = SqliteRemoteStore(writer, readers)
        store.execute(lambda w: store.observe(w, balance(9 * USD, 10)))
        assert AccountBook(readers.get()).summary("vendor")["C"] == str(USD)
        assert AccountBook(readers.get()).summary("vendor")["B"] == str(9 * USD)


def test_account_settlement_failure_rolls_back_endpoint_and_retries_once(tmp_path: Path):
    import sqlite3
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(lambda w: vendor(w, initial="10"))
        store = SqliteRemoteStore(writer, readers)
        store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
        store.execute(lambda w: store.observe(w, balance(10 * USD)))
        writer.execute(lambda w: w.connection.execute(
            "CREATE TRIGGER reject_settlement BEFORE INSERT ON account_interval "
            "BEGIN SELECT RAISE(ABORT,'settlement failed'); END"))
        with pytest.raises(sqlite3.IntegrityError, match="settlement failed"):
            store.execute(lambda w: store.observe(w, balance(8 * USD, 10)))
        assert AccountBook(readers.get()).summary("vendor")["B"] == str(10 * USD)
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER reject_settlement"))
        for _ in range(2):
            store.execute(lambda w: store.observe(w, balance(8 * USD, 10)))
        assert AccountBook(readers.get()).summary("vendor")["C"] == str(2 * USD)
        assert readers.get().execute("SELECT COUNT(*) FROM account_interval").fetchone()[0] == 1


@pytest.mark.parametrize("archive", [False, True])
def test_coarse_queries_preserve_boundaries_quality_and_dirty_hours(tmp_path: Path, archive: bool):
    start = parse_instant("2026-09-13T00:00:00Z")
    end = parse_instant("2026-09-14T00:00:00Z")
    at = parse_instant("2026-11-01T00:00:00Z" if archive else "2026-09-20T00:00:00Z")
    with Application(tmp_path / "data", start_scheduler=False) as app:
        seed_rows(
            app,
            [
                row(
                    i + 1,
                    i % 60,
                    hour=i // 60,
                    day=13,
                    model=f"model-{i % 2}",
                    input_count=70_000 + i,
                    cache=0 if i % 3 else 50_000,
                    pico=9_007_199_254_740_993 + i,
                    unpriced=i % 7 == 0,
                )
                for i in range(180)
            ],
        )
        while app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=at)) == 500:
            pass

        def compare(begin=start, finish=end, offset=0):
            with app.readers.connection() as db:
                aggregate = Aggregator(offset, "hour", begin, finish)
                for item in LedgerReader(db).iter_rows(begin, finish):
                    aggregate.push(item)
                expected = aggregate.finish()
            actual = app.statistics.summary("all", Window(begin, finish, offset, "hour"))[0]
            assert actual == expected

        compare()
        compare(parse_instant("2026-09-13T00:17:00Z"), parse_instant("2026-09-13T02:43:00Z"))
        compare(offset=480)
        compare(offset=345)  # A fractional hour offset keeps authoritative minutes.
        with app.readers.connection() as db:
            raw = list(LedgerReader(db).iter_rows(start, end))
            coarse = list(LedgerReader(db).iter_rows(start, end, coarse=True))
            assert len(coarse) < len(raw) / 4  # Unpriced requests retain their 30-day detail window.
        # A late row, repricing, or retention changes authoritative data and
        # invalidates the projection in the very same transaction.
        if not archive:
            app.writer.execute(
                lambda w: w.connection.execute(
                    "UPDATE usage_rollup SET pico='123' WHERE bucket=?", ("2026-09-13T00:17:00.000Z",)
                )
            )
            compare()
            app.writer.execute(lambda w: refresh_hours(w.connection))
            compare()
            app.writer.execute(
                lambda w: w.connection.execute(
                    "DELETE FROM usage_rollup WHERE bucket<?", ("2026-09-13T01:15:00.000Z",)
                )
            )
            compare()


def test_model_metadata_skips_unrelated_payloads_and_hour_queries_do_not_decompress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    start = parse_instant("2026-09-13T00:00:00Z")
    end = parse_instant("2026-09-14T00:00:00Z")
    with Application(tmp_path / "data", start_scheduler=False) as app:
        seed_rows(app, [row(i + 1, i % 60, day=13, model=f"model-{i % 4}") for i in range(240)])
        while (
            app.writer.execute(
                lambda w: RollupRepository(w.connection).compact(w, at=parse_instant("2026-11-01T00:00:00Z"))
            )
            == 500
        ):
            pass
        unpack = usage_archive.unpack
        decoded = []

        def counted(payload):
            decoded.append(len(payload))
            return unpack(payload)

        monkeypatch.setattr(usage_archive, "unpack", counted)
        with app.readers.connection() as db:
            assert list(LedgerReader(db).iter_rows(start, end, model_key="model-1", coarse=True))
            assert decoded == []
            assert list(LedgerReader(db).iter_rows(start, end, model_key="model-1"))
            assert len(decoded) == 1
            decoded.clear()
            assert list(LedgerReader(db).iter_rows(start, end, model_key="missing")) == []
            assert decoded == []


def test_hour_totals_keep_arbitrary_precision_beyond_individual_request_limit(tmp_path: Path):
    start, end = parse_instant("2026-09-13T00:00:00Z"), parse_instant("2026-09-14T00:00:00Z")
    with Application(tmp_path / "data", start_scheduler=False) as app:
        seed_rows(app, [row(i + 1, i, day=13, pico=2**126) for i in range(5)])
        while (
            app.writer.execute(
                lambda w: RollupRepository(w.connection).compact(w, at=parse_instant("2026-11-01T00:00:00Z"))
            )
            == 500
        ):
            pass
        summary = app.statistics.summary("all", Window(start, end, 0, "hour"))[0]
        assert summary.metrics.pico_usd == str(5 * 2**126)
