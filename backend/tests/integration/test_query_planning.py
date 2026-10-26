"""Bounded query slots, precise cache keys, diagnostic chains and explicit historical scope."""

import sqlite3
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from htd.adapters.sqlite import explorer_read
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ledger_read import LedgerReader
from htd.adapters.sqlite.statistics_store import SqliteStatisticsSnapshot, SqliteStatisticsStore
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports.query_control import QuerySlots, analysis_read, cancel_analysis
from htd.bootstrap import Application
from htd.domain.common.errors import DomainError
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant, format_instant
from htd.domain.statistics import Window
from htd.domain.statistics.query import QueryPlan
from htd.ipc.dto.evidence import EvidenceResponse
from htd.ipc.dto.explorer import OutputReferencesResponse

from backend.tests.contracts.command_helpers import invoke

from .collection_helpers import usage
from .test_calibration_targets import apply, proposal
from .test_explorer_043 import AT, bill, request, setup
from .test_reconciliation_batches import configured, receive


def test_custom_range_and_session_capabilities_are_canonical_and_bounded() -> None:
    query = dict(
        request(),
        start=format_instant(Instant(AT.value - timedelta(days=30))),
        end=format_instant(AT),
        rangeKind="custom",
        modelKeys=["b", "a", "b"],
    )
    assert QueryPlan.build(query, AT).parameters["modelKeys"] == ["a", "b"]
    with pytest.raises(DomainError, match="30"):
        QueryPlan.build(dict(query, start=format_instant(Instant(AT.value - timedelta(days=31)))), AT)
    with pytest.raises(DomainError, match="62"):
        QueryPlan.build(
            dict(
                query,
                start=format_instant(Instant(AT.value - timedelta(days=70))),
                end=format_instant(Instant(AT.value - timedelta(days=69))),
            ),
            AT,
        )
    with pytest.raises(DomainError, match="62"):
        QueryPlan.build(
            dict(
                query,
                rangeKind="year",
                sessionRefs=[""],
                start=format_instant(Instant(AT.value - timedelta(days=70))),
            ),
            AT,
        )
    assert (
        QueryPlan.build(dict(query, rangeKind="year", start="2025-01-01T00:00:00.000Z"), AT).layer == "mixed"
    )


def test_cache_survives_unrelated_source_and_balance_changes_but_tracks_selected_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        setup(writer)
        bill(writer, (usage(1, at=AT, model="gpt-6-astra"),))
        original = explorer_read.explore
        calls: list[int] = []

        def counted(db: sqlite3.Connection, query: dict[str, Any], now: Instant) -> dict[str, object]:
            calls.append(1)
            return original(db, query, now)

        monkeypatch.setattr(explorer_read, "explore", counted)
        store = SqliteStatisticsStore(readers)
        query = request(harnesses=["codex"], sections=["totals"])
        store.explore(query, AT)
        writer.execute(lambda w: w.touch("wallet"))
        writer.execute(
            lambda w: w.connection.execute("INSERT INTO usage_day_revision VALUES('claude','2026-10-21',1)")
        )
        store.explore(query, AT)
        assert len(calls) == 1
        writer.execute(
            lambda w: w.connection.execute(
                "UPDATE usage_day_revision SET value=value+1 WHERE harness='codex' AND day='2026-10-21'"
            )
        )
        store.explore(query, AT)
        assert len(calls) == 2


