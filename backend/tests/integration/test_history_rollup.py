"""The detail boundary must preserve statistics, money, deduplication and recovery."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from htu.adapters.sqlite.codex_audit import read_audit_ledger
from htu.adapters.sqlite.ingest_repo import IngestRepository
from htu.adapters.sqlite.ledger_read import LedgerReader
from htu.adapters.sqlite.rollup_repo import RollupRepository
from htu.adapters.sqlite.wallet_entries import wallet_entries
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.application.ports import BilledUsage
from htu.application.ports import UnitOfWork as WorkPort
from htu.domain.common.time import Instant
from htu.domain.statistics.aggregator import Aggregator
from htu.domain.usage.model import Tokens
from htu.tools.codex_audit import AuditReport, markdown

from .collection_helpers import AT, collect, quote, usage
from .test_pricing_storage import create
from .test_wallet_storage import vendor

LATER = Instant(AT.value + timedelta(hours=73))
END = Instant(AT.value + timedelta(days=1))


def summary(work: UnitOfWork):
    aggregate = Aggregator(480, "hour", AT, END)
    for row in LedgerReader(work.connection).iter_rows(AT, END):
        aggregate.push(row)
    return aggregate.finish()


def seed(writer: WriteExecutor) -> None:
    writer.execute(create)
    writer.execute(lambda w: vendor(w, initial="100"))
    writer.execute(lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=AT))
    events = tuple(usage(i, tokens=Tokens(100_000, 50_000 if i % 2 else 0, 0, 10, 0)) for i in range(1, 101))
    collect(writer, events)

    def bill(work: UnitOfWork):
        def append(w: WorkPort, b: BilledUsage) -> None:
            WalletRepository(work.connection).append_consumption(w, b, at=AT)

        return IngestRepository(work.connection).bill_batch(
            work,
            lambda e: replace(quote(e), group_id="g", version_id="v", revision_id="r"),
            at=AT,
            on_billed=append,
        )

    writer.execute(bill)


def test_compaction_preserves_all_breakdowns_money_and_daily_filter_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    with WriteExecutor(path) as writer:
        seed(writer)
        before = writer.execute(summary)
        total = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        from htu.adapters.sqlite.wallet_closing import close_days
        writer.execute(lambda w: close_days(w,w.connection,at=END))
        total = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        entries = writer.execute(
            lambda w: wallet_entries(w.connection, "vendor", None, 50, "consumption_daily", 480)
        )
        assert (
            cast(list[dict[str, object]], entries["entries"])[0]["occurredAt"] == "2026-09-30T15:59:00.000Z"
        )
        assert writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=LATER)) == 100
        assert writer.execute(summary) == before
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")) == total
        assert (
            writer.execute(
                lambda w: wallet_entries(w.connection, "vendor", None, 50, "consumption_daily", 480)
            )
            == entries
        )
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]) == 0
        )
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM usage_rollup").fetchone()[0])
            == 1
        )
        assert (
            writer.execute(
                lambda w: w.connection.execute(
                    "SELECT COUNT(*) FROM wallet_entry WHERE kind='consumption'"
                ).fetchone()[0]
            )
            == 0
        )
        assert collect(writer, (usage(1),)).duplicates == 1
        writer.execute(
            lambda w: RollupRepository(w.connection).compact(w, at=Instant(AT.value + timedelta(days=31)))
        )
        assert writer.execute(summary) == before
        assert writer.execute(
            lambda w: wallet_entries(w.connection, "vendor", None, 50, "consumption_daily", 480)
        ) == entries
        assert writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=LATER)) == 0
    with WriteExecutor(path) as writer:
        assert writer.execute(summary) == before
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")).b == total.b
        assert writer.execute(lambda w: WalletRepository(w.connection).validate_rebuild(w, at=LATER)) == ()
        assert writer.execute(lambda w: w.connection.execute("PRAGMA foreign_key_check").fetchall()) == []
    audit = read_audit_ledger(path, AT, END)
    assert audit.compacted_records == 100
    assert audit.compacted_pico == 100 * 84_000_000
    report = markdown(AuditReport(AT, END, LATER, ledger=audit))
    assert "100 条已汇总请求" in report and "来源未入账" not in report
    assert "逐请求字段一致性仅适用于保留的明细" in report


def test_boundary_and_failure_are_atomic(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer:
        seed(writer)
        boundary = Instant(AT.value + timedelta(hours=72))
        assert writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=boundary)) == 0
        before = writer.execute(summary)
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=LATER))
        assert writer.execute(summary) == before
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM usage_rollup").fetchone()[0])
            == 0
        )
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_rollup").fetchone()[0])
            == 0
        )
