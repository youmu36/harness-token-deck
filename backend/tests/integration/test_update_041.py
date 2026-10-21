"""Exact multi-tier analysis, completion-minute unions and quiet remote observations."""

from datetime import timedelta
from pathlib import Path

import pytest
from htd.adapters.sqlite.analysis_read import analyze
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.counter_codec import compress, decompress
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.rollup_repo import RollupRepository
from htd.adapters.sqlite.usage_archive import archive_minutes
from htd.adapters.sqlite.usage_daily import publish
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, parse_instant
from htd.domain.statistics.analysis import AnalysisQuery, analysis_bounds
from htd.domain.statistics.window import Window
from htd.domain.usage.model import Harness, Tokens
from htd.ipc.dto.statistics import AnalysisResponse

from backend.tests.integration.collection_helpers import AT, collect, quote, usage
from backend.tests.integration.test_account_book import observe, setup, summary
from backend.tests.integration.test_history_rollup import LATER, seed
from backend.tests.integration.test_wallet_storage import USD


def test_long_history_is_exact_across_all_tiers_and_unions_sources(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        seed(writer)
        collect(writer, (usage(101, harness=Harness.CLAUDE, model="Other"),), harness=Harness.CLAUDE)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
        now = parse_instant("2027-01-01T00:00:00Z")

        def read():
            return AnalysisResponse.model_validate(
                writer.execute(lambda w: analyze(w.connection, AnalysisQuery(period="all"), now))
            )

        before = read()
        assert before.totals.recordCount == "101"
        assert before.totals.activeMinutes == "1"
        assert before.totals.outputTokens == "1010"
        assert before.totals.tokensPerSecond == "16.83"
        assert len(before.harnesses) == 2 and len(before.models) == 2
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=LATER))
        assert read().totals.outputTokens == before.totals.outputTokens
        cold_at = Instant(AT.value + timedelta(days=40))
        writer.execute(lambda w: archive_minutes(w.connection, w, at=cold_at))
        archived = read()
        assert archived.totals.activeMinutes == "1"
        assert archived.totals.picoUsd == before.totals.picoUsd
        writer.execute(lambda w: publish(w.connection, w, at=now))
        daily = read()
        assert daily.totals.recordCount == before.totals.recordCount
        assert daily.totals.activeMinutes == "1" and daily.totals.picoUsd == before.totals.picoUsd
        assert "request_timing" in daily.totals.missingCapabilities
        page = writer.execute(lambda w: analyze(w.connection, AnalysisQuery(period="all", limit=1), now))
        assert page["totalModels"] == 2 and page["nextOffset"] == 1
        assert len(page["models"]) == 1


def test_big_counts_and_whole_boundary_days_are_not_rounded(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        seed(writer)
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=LATER))
        now = parse_instant("2027-01-01T00:00:00Z")
        writer.execute(lambda w: publish(w.connection, w, at=now))
        large = 2**53 + 123

        def enlarge(work: UnitOfWork) -> None:
            record = work.connection.execute("SELECT * FROM usage_daily").fetchone()
            counter = decompress(record["payload"])
            counter["output"] = large
            work.connection.execute("UPDATE usage_daily SET payload=?", (compress(counter),))

        writer.execute(enlarge)
        result = AnalysisResponse.model_validate(
            writer.execute(
                lambda w: analyze(
                    w.connection,
                    AnalysisQuery(
                        period="range", start="2026-09-30T01:00:00.000Z", end="2026-09-30T02:00:00.000Z"
                    ),
                    now,
                )
            )
        )
        assert result.totals.outputTokens == str(large)
        assert result.includesWholeBoundaryDays
        assert result.comparison is not None
        # Recent summary keeps its existing limit rather than opening unbounded charts.
        with pytest.raises(DomainError, match="30"):
            Window(AT, now, 0)


def test_calendar_progress_leap_days_empty_baseline_and_bounded_years(tmp_path: Path) -> None:
    leap = parse_instant("2024-02-29T12:00:00Z")
    start, end, previous, _ = analysis_bounds(AnalysisQuery(period="year", year=2024), leap, "UTC", None)
    assert start == parse_instant("2024-01-01T00:00:00Z") and end == leap
    assert previous is not None and previous[1] == parse_instant("2023-02-28T12:00:00Z")
    _, _, _, quarter_grain = analysis_bounds(
        AnalysisQuery(period="quarter", year=2024, quarter=1), leap, "UTC", None
    )
    assert quarter_grain == "month"
    with WriteExecutor(tmp_path / "a.db") as writer:
        seed(writer)
        response = AnalysisResponse.model_validate(
            writer.execute(
                lambda w: analyze(
                    w.connection,
                    AnalysisQuery(period="years", year=1971, end_year=2070),
                    parse_instant("2071-01-01T00:00:00Z"),
                )
            )
        )
        assert response.grain == "year" and len(response.trend) == 100
        assert response.comparison is not None and all(
            value is None for value in response.comparison.changes.values()
        )


def test_zero_or_unknown_output_does_not_invent_active_samples(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        events = (usage(1, tokens=Tokens(100, 50, 0, 0, 0)), usage(2, tokens=Tokens(100, None, 0, None, 0)))
        collect(writer, events)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
        result = AnalysisResponse.model_validate(
            writer.execute(lambda w: analyze(w.connection, AnalysisQuery(period="all"), LATER))
        )
        assert result.totals.activeMinutes == "0" and result.totals.tokensPerMinute is None
        assert result.totals.cacheHitRate == 0.5
        assert result.totals.cacheCoverageIncomplete
        assert "complete_output_samples" in result.totals.missingCapabilities


def test_unchanged_remote_balance_advances_evidence_without_zero_postings(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10", used=0)
        for minute in (5, 10, 15):
            observe(store, 10 * USD, minute, 0)
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM account_posting").fetchone()[0]
            )
            == 0
        )
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM account_endpoint").fetchone()[0]
            )
            == 4
        )
        assert summary(writer)["observedAt"] is not None
        observe(store, 9 * USD, 20, USD)
        assert summary(writer)["C"] == str(USD)
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM account_posting").fetchone()[0]
            )
            == 1
        )
