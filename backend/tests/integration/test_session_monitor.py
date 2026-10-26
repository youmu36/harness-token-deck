"""Durable session selection, independent warnings and bounded sparse monitor reads."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.usage_evidence import mark_compacted
from htd.adapters.sqlite.writer import WriteExecutor
from htd.domain.common.time import Instant, parse_instant
from htd.domain.usage.model import Harness, Tokens
from htd.domain.usage.session import session_reference

from .collection_helpers import AT, collect, quote, usage


def test_monitor_freezes_at_ten_minutes_expires_at_twenty_and_resumes_with_totals(tmp_path: Path) -> None:
    ref = session_reference(Harness.CODEX, "session")
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, (usage(1, session="session"),), at=AT)
        store = SqliteStatisticsStore(readers)
        for seconds, present, frozen in (
            (599, True, False),
            (600, True, True),
            (1199, True, True),
            (1200, False, False),
        ):
            now = Instant(AT.value + timedelta(seconds=seconds))
            data = store.monitor_data("codex", now, ref, chart_session_ref=ref)
            assert bool(data["sessionHitRates"]) == present
            if present:
                assert data["sessionHitRates"][0]["frozen"] == frozen
            else:
                assert data["selectedSessionRef"] is data["chartSessionRef"] is None
                assert not data["outputSpeeds"]
                assert data["windowTotals"]["recordCount"] == "0"
        now = Instant(AT.value + timedelta(minutes=21))
        collect(writer, (usage(2, session="session", at=now),), at=now, offset=2)
        data = store.monitor_data("codex", now, ref, chart_session_ref=ref)
        assert data["selectedSessionRef"] == ref
        assert not data["sessionHitRates"][0]["frozen"]
        assert data["sessionTotals"]["recordCount"] == "2"


def test_selected_session_warnings_reception_late_compaction_and_resume(tmp_path: Path) -> None:
    a, b = (session_reference(Harness.CODEX, value) for value in ("a", "b"))
    first = usage(1, session="a", tokens=Tokens(100_000, 0, 0, 10))
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, (first, usage(2, session="b", tokens=Tokens(100_000, 20_000, 0, 20))), at=AT)
        store = SqliteStatisticsStore(readers)
        now = parse_instant("2026-09-30T00:01:00Z")
        before = store.monitor_data("codex", now, a)
        assert len(before["trend"]) == 60 and before["windowTotals"]["inputTokens"] == "100000"
        assert before["trend"][-1]["cacheBust"]
        assert not before["outputSpeeds"]  # Token is visible before valuation.
        writer.execute(lambda work: mark_compacted(work.connection, work, first.dedupe_key))
        changed = store.monitor_data("codex", now, a)
        other = store.monitor_data("codex", now, b)
        assert changed["trend"][-1]["contextCompacted"] and not changed["trend"][-1]["cacheBust"]
        assert not other["trend"][-1]["contextCompacted"] and other["trend"][-1]["cacheLoss"]
        writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=now))
        assert store.monitor_data("codex", now, a)["sessionTotals"]["recordCount"] == "1"
        later = parse_instant("2026-09-30T01:11:00Z")
        assert store.monitor_data("codex", later, a)["windowTotals"]["recordCount"] == "0"
        collect(writer, (usage(3, session="b", at=later),), at=later, offset=3)
        frozen = store.monitor_data("codex", later, a)
        assert frozen["selectedSessionRef"] == b and len(frozen["sessionHitRates"]) == 1
        assert all(row["sessionRef"] != a for row in frozen["sessionHitRates"])
        collect(writer, (usage(4, session="a", at=later),), at=later, offset=4)
        assert store.monitor_data("codex", later, a)["sessionTotals"]["recordCount"] == "2"
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        result = SqliteStatisticsStore(readers).monitor_data("codex", later, a)
        assert result["sessionTotals"]["inputTokens"] == "100100"


def test_directory_pages_restore_selected_and_read_only_its_curve(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, tuple(usage(index, session=f"session-{index}") for index in range(1, 76)), at=AT)
        store = SqliteStatisticsStore(readers)
        now = parse_instant("2026-09-30T00:01:00Z")
        page = store.monitor_data("codex", now)
        assert len(page["sessionHitRates"]) == 50 and page["nextCursor"]
        later = store.monitor_data("codex", now, cursor=page["nextCursor"])
        assert len(later["sessionHitRates"]) == 25 and later["nextCursor"] is None
        selected = later["sessionHitRates"][0]["sessionRef"]
        restored = store.monitor_data("codex", now, selected)
        assert len(restored["sessionHitRates"]) == 51 and restored["selectedSessionRef"] == selected
        assert restored["windowTotals"]["recordCount"] == "1"
        assert sum(point["recordCount"] for point in restored["trend"]) == 1
        statements: list[str] = []
        readers.get().set_trace_callback(statements.append)
        assert store.monitor_data("codex", now, selected) == restored
        readers.get().set_trace_callback(None)
        assert not any("FROM session_cache_minute" in sql for sql in statements)


def test_concurrent_monitor_misses_do_not_double_count_cache_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import htd.adapters.sqlite.session_monitor as module

    arrived, resume, joined = Event(), Event(), Event()
    reads: list[int] = []
    payload: dict[str, object] = {"sample": "x" * 1000}

    def read(*args: object) -> dict[str, object]:
        reads.append(1)
        arrived.set()
        assert resume.wait(timeout=5)
        return payload

    monkeypatch.setattr(module, "read", read)
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = SqliteStatisticsStore(readers)
        original = store.monitor_cache.claim

        def claim(*args: Any, **kwargs: Any) -> Any:
            future, owner = original(*args, **kwargs)
            if not owner:
                joined.set()
            return future, owner

        monkeypatch.setattr(store.monitor_cache, "claim", claim)
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(store.monitor_data, "codex", AT)
            assert arrived.wait(timeout=5)
            second = workers.submit(store.monitor_data, "codex", AT)
            try:
                assert joined.wait(timeout=5)
                assert readers.diagnostics()["active"] == 1
            finally:
                resume.set()
            pending = [first, second]
            assert all(future.result(timeout=5) == payload for future in pending)
        assert reads == [1]
        assert store.monitor_diagnostics()["entries"] == 1
        assert 0 < store.monitor_diagnostics()["estimatedBytes"] < 2048
        assert not store.monitor_cache.pending and not store.monitor_cache.controls


def test_hidden_monitor_read_cannot_repopulate_released_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import htd.adapters.sqlite.session_monitor as module

    arrived, resume = Event(), Event()

    def read(*args: object) -> dict[str, object]:
        arrived.set()
        assert resume.wait(timeout=5)
        return {"sample": "bounded"}

    monkeypatch.setattr(module, "read", read)
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = SqliteStatisticsStore(readers)
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(store.monitor_data, "codex", AT)
            assert arrived.wait(timeout=5)
            store.clear_analysis_cache()
            resume.set()
            assert pending.result(timeout=5) == {"sample": "bounded"}
        assert store.monitor_diagnostics() == {"entries": 0, "estimatedBytes": 0}


def test_monitor_observes_idle_boundaries_inside_one_minute(tmp_path: Path) -> None:
    started = Instant(AT.value + timedelta(seconds=30))
    ref = session_reference(Harness.CODEX, "session")
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, (usage(1, at=started),), at=started)
        store = SqliteStatisticsStore(readers)
        for seconds, present, frozen in (
            (599, True, False),
            (600, True, True),
            (1199, True, True),
            (1200, False, False),
        ):
            now = Instant(started.value + timedelta(seconds=seconds))
            statements: list[str] = []
            with readers.connection() as db:
                db.set_trace_callback(statements.append)
            result = store.monitor_data("codex", now, ref, chart_session_ref=ref)
            with readers.connection() as db:
                db.set_trace_callback(None)
            assert bool(result["sessionHitRates"]) == present
            if present:
                assert result["sessionHitRates"][0]["frozen"] == frozen
            else:
                assert result["selectedSessionRef"] is result["chartSessionRef"] is None
                assert not any(
                    "FROM session_cache_minute" in sql or "SELECT * FROM output_session" in sql
                    for sql in statements
                )


def test_monitor_reuses_shared_revisions_and_ignores_unrelated_wallet_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import htd.adapters.sqlite.session_monitor as module

    calls: list[int] = []
    original = module.read

    def counted(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "read", counted)
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, (usage(1),), at=AT)
        store = SqliteStatisticsStore(readers)
        before = store.monitor_data("codex", AT)
        writer.execute(lambda work: work.touch("wallet"))
        assert store.monitor_data("codex", AT) == before and calls == [1]
        collect(writer, (usage(2, harness=Harness.CLAUDE),), harness=Harness.CLAUDE, at=AT)
        writer.execute(lambda work: (work.touch("usage"), work.touch("output")))
        after = store.monitor_data("codex", AT)
        assert calls == [1, 1]
        assert after["revision"] > before["revision"]
        assert after["outputRevision"] > before["outputRevision"]
        assert after["sessionHitRates"] == before["sessionHitRates"]
        collect(writer, (usage(3),), offset=3, at=AT)
        assert store.monitor_data("codex", AT)["sessionTotals"]["recordCount"] == "2"
        assert calls == [1, 1, 1]


def test_monitor_hit_checks_snapshot_deadline_without_reparsing_each_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import htd.adapters.sqlite.statistics_store as module

    started = Instant(AT.value + timedelta(seconds=30))
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, tuple(usage(i, session=f"s-{i}", at=started) for i in range(1, 51)), at=started)
        store = SqliteStatisticsStore(readers)
        first = store.monitor_data("codex", Instant(started.value + timedelta(seconds=590)))
        parse = module.parse_instant
        parsed: list[str] = []

        def counted(value: str) -> Instant:
            parsed.append(value)
            return parse(value)

        monkeypatch.setattr(module, "parse_instant", counted)
        assert store.monitor_data("codex", Instant(started.value + timedelta(seconds=599))) == first
        assert not parsed
        expired = store.monitor_data("codex", Instant(started.value + timedelta(seconds=600)))
        assert parsed and all(row["frozen"] for row in expired["sessionHitRates"])


def test_directory_query_count_does_not_grow_with_session_count(tmp_path: Path) -> None:
    from htd.adapters.sqlite.session_monitor import read

    counts: list[int] = []
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        for total in (1, 50):
            collect(
                writer,
                tuple(usage(index, session=f"s-{index}") for index in range(1, total + 1)),
                offset=total,
                at=AT,
            )
            statements: list[str] = []
            with readers.connection() as db:
                db.set_trace_callback(statements.append)
                result = read(db, "codex", AT)
                db.set_trace_callback(None)
            assert len(result["sessionHitRates"]) == total
            counts.append(sum(sql.lstrip().upper().startswith(("SELECT", "WITH")) for sql in statements))
        assert counts[0] == counts[1] and counts[1] < 20


def test_cancelled_monitor_owner_keeps_one_read_for_active_follower(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import htd.adapters.sqlite.session_monitor as module
    from htd.application.ports.query_control import analysis_read, cancel_analysis
    from htd.domain.common.errors import DomainError

    arrived, resume, joined = Event(), Event(), Event()
    calls: list[int] = []

    def slow(*args: Any) -> dict[str, Any]:
        calls.append(1)
        arrived.set()
        assert resume.wait(5)
        return {"sample": "shared"}

    monkeypatch.setattr(module, "read", slow)
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = SqliteStatisticsStore(readers)
        claim = store.monitor_cache.claim

        def observed(*args: Any, **kwargs: Any) -> Any:
            future, owner = claim(*args, **kwargs)
            if not owner:
                joined.set()
            return future, owner

        monkeypatch.setattr(store.monitor_cache, "claim", observed)

        def read(identity: str) -> dict[str, Any]:
            with analysis_read(identity):
                return store.monitor_data("codex", AT)

        with ThreadPoolExecutor(2) as workers:
            owner = workers.submit(read, "monitor-owner")
            assert arrived.wait(5)
            follower = workers.submit(read, "monitor-follower")
            try:
                assert joined.wait(5)
                cancel_analysis("monitor-owner")
            finally:
                resume.set()
            with pytest.raises(DomainError, match="取消"):
                owner.result(timeout=5)
            assert follower.result(timeout=5) == {"sample": "shared"}
        assert calls == [1] and not store.monitor_cache.controls
        assert readers.diagnostics()["active"] == 0
