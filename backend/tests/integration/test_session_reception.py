"""Transaction A owns Token; transaction B owns price, and quiet evidence remains per session."""

from datetime import timedelta
from pathlib import Path

from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.scheduler_store import SqliteSchedulerStore
from htd.adapters.sqlite.session_activity import seal, source_observed
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.statistics import Statistics
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant
from htd.domain.statistics.window import Window
from htd.domain.usage.model import Harness
from htd.domain.usage.session import SourceObservation, session_reference
from htd.jobs.scheduler import Scheduler

from .collection_helpers import AT, collect, ensure_source, quote, usage
from .scan_helpers import scanner, seed_prices
from .test_collection_scheduler import AdvancingWait


def test_accepted_tokens_are_immediate_and_billing_does_not_add_them_again(tmp_path: Path) -> None:
    database = tmp_path / "data.sqlite3"
    with WriteExecutor(database) as writer, ReadPool(database) as readers:
        statistics = Statistics(SqliteStatisticsStore(readers, writer), FakeClock(AT))
        window = Window(Instant(AT.value - timedelta(seconds=1)), Instant(AT.value + timedelta(seconds=1)), 0)
        assert statistics.summary(Harness.CODEX, window)[0].metrics.record_count == 0
        event = usage(1)
        collect(writer, (event,))
        result = statistics.summary(Harness.CODEX, window)[0].metrics
        assert (result.record_count, result.input_tokens, result.output_tokens) == (1, 100, 10)
        assert result.pending_valuation_count == 1 and result.pico_usd == "0"
        writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
        result = statistics.summary(Harness.CODEX, window)[0].metrics
        assert (result.record_count, result.input_tokens, result.pending_valuation_count) == (1, 100, 0)
        assert result.pico_usd == "84000000"
        assert writer.execute(
            lambda work: [
                tuple(row)
                for row in work.connection.execute("SELECT records,input,pending,estimate FROM session_total")
            ]
        ) == [("1", "100", "0", "84000000")]
        collect(writer, (event,))
    with WriteExecutor(database) as writer:
        assert (
            writer.execute(
                lambda work: work.connection.execute("SELECT records FROM session_total").fetchone()[0]
            )
            == "1"
        )


def test_activity_seals_only_quiet_complete_sessions_and_pause_preserves_uncertainty(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        collect(writer, (usage(1, session="A"), usage(2, session="B")))
        writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
        later = Instant(AT.value + timedelta(minutes=9))
        collect(writer, (usage(3, session="A", at=later),), at=later)
        writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=later))
        writer.execute(
            lambda work: source_observed(work.connection, SourceObservation(Harness.CODEX, later, "ready"))
        )
        assert (
            writer.execute(lambda work: seal(work.connection, Instant(AT.value + timedelta(minutes=11)))) == 1
        )
        states = dict(
            writer.execute(
                lambda work: [
                    tuple(row)
                    for row in work.connection.execute("SELECT session_ref,state FROM session_activity_batch")
                ]
            )
        )
        assert states[session_reference(Harness.CODEX, "A")] == "open"
        assert states[session_reference(Harness.CODEX, "B")] == "sealed"
        writer.execute(
            lambda work: work.connection.execute(
                "INSERT INTO runtime_state VALUES('collection_paused','true')"
            )
        )
        assert (
            writer.execute(lambda work: seal(work.connection, Instant(AT.value + timedelta(minutes=25)))) == 0
        )
        assert (
            writer.execute(
                lambda work: work.connection.execute(
                    "SELECT input FROM session_total WHERE session_ref=?",
                    (session_reference(Harness.CODEX, "A"),),
                ).fetchone()[0]
            )
            == "200"
        )


def test_all_disabled_sources_perform_no_discovery_and_resume_keeps_checkpoint(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer, ReadPool(writer.path) as readers:
        for harness in Harness:
            ensure_source(writer, harness)
            writer.execute(
                lambda work, harness=harness: SourcesRepository(work.connection).set_enabled(
                    work, harness, False, AT
                )
            )
        scan = scanner(writer, readers, tmp_path, FakeClock(AT))
        scan.discovery.default_path = lambda *_: (_ for _ in ()).throw(AssertionError("disabled discovery"))  # type: ignore[method-assign]
        assert scan.collect() == ()
        writer.execute(
            lambda work: SourcesRepository(work.connection).set_enabled(work, Harness.CODEX, True, AT)
        )
        source = writer.execute(lambda work: SourcesRepository(work.connection).get(Harness.CODEX))
        assert source and source.scan_enabled and source.started_at is None


def test_paused_coordinator_drains_previously_accepted_inbox_without_source_reads(tmp_path: Path) -> None:
    class Maintenance:
        def run(self, *, more: bool, paused: bool, should_stop: object) -> None:
            pass

    with WriteExecutor(tmp_path / "data.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(lambda work: seed_prices(work, AT))
        collect(writer, (usage(1, model="gpt-6-astra"),))
        store = SqliteSchedulerStore(writer, readers)
        store.set_paused(True)
        clock = FakeClock(AT)
        scan = scanner(writer, readers, tmp_path, clock)
        wait = AdvancingWait()
        scheduler = Scheduler(
            scan,
            store,
            clock,
            wait=wait,
            maintenance=Maintenance(),
            drain_inbox=lambda stop: scan.billing.run_batch(should_stop=stop),
        )
        scheduler.start()
        try:
            wait.parked()
            assert (
                writer.execute(
                    lambda work: work.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
                )
                == 1
            )
            assert (
                writer.execute(
                    lambda work: work.connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
                )
                == 0
            )
            assert scheduler.status().state == "paused"
        finally:
            scheduler.close(2)
