"""Seventy-two-hour retirement preserves sixty-two-day numeric scope capability."""

from datetime import timedelta
from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.recent_usage import fold
from htd.adapters.sqlite.rollup_repo import RollupRepository
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.writer import WriteExecutor
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant
from htd.domain.usage.model import Harness
from htd.domain.usage.session import session_reference
from htd.ipc.dto.explorer import ExploreResponse

from .collection_helpers import collect, quote, usage
from .test_explorer_043 import AT, bill, request, setup


def test_recent_minutes_keep_scopes_and_conserve_after_retirement_then_expire(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        setup(writer)
        bill(
            writer,
            tuple(
                usage(
                    index,
                    at=Instant(AT.value + timedelta(seconds=index * 10)),
                    session=session,
                    model="gpt-6-astra",
                )
                for index, session in enumerate(("A", "B"), 1)
            ),
        )
        bill(
            writer,
            (
                usage(
                    3,
                    at=Instant(AT.value + timedelta(seconds=30)),
                    harness=Harness.ZCODE,
                    session=None,
                    model="gpt-6-astra",
                ),
            ),
        )
        store = SqliteStatisticsStore(readers)
        query = request(grain="hour", sections=["totals", "models", "trend"])
        initial = ExploreResponse.model_validate(store.explore(query, AT))
        refs = [session_reference(Harness.CODEX, "A"), session_reference(Harness.CODEX, "B"), ""]
        for days in (4, 61):
            now = Instant(AT.value + timedelta(days=days))
            writer.execute(
                lambda w, now=now: RollupRepository(w.connection).compact(w, at=now, publish_daily=False)
            )
            total = ExploreResponse.model_validate(store.explore(query, now))
            assert total.grain == "hour" and total.totals.recordCount == "3"
            assert total.totals.inputTokens == initial.totals.inputTokens
            scoped = [
                ExploreResponse.model_validate(store.explore(dict(query, sessionRefs=[ref]), now))
                for ref in refs
            ]
            assert [row.totals.recordCount for row in scoped] == ["1", "1", "1"]
            assert sum(int(row.totals.picoUsd) for row in scoped) == int(total.totals.picoUsd)
            assert sum(int(row.totals.totalTokens) for row in scoped) == int(total.totals.totalTokens)
            batch = (
                readers.get()
                .execute("SELECT id FROM session_activity_batch WHERE session_ref=?", (refs[0],))
                .fetchone()[0]
            )
            activity = ExploreResponse.model_validate(
                store.explore(dict(query, activityBatchIds=[batch]), now)
            )
            assert activity.totals == scoped[0].totals
        expired = Instant(AT.value + timedelta(days=63))
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=expired, publish_daily=False))
        with pytest.raises(DomainError, match="旧历史"):
            store.explore(dict(query, sessionRefs=[refs[0]]), expired)
        after = ExploreResponse.model_validate(store.explore(query, expired))
        assert after.grain == "day" and after.totals.totalTokens == initial.totals.totalTokens
        assert after.totals.picoUsd == initial.totals.picoUsd


def test_retired_partial_minute_reports_expanded_bounds_instead_of_fake_precision(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        setup(writer)
        bill(writer, (usage(1, at=Instant(AT.value + timedelta(seconds=10)), model="gpt-6-astra"),))
        now = Instant(AT.value + timedelta(days=4))
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=now, publish_daily=False))
        query = dict(
            request(grain="hour"),
            start=format_instant(Instant(AT.value + timedelta(seconds=15))),
            end=format_instant(Instant(AT.value + timedelta(seconds=45))),
        )
        data = ExploreResponse.model_validate(SqliteStatisticsStore(readers).explore(query, now))
        assert data.start == format_instant(AT)
        assert data.end == format_instant(Instant(AT.value + timedelta(minutes=1)))
        assert data.totals.recordCount == "1" and any(row.code == "boundary_minute" for row in data.coverage)


def test_pending_tokens_are_visible_once_in_all_and_session_queries(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        setup(writer)
        event = usage(1, at=AT, model="gpt-6-astra")
        collect(writer, (event,), at=AT)
        store = SqliteStatisticsStore(readers)
        query = request(sessionRefs=[session_reference(Harness.CODEX, "session")])
        before = ExploreResponse.model_validate(store.explore(query, AT))
        assert before.totals.recordCount == "1" and before.totals.pendingValuationCount == "1"
        assert before.totals.inputTokens == "100" and before.totals.picoUsd == "0"
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
        after = ExploreResponse.model_validate(store.explore(query, AT))
        assert after.totals.recordCount == "1" and after.totals.pendingValuationCount == "0"
        assert after.totals.inputTokens == before.totals.inputTokens
        assert after.totals.picoUsd == "84000000"


def test_sixty_two_day_boundary_keeps_the_whole_business_day_detailed(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        setup(writer)
        bill(writer, (usage(1, at=AT, model="gpt-6-astra"),))
        writer.execute(
            lambda w: RollupRepository(w.connection).compact(
                w, at=Instant(AT.value + timedelta(days=4)), publish_daily=False
            )
        )
        boundary = Instant(AT.value + timedelta(days=62, hours=1))
        assert writer.execute(lambda w: fold(w.connection, boundary)) == 0
        with readers.connection() as db:
            assert db.execute("SELECT COUNT(*) FROM usage_recent").fetchone()[0] == 1
            assert not db.execute("SELECT 1 FROM usage_dimensions").fetchone()
        assert writer.execute(lambda w: fold(w.connection, Instant(boundary.value + timedelta(days=1)))) == 1