def test_evidence_chain_is_read_only_and_connects_original_price_voucher_and_target(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        receive(app, usage(1, at=AT))
        clock.advance(11 * 60000)
        parameters = dict(
            proposal(app, supplier, 100), start=format_instant(AT), end=format_instant(clock.now_utc())
        )
        target = apply(app, parameters)
        revisions = app.revisions()
        data = invoke(
            app,
            "stats.evidence",
            {"vendorId": supplier, "start": parameters["start"], "end": parameters["end"]},
            EvidenceResponse,
        )
        assert app.revisions() == revisions
        item = data.items[0]
        assert item.requestKey == usage(1).dedupe_key
        assert item.priceRevisionId and item.voucherSequence and item.localBatchId
        assert item.calibrationTargetId == target.targetId and item.canRecalculate
        assert item.acceptedRevision == item.valuedRevision == item.projectedRevision


def test_scoped_tps_rejects_older_history_and_explicit_unscoped_range_is_not_thirty_days(
    tmp_path: Path,
) -> None:
    with Application(tmp_path, clock=FakeClock(AT), start_scheduler=False) as app:

        def seed(work: Any) -> None:
            work.connection.execute(
                "INSERT INTO output_daily_speed "
                "VALUES('2026-09-01','Asia/Shanghai','codex','model','Model','12.3',2),('2026-10-21','Asia/Shanghai','codex','model','Model','99.0',1)"
            )

        app.writer.execute(seed)
        query = {
            "start": "2026-08-01T00:00:00.000Z",
            "end": "2026-09-02T00:00:00.000Z",
            "modelKeys": ["model"],
        }
        ordinary = invoke(app, "stats.outputReferences", query, OutputReferencesResponse)
        assert ordinary.references[0].day == "2026-09-01"
        assert ordinary.todayReferences and ordinary.todayReferences[0].tokensPerSecond == "99.0"
        unsupported = invoke(
            app,
            "stats.outputReferences",
            dict(query, start="2026-01-01T00:00:00.000Z", vendorIds=["vendor"]),
            OutputReferencesResponse,
        )
        assert not unsupported.references and unsupported.coverage[0].code == "tps_scope_history"


def test_recent_session_and_model_reads_use_bounded_range_indexes(tmp_path: Path) -> None:
    with (
        WriteExecutor(tmp_path / "a.db") as writer,
        ReadPool(writer.path) as readers,
        readers.connection() as db,
    ):
        for column, index in (
            ("session_ref", "usage_recent_session"),
            ("model_key", "usage_recent_model"),
        ):
            plan = db.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM usage_recent WHERE "
                + column
                + "=? AND bucket>=? AND bucket<?",
                ("selected", "2026-09-01", "2026-09-02"),
            ).fetchall()
            assert any(index in row[3] and "bucket>? AND bucket<?" in row[3] for row in plan)


def test_summary_owner_can_leave_without_repeating_the_shared_aggregation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, joined, release = Event(), Event(), Event()
    original, sign = LedgerReader.iter_rows, SqliteStatisticsSnapshot.signature
    calls: list[int] = []

    def slow(self: LedgerReader, *args: Any, **kwargs: Any) -> Generator[Any, None, None]:
        calls.append(1)
        entered.set()
        assert release.wait(3)
        yield from original(self, *args, **kwargs)

    def signed(self: SqliteStatisticsSnapshot, *args: Any, **kwargs: Any) -> Any:
        if entered.is_set():
            joined.set()
        return sign(self, *args, **kwargs)

    monkeypatch.setattr(LedgerReader, "iter_rows", slow)
    monkeypatch.setattr(SqliteStatisticsSnapshot, "signature", signed)
    with (
        Application(tmp_path, clock=FakeClock(AT), start_scheduler=False) as app,
        ThreadPoolExecutor(2) as workers,
    ):
        window = Window(AT, Instant(AT.value + timedelta(minutes=1)), 0, "hour")

        def read(identity: str) -> Any:
            with analysis_read(identity):
                return app.statistics.summary("all", window)

        owner = workers.submit(read, "summary-owner")
        assert entered.wait(3)
        follower = workers.submit(read, "summary-follower")
        assert joined.wait(3)
        cancel_analysis("summary-owner")
        release.set()
        with pytest.raises(DomainError, match="取消"):
            owner.result(3)
        assert follower.result(3)[0].metrics.record_count == 0
        assert len(calls) == 1 and app.readers.diagnostics()["active"] == 0


def test_waiting_summary_and_explorer_hold_no_read_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting, seen = Event(), set[int]()
    original = QuerySlots.acquire

    @contextmanager
    def observed(self: QuerySlots) -> Generator[None, None, None]:
        from threading import get_ident

        seen.add(get_ident())
        if len(seen) >= 2:
            waiting.set()
        with original(self):
            yield

    with (
        Application(tmp_path, clock=FakeClock(AT), start_scheduler=False) as app,
        ThreadPoolExecutor(2) as workers,
    ):
        store = app.statistics.store
        assert isinstance(store, SqliteStatisticsStore)
        with original(store.query_slots), original(store.query_slots):
            monkeypatch.setattr(QuerySlots, "acquire", observed)
            summary = workers.submit(
                app.statistics.summary, "all", Window(AT, Instant(AT.value + timedelta(minutes=1)), 0, "hour")
            )
            explore = workers.submit(app.statistics.explore, request(sections=["totals"]))
            assert waiting.wait(3)
            assert app.readers.diagnostics()["active"] == 0
        assert summary.result(3)[0].metrics.record_count == 0
        assert explore.result(3)["totalModels"] == 0
