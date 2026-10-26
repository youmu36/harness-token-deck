"""S-series expectations through the real router/WAL ledger and revision-aware cache."""

import logging
from collections.abc import Generator, Iterable, Iterator
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import Literal

import pytest
from htd.adapters.sqlite.ledger_read import LedgerReader
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics import LedgerRow
from htd.domain.statistics.window import Grain
from htd.domain.usage.model import Harness
from htd.ipc.dto.statistics import CacheMonitorResponse, SummaryResponse

from backend.tests.unit.statistics.helpers import row

from .command_helpers import failure, invoke, seed_rows


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Application]:
    clock = FakeClock(parse_instant("2026-09-13T02:30:45Z"))
    with Application(tmp_path / "data", clock=clock, start_scheduler=False, cache_capacity=2) as value:
        yield value


def query(
    app: Application,
    *,
    start: str = "2026-09-13T00:00:00Z",
    end: str = "2026-09-13T02:00:00Z",
    grain: Grain | None = "hour",
    offset: int = 480,
    harness: str = "all",
) -> SummaryResponse:
    return invoke(
        app,
        "stats.summary",
        {
            "harness": harness,
            "start": format_instant(parse_instant(start)),
            "end": format_instant(parse_instant(end)),
            "offsetMinutes": offset,
            "grain": grain,
        },
        SummaryResponse,
    )


@pytest.mark.parametrize("pico", [84_000_000, 9_007_199_254_740_993], ids=["S-01", "S-02"])
def test_hourly_totals_and_exact_pico(app: Application, pico: int) -> None:
    seed_rows(
        app,
        [
            row(1, input_count=100, cache=20, output=10, pico=pico),
            row(2, model="b", input_count=300, cache=180, output=20, pico=pico),
            row(3, model=None, input_count=500, cache=None, output=30, pico=pico),
            row(4, hour=1, input_count=200, cache=100, output=40, pico=pico),
        ],
    )
    result = query(app)
    first = result.hourlyTotals[0].metrics
    assert (first.recordCount, first.inputTokens, first.cacheReadTokens, first.outputTokens) == (
        3,
        900,
        200,
        60,
    )
    assert first.cacheHitRate == 0.5 and first.cacheCoverageIncomplete and first.unknownModelCount == 1
    assert (len(result.hourlyTotals), len(result.hourly)) == (2, 4)
    assert result.metrics.picoUsd == str(4 * pico)
    assert result.metrics.cacheHitRate == 0.5
    assert all(item.displayName == "全部模型" for item in result.hourlyTotals)


def test_S03_cache_bust_weighted_hit(app: Application) -> None:
    seed_rows(app, [row(1, input_count=50_000, cache=0), row(2, input_count=50_000, cache=50_000)])
    result = query(app, grain="minute")
    assert result.trend[0].cacheBust and result.trend[0].cacheHitRate == 0.5


def test_two_hour_trend_preserves_weighting_money_hourly_details_and_cache(app: Application) -> None:
    pico = 9_007_199_254_740_993
    seed_rows(
        app,
        [
            row(1, input_count=100, cache=10, pico=pico),
            row(2, hour=1, input_count=900, cache=810, pico=pico),
            row(3, hour=1, input_count=10_000, cache=None, pico=pico),
            row(4, hour=2, input_count=500, cache=250, pico=pico),
        ],
    )
    result = query(app, grain="twoHours", end="2026-09-20T00:00:00Z", offset=0)
    assert result.grain == "twoHours" and len(result.trend) == 84
    assert result.trend[0].recordCount == 3
    assert result.trend[0].cacheHitRate == pytest.approx(0.82)
    assert result.trend[0].picoUsd == str(3 * pico)
    assert result.trend[1].cacheHitRate == 0.5
    assert result.trend[2].recordCount == 0 and result.trend[2].cacheHitRate is None
    assert len(result.hourlyTotals) == 3
    assert sum(point.inputTokens for point in result.trend) == result.metrics.inputTokens
    assert query(app, grain="twoHours", end="2026-09-20T00:00:00Z", offset=0) == result
    hourly = query(app, grain="hour", end="2026-09-20T00:00:00Z", offset=0)
    assert len(hourly.trend) == 168 and hourly.metrics == result.metrics


