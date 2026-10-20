"""S-01 through S-14, with exact finance and weighted cache expectations."""

import pytest
from htu.domain.billing import estimate_cost
from htu.domain.common.errors import DomainError
from htu.domain.common.time import parse_instant
from htu.domain.pricing.model import ModelRate
from htu.domain.statistics.window import Window
from htu.domain.usage.model import Harness, Tokens

from .helpers import row, summarize


@pytest.mark.parametrize("pico", [84_000_000, 9_007_199_254_740_993], ids=["S-01", "S-02"])
def test_S01_S02_hourly_totals(pico: int) -> None:
    summary = summarize(
        [
            row(1, input_count=100, cache=20, output=10, pico=pico),
            row(2, model="b", input_count=300, cache=180, output=20, pico=pico),
            row(3, model=None, input_count=500, cache=None, output=30, pico=pico),
            row(4, hour=1, input_count=200, cache=100, output=40, pico=pico),
        ]
    )
    first = summary.hourly_totals[0].metrics
    assert (first.record_count, first.input_tokens, first.cache_read_tokens, first.output_tokens) == (
        3,
        900,
        200,
        60,
    )
    assert first.cache_hit_rate == 0.5
    assert first.cache_coverage_incomplete
    assert first.unknown_model_count == 1
    assert (len(summary.hourly_totals), len(summary.hourly)) == (2, 4)
    assert summary.metrics.pico_usd == str(4 * pico)
    for name in ("record_count", "input_tokens", "output_tokens", "cache_read_tokens", "total_tokens"):
        assert sum(getattr(item.metrics, name) for item in summary.hourly_totals) == getattr(
            summary.metrics, name
        )
    assert summary.metrics.cache_hit_rate == 300 / 600
    assert all(item.display_name == "全部模型" for item in summary.hourly_totals)


def test_S03_cache_bust_and_weighted_hit() -> None:
    summary = summarize(
        [row(1, input_count=50_000, cache=0), row(2, input_count=50_000, cache=50_000)], grain="minute"
    )
    assert summary.trend[0].cache_bust
    assert summary.trend[0].cache_hit_rate == 0.5


def test_S04_unknown_cache() -> None:
    metrics = summarize([row(1, cache=None)]).metrics
    assert metrics.cache_hit_rate is None
    assert metrics.cache_coverage_incomplete
    assert metrics.cache_read_tokens == 0


def test_S05_cancelled_null_usage() -> None:
    metrics = summarize([row(1), row(2, input_count=None, cache=None, output=None, incomplete=True)]).metrics
    assert metrics.record_count == 2
    assert metrics.cache_hit_rate == 0.4
    assert not metrics.cache_coverage_incomplete
    assert metrics.incomplete
    assert metrics.missing_usage_count == 1


@pytest.mark.parametrize("harness", [Harness.ZCODE, Harness.CODEX], ids=["S-06-zcode", "S-06-codex"])
def test_S06_grok_is_harness_specific(harness: Harness) -> None:
    summary = summarize(
        [
            row(1, harness=harness, model="grok", cache=0),
            row(2, harness=harness, model="grok", cache=80),
            row(3, harness=harness, model="m", cache=0),
            row(4, harness=harness, model="m", cache=80),
        ]
    )
    by_model = {item.model_key: item.metrics for item in summary.models}
    assert summary.metrics.cache_hit_rate == (160 / 300 if harness == Harness.ZCODE else 0.4)
    assert by_model["grok"].cache_hit_rate == (0.8 if harness == Harness.ZCODE else 0.4)
    assert by_model["m"].cache_hit_rate == 0.4


def test_S07_session_cache_loss() -> None:
    summary = summarize(
        [
            row(1, 1, session="fresh", input_count=1000, cache=0),
            row(2, 5, session="dropped", input_count=1000, cache=0),
            row(3, 10, session="dropped", input_count=70_000, cache=60_000),
            row(4, 20, session="dropped", input_count=70_000, cache=10_000),
            row(5, 12, session="steady", input_count=70_000, cache=60_000),
            row(6, 15, session="steady", input_count=70_000, cache=30_000),
        ],
        end="2026-09-13T00:30:00Z",
        grain="minute",
        offset=0,
    )
    assert [item.label for item in summary.trend if item.cache_loss] == ["09-13 00:20"]


