"""Session cache hit rate survives restart and expires its temporary prior generation."""

from pathlib import Path

from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.usage.model import Harness, Tokens, UsageEvent
from htd.ipc.dto.statistics import CacheMonitorResponse

from backend.tests.integration.collection_helpers import AT, collect, ensure_source, quote, usage


def _bill(app: Application, events: tuple[UsageEvent, ...]) -> None:
    by_harness: dict[Harness, list[UsageEvent]] = {}
    for event in events:
        by_harness.setdefault(event.harness, []).append(event)
    for harness, records in by_harness.items():
        root = ensure_source(app.writer, harness)
        app.writer.execute(
            lambda work, harness=harness, root=root: SourcesRepository(work.connection).set_path(
                work, harness, root, available=True
            )
        )
        collect(app.writer, tuple(records), harness=harness, at=app.clock.now_utc())
    app.writer.execute(
        lambda work: IngestRepository(work.connection).bill_batch(
            work, quote, at=app.clock.now_utc(), limit=500
        )
    )


def _sessions(app: Application) -> list[dict[str, object]]:
    response = CacheMonitorResponse.model_validate(app.statistics.cache_monitor(Harness.CODEX, "minute", 0))
    return [item.model_dump() for item in response.sessionHitRates]


def test_concurrent_sessions_freeze_resume_and_clear_previous_generation(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    data = tmp_path / "data"
    with Application(data, clock=clock, start_scheduler=False) as app:
        _bill(
            app,
            (
                usage(1, session="one", tokens=Tokens(input=100, cache_read=40, cache_write=0, output=10)),
                usage(2, session="two", tokens=Tokens(input=100, cache_read=70, cache_write=0, output=10)),
            ),
        )
        first = _sessions(app)
        assert {row["cacheHitRate"] for row in first} == {0.4, 0.7}
        assert len(first) == 2 and all(not row["frozen"] for row in first)

        clock.advance(5 * 60_000)
        _bill(
            app,
            (usage(3, session="one", tokens=Tokens(input=300, cache_read=180, cache_write=0, output=20)),),
        )
        first = _sessions(app)
        one = next(row for row in first if row["cacheHitRate"] == 0.55)
        assert one["displayName"] == "Model"

        clock.advance(10 * 60_000)
        frozen = _sessions(app)
        assert len(frozen) == 2 and all(row["frozen"] for row in frozen)

    with Application(data, clock=clock, start_scheduler=False) as app:
        restored = _sessions(app)
        assert len(restored) == 2 and all(row["frozen"] for row in restored)
        clock.advance(60_000)
        _bill(
            app, (usage(4, session="three", tokens=Tokens(input=50, cache_read=25, cache_write=0, output=5)),)
        )
        current = _sessions(app)
        assert len(current) == 1 and current[0]["cacheHitRate"] == 0.5 and not current[0]["frozen"]
        assert (
            app.writer.execute(
                lambda work: work.connection.execute("SELECT COUNT(*) FROM session_hit_rate").fetchone()[0]
            )
            == 1
        )


def test_unknown_cache_does_not_enter_the_rate_and_marks_partial_coverage(tmp_path: Path) -> None:
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as app:
        _bill(
            app,
            (
                usage(
                    1, session="partial", tokens=Tokens(input=100, cache_read=25, cache_write=0, output=10)
                ),
                usage(
                    2, session="partial", tokens=Tokens(input=100, cache_read=None, cache_write=0, output=10)
                ),
            ),
        )
        result = _sessions(app)
        assert len(result) == 1
        assert result[0]["cacheHitRate"] == 0.25
        assert result[0]["cacheCoverageIncomplete"] is True
