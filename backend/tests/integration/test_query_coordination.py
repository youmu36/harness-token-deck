"""Concurrent snapshots, selective reads and reusable wallet days on isolated databases."""

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any, Literal, cast

import pytest
from htu.adapters.sqlite.ledger_read import LedgerReader
from htu.adapters.sqlite.statistics_store import SqliteStatisticsSnapshot
from htu.adapters.sqlite.wallet_command_store import SqliteWalletCommandStore
from htu.adapters.sqlite.wallet_entries import wallet_entries
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, parse_instant
from htu.domain.statistics import LedgerRow, Window
from htu.domain.usage.model import Harness

from backend.tests.contracts.command_helpers import seed_rows
from backend.tests.unit.statistics.helpers import row

from .test_wallet_storage import consumption, vendor

START = parse_instant("2026-09-01T00:00:00Z")
END = parse_instant("2026-10-01T00:00:00Z")


def test_slow_history_does_not_lock_monitor_and_identical_snapshot_reads_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, follower = Event(), Event(), Event()
    original = LedgerReader.iter_rows
    signature = SqliteStatisticsSnapshot.signature
    calls: list[tuple[Instant, Instant]] = []

    def read(
        self: LedgerReader, start: Instant, end: Instant, **kwargs: Any
    ) -> Generator[LedgerRow, None, None]:
        calls.append((start, end))
        if start == START:
            entered.set()
            assert release.wait(2)
        yield from original(self, start, end, **kwargs)

    def signed(
        self: SqliteStatisticsSnapshot, start: Instant, end: Instant, harness: Harness | Literal["all"]
    ) -> tuple[tuple[str, str, int], ...]:
        result = signature(self, start, end, harness)
        if entered.is_set() and start == START:
            follower.set()
        return result

    monkeypatch.setattr(LedgerReader, "iter_rows", read)
    monkeypatch.setattr(SqliteStatisticsSnapshot, "signature", signed)
    with Application(tmp_path / "data", clock=FakeClock(END), start_scheduler=False) as app:
        seed_rows(app, [row(1)])
        window = Window(START, END, 345, "day")
        with ThreadPoolExecutor(3) as executor:
            first = executor.submit(app.statistics.summary, Harness.CODEX, window)
            try:
                assert entered.wait(2)
                second = executor.submit(app.statistics.summary, Harness.CODEX, window)
                assert follower.wait(2)
                # No deadline sleeps: a monitor must finish while history remains blocked.
                monitor = executor.submit(app.statistics.cache_monitor, Harness.CODEX, "hour", 345)
                assert monitor.result(2)["trend"] == []
                assert not first.done()
            finally:
                release.set()
            assert first.result(2) == second.result(2)
        assert len([call for call in calls if call[0] == START]) == 1
        assert app.readers.diagnostics()["connections"] <= 4
        assert app.statistics.diagnostics()["estimatedBytes"] <= 4 * 1024**2


def test_release_during_query_prevents_cache_repopulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = LedgerReader.iter_rows

    def read(self: LedgerReader, *args: Any, **kwargs: Any) -> Generator[LedgerRow, None, None]:
        entered.set()
        assert release.wait(2)
        yield from original(self, *args, **kwargs)

    monkeypatch.setattr(LedgerReader, "iter_rows", read)
    with Application(tmp_path / "data", start_scheduler=False) as app, ThreadPoolExecutor(1) as executor:
        seed_rows(app, [row(1)])
        request = executor.submit(app.statistics.summary, Harness.CODEX, Window(START, END, 0, "day"))
        try:
            assert entered.wait(2)
            app.statistics.invalidate()
        finally:
            release.set()
        assert request.result(2)[0].metrics.record_count == 1
        assert app.statistics.diagnostics()["entries"] == 0


