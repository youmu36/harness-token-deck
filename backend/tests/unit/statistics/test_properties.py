"""Conservation across grouping/grains and streaming over a large synthetic ledger."""

from datetime import timedelta

import pytest
from htu.domain.common.time import Instant, parse_instant
from htu.domain.statistics import Aggregator, LedgerRow, Metrics
from htu.domain.usage.model import Harness
from hypothesis import given, settings
from hypothesis import strategies as st

from .helpers import row, summarize


@st.composite
def ledger_rows(draw: st.DrawFn) -> list[LedgerRow]:
    records: list[LedgerRow] = []
    for index in range(draw(st.integers(0, 50))):
        input_count = draw(st.one_of(st.none(), st.integers(0, 200_000)))
        cache = draw(
            st.one_of(st.none(), st.integers(0, input_count if input_count is not None else 200_000))
        )
        records.append(
            row(
                index,
                minute=index % 60,
                hour=index // 60,
                harness=draw(st.sampled_from(list(Harness))),
                session=draw(st.sampled_from([None, "a", "b"])),
                model=draw(st.sampled_from([None, "M", "grok"])),
                input_count=input_count,
                cache=cache,
                output=draw(st.one_of(st.none(), st.integers(0, 200_000))),
                pico=draw(st.integers(0, 2**100)),
                incomplete=draw(st.booleans()),
                unpriced=draw(st.booleans()),
            )
        )
    return records


@given(ledger_rows())
@settings(max_examples=70, deadline=None)
def test_all_groupings_conserve_counts_exact_costs_and_quality(records: list[LedgerRow]) -> None:
    summary = summarize(records)
    metric_groups: list[list[Metrics]] = [
        [item.metrics for item in summary.models],
        [item.metrics for item in summary.hourly],
        [item.metrics for item in summary.hourly_totals],
        [item.metrics for item in summary.harnesses],
    ]
    for group in metric_groups:
        for name in (
            "record_count",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "new_input",
            "total_tokens",
            "unknown_model_count",
            "missing_usage_count",
        ):
            assert sum(getattr(item, name) for item in group) == getattr(summary.metrics, name)
        assert sum(int(item.pico_usd) for item in group) == int(summary.metrics.pico_usd)
        assert any(item.incomplete for item in group) == summary.metrics.incomplete
        assert any(item.unpriced for item in group) == summary.metrics.unpriced
        assert (
            any(item.cache_coverage_incomplete for item in group) == summary.metrics.cache_coverage_incomplete
        )
    assert summary.metrics.record_count == len(records)
    assert int(summary.metrics.pico_usd) == sum(record.pico_usd for record in records)
    for grain in ("minute", "hour", "day"):
        result = summarize(records, grain=grain)
        assert result.metrics == summary.metrics
        assert sum(item.record_count for item in result.trend) == len(records)
        assert sum(int(item.pico_usd) for item in result.trend) == int(summary.metrics.pico_usd)
    rated = [
        item
        for item in records
        if item.input is not None
        and item.cache_read is not None
        and (item.cache_read > 0 or not item.unprobed)
    ]
    denominator = sum(item.input or 0 for item in rated)
    expected_hit = sum(item.cache_read or 0 for item in rated) / denominator if denominator else None
    assert summary.metrics.cache_hit_rate == expected_hit


@pytest.mark.slow
def test_large_stream_can_finish_without_materializing_ledger_rows() -> None:
    start = parse_instant("2026-09-13T00:00:00Z")
    aggregator = Aggregator(480, "minute", start, Instant(start.value + timedelta(hours=24)))
    count = 50_000
    pico = 9_007_199_254_740_993
    for index in range(count):
        aggregator.push(
            LedgerRow(
                Harness.CODEX,
                f"{index:064x}",
                "s",
                Instant(start.value + timedelta(seconds=index)),
                "MODEL",
                100,
                40,
                10,
                pico,
                False,
                False,
                "Model",
            )
        )
    summary = aggregator.finish()
    assert summary.metrics.record_count == count
    assert summary.metrics.input_tokens == 100 * count
    assert summary.metrics.output_tokens == 10 * count
    assert summary.metrics.cache_hit_rate == 0.4
    assert summary.metrics.pico_usd == str(pico * count)
    assert len(summary.trend) == 1440
    assert sum(point.record_count for point in summary.trend) == count
    assert sum(int(item.metrics.pico_usd) for item in summary.hourly_totals) == pico * count