def test_S04_unknown_cache_and_S05_null_usage(app: Application) -> None:
    seed_rows(app, [row(1, cache=None)])
    result = query(app)
    assert result.metrics.cacheHitRate is None and result.metrics.cacheCoverageIncomplete
    app.writer.execute(lambda work: work.connection.execute("DELETE FROM ledger"))
    seed_rows(app, [row(2), row(3, input_count=None, cache=None, output=None, incomplete=True)])
    result = query(app)
    assert result.metrics.recordCount == 2 and result.metrics.cacheHitRate == 0.4
    assert result.metrics.incomplete and result.metrics.missingUsageCount == 1
    assert not result.metrics.cacheCoverageIncomplete


@pytest.mark.parametrize("harness", [Harness.ZCODE, Harness.CODEX])
def test_S06_grok_harness_specific(app: Application, harness: Harness) -> None:
    seed_rows(
        app,
        [
            row(1, harness=harness, model="grok", cache=0),
            row(2, harness=harness, model="grok", cache=80),
            row(3, harness=harness, model="m", cache=0),
            row(4, harness=harness, model="m", cache=80),
        ],
    )
    result = query(app, harness=harness.value)
    assert result.metrics.cacheHitRate == (160 / 300 if harness == Harness.ZCODE else 0.4)
    assert next(item for item in result.models if item.modelKey == "grok").cacheHitRate == (
        0.8 if harness == Harness.ZCODE else 0.4
    )


def test_S07_session_loss_and_S08_unprobed_grok(app: Application) -> None:
    seed_rows(
        app,
        [
            row(1, 1, session="fresh", input_count=1000, cache=0),
            row(2, 5, session="dropped", input_count=1000, cache=0),
            row(3, 10, session="dropped", input_count=70_000, cache=60_000),
            row(4, 20, session="dropped", input_count=70_000, cache=10_000),
            row(5, 12, session="steady", input_count=70_000, cache=60_000),
            row(6, 15, session="steady", input_count=70_000, cache=30_000),
            row(7, harness=Harness.ZCODE, model="grok", input_count=70_000, cache=60_000),
            row(8, 1, harness=Harness.ZCODE, model="grok", input_count=70_000, cache=0),
        ],
    )
    result = query(app, end="2026-09-13T00:30:00Z", grain="minute", offset=0)
    assert [point.label for point in result.trend if point.cacheLoss] == ["09-13 00:20"]
    assert not any(point.cacheLoss for point in query(app, harness="zcode", grain="minute").trend)


def test_S09_overview_and_single_harness(app: Application) -> None:
    seed_rows(app, (row(index, harness=harness) for index, harness in enumerate(Harness, 1)))
    overview = query(app)
    assert (overview.metrics.recordCount, overview.metrics.inputTokens, overview.metrics.cacheReadTokens) == (
        4,
        400,
        160,
    )
    assert [item.harness for item in overview.harnesses] == ["codex", "zcode", "dsh", "claude"]
    assert [item.displayName for item in overview.harnesses] == [
        "Codex",
        "Zcode",
        "Deepseek Harness",
        "Claude Code",
    ]
    single = query(app, harness="dsh")
    assert single.metrics.recordCount == 1 and single.harnesses == []


@pytest.mark.parametrize(
    ("start", "end", "labels", "counts"),
    [
        ("2026-09-13T00:00:00Z", "2026-09-13T00:02:00Z", ["09-13 00:00", "09-13 00:01"], [1, 0]),
        ("2026-09-12T23:59:00Z", "2026-09-13T00:01:00Z", ["09-12 23:59", "09-13 00:00"], [1, 1]),
    ],
)
def test_S10_half_open_midnight(
    app: Application, start: str, end: str, labels: list[str], counts: list[int]
) -> None:
    seed_rows(app, [row(1, 59, hour=23, day=12), row(2), row(3, 2)])
    result = query(app, start=start, end=end, offset=0, grain="minute")
    assert [point.label for point in result.trend] == labels
    assert [point.recordCount for point in result.trend] == counts