def test_model_filter_reduces_rows_before_aggregation_and_uses_index(tmp_path: Path) -> None:
    with Application(tmp_path / "data", start_scheduler=False) as app:
        seed_rows(app, [row(1, model="a"), row(2, model=None), row(3, model="b")])
        with app.readers.connection() as db:
            assert [r.model_key for r in LedgerReader(db).iter_rows(START, END, model_key="")] == [""]
            assert [r.model_key for r in LedgerReader(db).iter_rows(START, END, model_key="b")] == ["b"]
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM ledger WHERE model_key=? "
                "AND occurred_at>=? AND occurred_at<?",
                ("b", "2026-09-01", "2026-10-01"),
            ).fetchall()
            assert any("ledger_model_time" in r[3] for r in plan)


def test_advancing_end_reads_only_extension_and_still_invalidates_late_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = LedgerReader.iter_rows
    calls: list[tuple[Instant, Instant]] = []

    def read(self: LedgerReader, start: Instant, end: Instant, **kwargs: Any):
        calls.append((start, end))
        yield from original(self, start, end, **kwargs)

    monkeypatch.setattr(LedgerReader, "iter_rows", read)
    first_end, second_end = parse_instant("2026-09-01T00:02:00Z"), parse_instant("2026-09-01T00:03:00Z")
    with Application(tmp_path / "data", clock=FakeClock(second_end), start_scheduler=False) as app:
        seed_rows(app, [row(1, 1, day=1), row(2, 2, day=1)])
        first = app.statistics.summary("all", Window(START, first_end, 345, "hour"))[0]
        second = app.statistics.summary("all", Window(START, second_end, 345, "hour"))[0]
        assert first.metrics.record_count == 1 and second.metrics.record_count == 2
        assert calls[-1] == (first_end, second_end)
        app.statistics.invalidate()
        assert app.statistics.summary("all", Window(START, second_end, 345, "hour"))[0] == second
        seed_rows(app, [row(3, 1, day=1)])
        assert (
            app.statistics.summary("all", Window(START, second_end, 345, "hour"))[0].metrics.record_count == 3
        )
        assert calls[-1][0] == START


def test_wallet_pages_reuse_days_then_rebuild_on_same_minute_watermark_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from htu.adapters.sqlite import wallet_command_store

    calls: list[int] = []
    rebuild = wallet_command_store.daily_spending

    def counted(*args: Any, **kwargs: Any):
        calls.append(1)
        return rebuild(*args, **kwargs)

    monkeypatch.setattr(wallet_command_store, "daily_spending", counted)
    with Application(tmp_path / "data", start_scheduler=False) as app:
        writer = app.writer
        writer.execute(vendor)
        writer.execute(lambda w: consumption(w, amount=10**25 + 1))
        writer.execute(
            lambda w: consumption(w, 2, amount=10**25 + 2, at=parse_instant("2026-10-01T00:00:00Z"))
        )
        store = SqliteWalletCommandStore(writer, app.readers)
        first = store.wallet_entries("vendor", None, 1, offset_minutes=345)
        next_page = store.wallet_entries(
            "vendor", cast(int | None, first["nextCursor"]), 1, offset_minutes=345
        )
        assert len(calls) == 1
        assert first["entries"] and next_page["entries"]
        writer.execute(lambda w: consumption(w, 3, amount=7))
        store.wallet_entries("vendor", None, 1, offset_minutes=345)
        assert len(calls) == 2
        for offset in (345, -210):
            expected = writer.execute(
                lambda w, offset=offset: wallet_entries(w.connection, "vendor", None, 50, "all", offset)
            )
            assert store.wallet_entries("vendor", None, 50, offset_minutes=offset) == expected
        writer.execute(
            lambda w: WalletRepository(w.connection).calibrate(
                w, "vendor", "0", expected_revision=w.revision("wallet"), at=END, operation_id="calibrate"
            )
        )
        expected = writer.execute(lambda w: wallet_entries(w.connection, "vendor", None, 50, "all", 345))
        assert store.wallet_entries("vendor", None, 50, offset_minutes=345) == expected
