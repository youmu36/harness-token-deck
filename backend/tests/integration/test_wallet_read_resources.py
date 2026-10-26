"""One local waterline per wallet snapshot and exact streamed historical totals."""

from datetime import timedelta
from pathlib import Path

from htd.adapters.sqlite.account_book import AccountBook
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.local_wallet import remote_summary
from htd.adapters.sqlite.wallet_command_store import SqliteWalletCommandStore
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.domain.common.time import Instant, format_instant

from .collection_helpers import AT
from .test_wallet_storage import consumption, recharge, vendor


def test_wallet_snapshot_reads_local_waterline_once(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        writer.execute(vendor)
        writer.execute(recharge)
        writer.execute(consumption)
        statements: list[str] = []
        with readers.connection() as db:
            expected = AccountBook(db).summary("vendor")
            db.set_trace_callback(statements.append)
        snapshots, _ = SqliteWalletCommandStore(writer, readers).wallets(at=AT, include_archived=False)
        with readers.connection() as db:
            db.set_trace_callback(None)
        assert len(snapshots) == 1
        assert snapshots[0].account == expected
        reads = [sql for sql in statements if "FROM wallet_total WHERE vendor_id=" in sql]
        assert len(reads) == 1


def test_long_account_history_preserves_exact_totals_in_both_projections(tmp_path: Path) -> None:
    amount, count = 10**26 + 123, 2500

    def seed(work: UnitOfWork) -> None:
        vendor(work)
        work.connection.executemany(
            "INSERT INTO account_segment(id,vendor_id,kind,identity,started_at,ended_at,"
            "anchor_r,anchor_c,anchor_a,initial_balance,r,c,a,methods) "
            "VALUES(?,'vendor','remote','history',?,?,'0','0','0','7',?,?,?,'[\"remote_usage\"]')",
            (
                (
                    str(index),
                    format_instant(Instant(AT.value - timedelta(days=3) + timedelta(seconds=index))),
                    "2026-09-02T00:00:00.000Z",
                    str(amount + index),
                    str(amount),
                    str(-index),
                )
                for index in range(count)
            ),
        )

    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        writer.execute(seed)
        with readers.connection() as db:
            combined = AccountBook(db).summary("vendor")
            remote = remote_summary(db, "vendor")
        expected = {
            "R": str(count * amount + count * (count - 1) // 2),
            "C": str(count * amount),
            "A": str(-count * (count - 1) // 2),
        }
        assert {key: combined[key] for key in expected} == expected
        assert remote is not None and {key: remote[key] for key in expected} == expected
        assert remote["B"] == "7" and combined["methods"] == ["remote_usage"]