@pytest.mark.parametrize(
    ("end", "grain"),
    [
        ("2026-09-04T00:00:00Z", "hour"),
        ("2026-09-04T00:01:00Z", "hour"),
        ("2026-10-01T00:00:00Z", "hour"),
    ],
)
def test_S11_S12_range_and_grain(app: Application, end: str, grain: str) -> None:
    result = query(app, start="2026-09-01T00:00:00Z", end=end, grain="hour")
    assert result.grain == grain and result.trend == []


def test_S13_display_name_and_S14_billed_prices(app: Application) -> None:
    seed_rows(
        app,
        [
            row(
                1,
                30,
                model="GPT-6-ASTRA",
                display="Astra",
                input_count=1_000_000,
                cache=200_000,
                output=100_000,
                pico=3_050_000_000_000,
            ),
            row(
                2,
                hour=1,
                model="GPT-6-ASTRA",
                display="最新名称",
                input_count=1_000_000,
                cache=0,
                output=0,
                pico=5_000_000_000_000,
            ),
        ],
    )
    result = query(app)
    assert result.metrics.picoUsd == "8050000000000"
    assert result.models[0].modelKey == "gpt-6-astra" and result.models[0].displayName == "GPT-6-ASTRA"
    assert result.hourly[0].displayName == "GPT-6-ASTRA"


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"end": "2026-09-13T00:00:00.000Z"}, "RANGE_INVALID"),
        ({"end": "2026-10-14T00:00:00.000Z"}, "RANGE_TOO_LONG"),
        ({"offsetMinutes": True}, "INVALID_REQUEST"),
        ({"offsetMinutes": 841}, "INVALID_REQUEST"),
        ({"grain": "week"}, "INVALID_REQUEST"),
        ({"harness": "bad"}, "INVALID_REQUEST"),
        ({"extra": 1}, "INVALID_REQUEST"),
        ({"start": "2026-09-13T00:00:00Z"}, "INVALID_REQUEST"),
    ],
)
def test_range_and_strict_query_errors(app: Application, patch: dict[str, object], code: str) -> None:
    error = failure(
        app,
        "stats.summary",
        {
            "harness": "all",
            "start": "2026-09-13T00:00:00.000Z",
            "end": "2026-09-13T01:00:00.000Z",
            "offsetMinutes": 0,
        }
        | patch,
    )
    assert error.code == code


def test_count_overflow_is_structured(app: Application) -> None:
    seed_rows(
        app, [row(1, input_count=2**53 - 1, cache=0, output=0), row(2, input_count=1, cache=0, output=0)]
    )
    assert (
        failure(
            app,
            "stats.summary",
            {
                "harness": "all",
                "start": "2026-09-13T00:00:00.000Z",
                "end": "2026-09-13T01:00:00.000Z",
                "offsetMinutes": 0,
            },
        ).code
        == "COUNT_OVERFLOW"
    )


def track_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = LedgerReader.iter_rows

    def read(
        self: LedgerReader,
        start: Instant,
        end: Instant,
        *,
        harness: Harness | Literal["all"] = "all",
        model_key: str | None = None,
        coarse: bool = False,
    ) -> Generator[LedgerRow, None, None]:
        calls.append(1)
        return original(self, start, end, harness=harness, model_key=model_key, coarse=coarse)

    monkeypatch.setattr(LedgerReader, "iter_rows", read)
    return calls


def test_partial_input_preserves_domain_new_input(app: Application) -> None:
    seed_rows(app, [row(1, input_count=None, cache=40, incomplete=True)])
    result = query(app)
    assert result.metrics.newInput == -40 and result.metrics.missingUsageCount == 1
    assert result.metrics.cacheHitRate is None


def test_aggregate_pico_is_not_limited_to_one_row(app: Application) -> None:
    seed_rows(app, (row(index, pico=2**126) for index in range(100)))
    assert query(app).metrics.picoUsd == str(100 * 2**126)