def test_S08_grok_cache_loss_is_unprobed() -> None:
    summary = summarize(
        [
            row(1, harness=Harness.ZCODE, model="grok", input_count=70_000, cache=60_000),
            row(2, 1, harness=Harness.ZCODE, model="grok", input_count=70_000, cache=0),
            row(3, harness=Harness.ZCODE, session="other", input_count=70_000, cache=5000),
        ],
        grain="minute",
    )
    assert not any(item.cache_loss for item in summary.trend)


def test_S09_overview() -> None:
    summary = summarize(row(index, harness=harness) for index, harness in enumerate(Harness, start=1))
    assert (
        summary.metrics.record_count,
        summary.metrics.input_tokens,
        summary.metrics.cache_read_tokens,
    ) == (4, 400, 160)
    assert [item.harness for item in summary.harnesses] == list(Harness)
    assert [item.display_name for item in summary.harnesses] == [
        "Codex",
        "Zcode",
        "Deepseek Harness",
        "Claude Code",
    ]


@pytest.mark.parametrize(
    ("start", "end", "labels", "records"),
    [
        ("2026-09-13T00:00:00Z", "2026-09-13T00:02:00Z", ["09-13 00:00", "09-13 00:01"], 1),
        ("2026-09-12T23:59:00Z", "2026-09-13T00:01:00Z", ["09-12 23:59", "09-13 00:00"], 2),
    ],
)
def test_S10_half_open_midnight(start: str, end: str, labels: list[str], records: int) -> None:
    summary = summarize(
        [
            row(1, 59, hour=23, day=12, input_count=1_000_000),
            row(2, input_count=1_000_000),
            row(3, 2, input_count=1_000_000),
        ],
        start=start,
        end=end,
        grain="minute",
        offset=0,
    )
    assert summary.metrics.record_count == records
    assert [point.label for point in summary.trend] == labels
    assert [point.record_count for point in summary.trend] == ([1, 0] if records == 1 else [1, 1])


def test_S11_range_limit() -> None:
    start = parse_instant("2026-09-01T00:00:00Z")
    assert Window(start, parse_instant("2026-10-01T00:00:00Z"), 480, "day").grain == "day"
    with pytest.raises(DomainError, match="自定义范围不能超过 30 天") as error:
        Window(start, parse_instant("2026-10-01T00:01:00Z"), 480, "day")
    assert error.value.code == "RANGE_TOO_LONG"


@pytest.mark.parametrize(
    ("end", "grain"),
    [("2026-09-04T00:00:00Z", "hour"), ("2026-09-04T00:01:00Z", "hour"), ("2026-10-01T00:00:00Z", "hour")],
)
def test_S12_explicit_hour_remains_available_for_30_days(end: str, grain: str) -> None:
    summary = summarize([], start="2026-09-01T00:00:00Z", end=end, grain="hour")
    assert summary.grain == grain


def test_S13_price_display_name() -> None:
    model = summarize([row(1, model="GPT-6-ASTRA", display="Astra")]).models[0]
    assert (model.model_key, model.display_name) == ("gpt-6-astra", "GPT-6-ASTRA")


def test_S14_price_change_is_already_billed() -> None:
    early = estimate_cost(
        Tokens(input=1_000_000, cache_read=200_000, output=100_000),
        ModelRate("m", "M", "2.5", "0.25", "10"),
        parse_instant("2026-09-13T00:30:00Z"),
    )
    late = estimate_cost(
        Tokens(input=1_000_000, cache_read=0, output=0),
        ModelRate("m", "M", "5", "0.25", "10"),
        parse_instant("2026-09-13T01:00:00Z"),
    )
    summary = summarize(
        [
            row(1, 30, input_count=1_000_000, cache=200_000, output=100_000, pico=early.pico_usd),
            row(2, hour=1, input_count=1_000_000, cache=0, output=0, pico=late.pico_usd),
        ]
    )
    assert summary.metrics.pico_usd == "8050000000000"