def test_cache_hits_lru_eviction_revision_invalidation(
    app: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_rows(app, [row(1)])
    calls = track_reads(monkeypatch)
    first = query(app)
    assert query(app) == first and len(calls) == 1
    app.writer.execute(lambda work: work.touch("preferences"))
    assert query(app) == first and len(calls) == 1
    query(app, harness="codex")
    query(app)  # all is most recently used.
    query(app, harness="dsh")
    assert len(calls) == 3
    query(app, harness="codex")  # Evicted.
    assert len(calls) == 4
    seed_rows(app, [row(2)])
    changed = query(app)
    assert changed.metrics.recordCount == 2 and changed.revision == first.revision + 1
    assert len(calls) == 5
    app.writer.execute(lambda work: (work.connection.execute("DELETE FROM ledger"), work.touch("usage")))
    assert query(app).metrics.recordCount == 0 and len(calls) == 6


def test_revision_and_rows_share_snapshot(app: Application, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_rows(app, [row(1)])
    original = LedgerReader.iter_rows
    inserted = False

    def read(
        self: LedgerReader,
        start: Instant,
        end: Instant,
        *,
        harness: Harness | Literal["all"] = "all",
        model_key: str | None = None,
        coarse: bool = False,
    ) -> Generator[LedgerRow, None, None]:
        nonlocal inserted
        if not inserted:
            inserted = True
            seed_rows(app, [row(2)])
        return original(self, start, end, harness=harness, model_key=model_key, coarse=coarse)

    monkeypatch.setattr(LedgerReader, "iter_rows", read)
    first = query(app)
    second = query(app)
    assert first.metrics.recordCount == 1 and second.metrics.recordCount == 2
    assert second.revision == first.revision + 1


def test_cache_expires_idle_snapshots_and_bypasses_oversized_responses(
    app: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_rows(app, [row(1)])
    calls = track_reads(monkeypatch)
    assert query(app).metrics.recordCount == 1
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(60_001)
    assert query(app).metrics.recordCount == 1 and len(calls) == 2
    app.statistics.invalidate()
    app.statistics.max_bytes = 1
    assert query(app).metrics.recordCount == query(app).metrics.recordCount == 1
    assert len(calls) == 4 and app.statistics.diagnostics()["entries"] == 0


def test_monitor_clock_window_hits_minute_roll_and_usage_refresh(app: Application) -> None:
    from backend.tests.contracts.maintenance_helpers import add_usage
    from backend.tests.integration.collection_helpers import usage

    add_usage(app, [usage(1, at=parse_instant("2026-09-13T02:29:00Z"))])
    payload = {"harness": "codex", "mode": "minute", "offsetMinutes": 480}
    first = invoke(app, "stats.cacheMonitor", payload, CacheMonitorResponse)
    assert first.end == "2026-09-13T02:30:00.000Z" and len(first.trend) == 60
    assert first.windowTotals.recordCount == "1"
    assert invoke(app, "stats.cacheMonitor", payload, CacheMonitorResponse) == first
    add_usage(app, [usage(2, at=parse_instant("2026-09-13T02:29:00Z"))])
    refreshed = invoke(app, "stats.cacheMonitor", payload, CacheMonitorResponse)
    assert refreshed.revision > first.revision and refreshed.windowTotals.recordCount == "2"
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(60_000)
    rolled = invoke(app, "stats.cacheMonitor", payload, CacheMonitorResponse)
    assert rolled.windowKey != first.windowKey


@pytest.mark.slow
def test_benchmark_30_days_100000_rows_through_router(app: Application) -> None:
    start = parse_instant("2026-09-01T00:00:00Z")

    def rows() -> Iterable[LedgerRow]:
        for index in range(100_000):
            yield LedgerRow(
                list(Harness)[index % 3],
                f"{index:064x}",
                f"session-{index % 100}",
                Instant(start.value + timedelta(seconds=index * 25)),
                f"model-{index % 10}",
                100,
                40,
                10,
                84_000_000,
                False,
                False,
                f"模型 {index % 10}",
            )

    seed_rows(app, rows())
    began = perf_counter()
    result = query(app, start="2026-09-01T00:00:00Z", end="2026-10-01T00:00:00Z", grain="day")
    elapsed = (perf_counter() - began) * 1000
    began = perf_counter()
    cached = query(app, start="2026-09-01T00:00:00Z", end="2026-10-01T00:00:00Z", grain="day")
    hit = (perf_counter() - began) * 1000
    assert cached == result and result.metrics.recordCount == 100_000
    assert result.metrics.picoUsd == "8400000000000" and len(result.harnesses) == 3
    logging.getLogger(__name__).warning(
        "P4.3 30d/100000/all router: cold=%.2f ms, cache=%.2f ms", elapsed, hit
    )
