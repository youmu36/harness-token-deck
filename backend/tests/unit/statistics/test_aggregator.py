import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from typing import cast

import pytest
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, parse_instant
from htu.domain.statistics import Aggregator, LedgerRow, monitor_window
from htu.domain.usage.model import MAX_TOKEN_COUNT, Harness

from .helpers import row, summarize

START = parse_instant("2026-09-13T00:00:00Z")
END = parse_instant("2026-09-13T02:00:00Z")


def test_empty_results_and_outside_only_rows_have_no_points() -> None:
    empty = summarize([])
    outside = summarize([row(1, day=12), row(2, hour=2)])
    assert empty == outside
    assert empty.metrics.record_count == empty.metrics.total_tokens == 0
    assert empty.metrics.cache_hit_rate is None
    assert empty.metrics.pico_usd == "0"
    assert (empty.trend, empty.models, empty.hourly, empty.hourly_totals, empty.harnesses) == ((),) * 5


def test_zero_usage_record_still_triggers_padding() -> None:
    result = summarize([row(1, input_count=0, cache=0, output=0, pico=0)])
    assert len(result.trend) == 2
    assert result.trend[0].record_count == 1
    assert result.trend[1].record_count == 0
    assert result.trend[1].cache_hit_rate is None
    assert result.trend[1].pico_usd == "0"
    assert not result.trend[1].cache_bust and not result.trend[1].cache_loss
    assert len(result.hourly_totals) == len(result.hourly) == 1


def test_single_harness_filters_and_does_not_export_overview_breakdown() -> None:
    result = summarize([row(i, harness=h) for i, h in enumerate(Harness)], harness=Harness.ZCODE)
    assert result.metrics.record_count == 1
    assert not result.harnesses
    with pytest.raises(DomainError) as error:
        Aggregator(0, None, START, END, harness="unknown")
    assert error.value.code == "UNKNOWN_HARNESS"


def test_overview_includes_claude_with_its_name_and_cost() -> None:
    result = summarize([row(1, harness=Harness.CLAUDE, pico=123), row(2, harness=Harness.CODEX, pico=456)])
    assert result.metrics.pico_usd == "579"
    assert {item.harness: item.display_name for item in result.harnesses} == {
        Harness.CLAUDE: "Claude Code",
        Harness.CODEX: "Codex",
    }


def test_latest_display_is_timestamp_based_across_sessions_and_harnesses() -> None:
    result = summarize(
        [
            row(1, 59, session="a", display="Newest"),
            row(2, 1, session="z", display="Pushed later"),
            row(3, 10, harness=Harness.ZCODE, display="Last Harness"),
        ]
    )
    assert result.models[0].display_name == result.hourly[0].display_name == "a"


def test_name_tie_breaking_and_hourly_scope() -> None:
    result = summarize(
        [
            row(1, session="a", display="First"),
            row(2, session="b", display="Tie winner"),
            row(3, hour=1, session="a", display="Later hour"),
        ]
    )
    assert result.models[0].display_name == "a"
    assert [item.display_name for item in result.hourly] == ["a", "a"]


@pytest.mark.parametrize("display", [None, "", "   "])
def test_newest_name_falls_back_to_raw_model_id(display: str | None) -> None:
    result = summarize([row(1, display="Old"), row(2, 1, model=" A ", display=display)])
    assert result.models[0].model_key == "a"
    assert result.models[0].display_name == " A "


def test_unknown_model_and_canonical_groups() -> None:
    result = summarize(
        [row(1, model="G P T-6"), row(2, model="gpt-6"), row(3, model=None, display="Ignored")]
    )
    by_key = {item.model_key: item for item in result.models}
    assert len(by_key) == 2
    assert by_key["gpt-6"].metrics.record_count == 2
    assert by_key[""].display_name == "模型未知"
    assert result.metrics.unknown_model_count == 1


def test_model_sort_uses_exact_integer_cost_then_display_name() -> None:
    large = 9_007_199_254_740_992
    result = summarize(
        [
            row(1, model="z", display="A", pico=large),
            row(2, model="a", display="Z", pico=large + 1),
            row(3, model="b", display="B", pico=large),
        ]
    )
    assert [item.model_key for item in result.models] == ["a", "b", "z"]


def test_hourly_order_is_bucket_then_key_independent_of_model_cost_order() -> None:
    result = summarize([row(1, model="z", pico=999), row(2, model="a", pico=1), row(3, hour=1, model=None)])
    assert [item.model_key for item in result.hourly] == ["a", "z", ""]


@pytest.mark.parametrize(
    "size,cache,bust,loss",
    [
        (49_999, 0, False, False),
        (50_000, 0, True, False),
        (50_000, 5_000, False, False),
        (100_000, 10_000, False, True),
        (100_000, 9_999, True, False),
    ],
)
def test_request_thresholds_and_exclusive_warnings(size: int, cache: int, bust: bool, loss: bool) -> None:
    result = summarize([row(1, input_count=size, cache=cache)], grain="minute")
    assert result.trend[0].cache_bust == bust
    assert result.trend[0].cache_loss == loss


def test_small_requests_never_sum_into_a_false_warning() -> None:
    result = summarize([row(i, input_count=10_000, cache=0) for i in range(10)])
    assert not result.trend[0].cache_bust and not result.trend[0].cache_loss


def test_low_hit_request_is_not_hidden_by_later_high_hit_in_same_minute() -> None:
    result = summarize([row(1, input_count=50_000, cache=0), row(2, input_count=500_000, cache=490_000)])
    assert result.trend[0].cache_bust and not result.trend[0].cache_loss


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.ZCODE])
def test_unknown_cache_and_grok_never_contribute_to_miss_threshold(harness: Harness) -> None:
    result = summarize(
        [
            row(1, harness=harness, input_count=99_999, cache=None),
            row(2, harness=harness, model="GROK", input_count=99_999, cache=0),
        ],
    )
    assert result.trend[0].cache_bust == (harness == Harness.CODEX)
    assert result.metrics.cache_coverage_incomplete


def test_hourly_bust_takes_priority_over_large_miss_in_another_minute() -> None:
    result = summarize([row(1, input_count=70_000, cache=20_000), row(2, 1, input_count=70_000, cache=0)])
    assert result.trend[0].cache_bust and not result.trend[0].cache_loss


def test_null_and_unprobed_requests_do_not_reset_previous_eligible_cache() -> None:
    result = summarize(
        [
            row(1, harness=Harness.ZCODE, input_count=70_000, cache=60_000),
            row(2, 1, harness=Harness.ZCODE, cache=None),
            row(3, 2, harness=Harness.ZCODE, model="grok", input_count=70_000, cache=1000),
            row(4, 3, harness=Harness.ZCODE, input_count=70_000, cache=20_000),
        ],
        grain="minute",
    )
    assert [point.label for point in result.trend if point.cache_loss] == ["09-13 08:03"]


@pytest.mark.parametrize("same_session", [True, False])
def test_loss_cannot_cross_harness_or_null_session(same_session: bool) -> None:
    session = "s" if same_session else None
    result = summarize(
        [
            row(1, session=session, input_count=70_000, cache=60_000),
            row(2, harness=Harness.ZCODE, session=session, input_count=70_000, cache=0),
        ]
    )
    assert result.trend[0].cache_loss == (not same_session)
    if not same_session:
        result = summarize(
            [row(1, session=None, input_count=70_000, cache=60_000), row(2, 1, session=None, cache=0)]
        )
        assert not any(point.cache_loss for point in result.trend)


def test_pre_range_request_is_not_a_cache_loss_baseline() -> None:
    result = summarize([row(1, 59, hour=23, day=12, input_count=70_000, cache=60_000), row(2, cache=0)])
    assert not result.trend[0].cache_loss


@pytest.mark.parametrize("bad", [row(1), row(2, session="a"), row(2, day=13, minute=0)])
def test_duplicate_or_out_of_order_row_is_rejected_without_changing_snapshot(bad: LedgerRow) -> None:
    aggregator = Aggregator(0, "hour", START, END)
    aggregator.push(row(1, 1))
    before = aggregator.finish()
    with pytest.raises(DomainError) as error:
        aggregator.push(bad)
    assert error.value.code == "STATISTICS_INVALID"
    assert aggregator.finish() == before


def test_same_time_is_ordered_by_dedupe_key_and_exact_duplicate_rejected() -> None:
    aggregator = Aggregator(0, "hour", START, END)
    first, second = row(1), row(2)
    aggregator.push(first)
    aggregator.push(second)
    with pytest.raises(DomainError):
        aggregator.push(second)
    assert aggregator.finish().metrics.record_count == 2


@pytest.mark.parametrize(
    ("input_count", "cache", "output"),
    [(MAX_TOKEN_COUNT, 0, 0), (0, 0, MAX_TOKEN_COUNT), (MAX_TOKEN_COUNT, MAX_TOKEN_COUNT, 0)],
)
def test_count_overflow_keeps_prior_snapshot(input_count: int, cache: int, output: int) -> None:
    aggregator = Aggregator(0, "hour", START, END)
    aggregator.push(row(1, input_count=input_count, cache=cache, output=output))
    before = aggregator.finish()
    with pytest.raises(DomainError) as error:
        aggregator.push(row(2, input_count=1, cache=1, output=0))
    assert error.value.code == "COUNT_OVERFLOW"
    assert aggregator.finish() == before


def test_total_token_overflow_when_each_component_is_still_safe() -> None:
    with pytest.raises(DomainError) as error:
        summarize([row(1, input_count=MAX_TOKEN_COUNT, cache=0, output=1)])
    assert error.value.code == "COUNT_OVERFLOW"


def test_aggregate_amount_has_arbitrary_precision_and_json_shape() -> None:
    pico = 2**127 - 1
    summary = summarize([row(1, pico=pico), row(2, pico=pico)])
    exported = json.loads(json.dumps(summary.to_dict()))
    assert exported["metrics"]["picoUsd"] == str(2 * pico)
    assert exported["trend"][0]["picoUsd"] == str(2 * pico)
    assert exported["models"][0]["picoUsd"] == str(2 * pico)
    assert exported["hourly"][0]["metrics"]["picoUsd"] == str(2 * pico)
    assert "modelKey" not in exported["hourlyTotals"][0]
    assert exported["harnesses"][0]["picoUsd"] == str(2 * pico)
    assert type(exported["metrics"]["inputTokens"]) is int
    assert type(exported["metrics"]["cacheHitRate"]) is float
    assert exported["grain"] == "hour" and exported["offsetMinutes"] == 480


def test_partial_usage_and_quality_flags_are_counted_independently() -> None:
    metrics = summarize(
        [
            row(1, input_count=None, cache=40, incomplete=True),
            row(2, output=None, unpriced=True),
            row(3, input_count=None, cache=None, output=None),
        ]
    ).metrics
    assert metrics.missing_usage_count == 3
    assert metrics.cache_read_tokens == 80
    assert not metrics.cache_coverage_incomplete
    assert metrics.incomplete and metrics.unpriced


def test_finish_is_repeatable_and_previous_snapshot_stays_immutable() -> None:
    aggregator = Aggregator(0, "hour", START, END)
    aggregator.push(row(1))
    first = aggregator.finish()
    assert first == aggregator.finish()
    exported = first.to_dict()
    cast(dict[str, object], exported["metrics"])["picoUsd"] = "changed"
    assert first.metrics.pico_usd == "84000000"
    with pytest.raises(FrozenInstanceError):
        first.metrics.record_count = 9  # pyright: ignore[reportAttributeAccessIssue]
    aggregator.push(row(2))
    assert first.metrics.record_count == 1
    assert aggregator.finish().metrics.record_count == 2


def test_cache_monitor_uses_same_aggregator_with_current_minute_excluded() -> None:
    now = parse_instant("2026-09-13T01:00:59Z")
    window = monitor_window(now, "minute", 480)
    aggregator = Aggregator(480, window.actual_grain, window.start, window.end, harness=Harness.CODEX)
    aggregator.push(row(1, 59))
    aggregator.push(row(2, hour=1))
    result = aggregator.finish()
    assert len(result.trend) == 60 and result.metrics.record_count == 1
    assert result.trend[-1].label == "09-13 08:59"


def test_non_aligned_window_pads_partial_first_and_last_buckets() -> None:
    first = replace(row(1), occurred_at=Instant(START.value + timedelta(seconds=30)))
    result = summarize(
        [first], start="2026-09-13T00:00:30Z", end="2026-09-13T00:02:15Z", grain="minute", offset=0
    )
    assert [point.label for point in result.trend] == ["09-13 00:00", "09-13 00:01", "09-13 00:02"]
    assert [point.record_count for point in result.trend] == [1, 0, 0]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"input": True}, "INVALID_TOKEN_COUNT"),
        ({"input": -1}, "INVALID_TOKEN_COUNT"),
        ({"cache_read": 101}, "CACHE_EXCEEDS_INPUT"),
        ({"output": MAX_TOKEN_COUNT + 1}, "INVALID_TOKEN_COUNT"),
        ({"pico_usd": True}, "PICO_INVALID"),
        ({"pico_usd": -1}, "PICO_INVALID"),
        ({"pico_usd": 2**127}, "COST_OVERFLOW"),
        ({"incomplete": 1}, "STATISTICS_INVALID"),
        ({"unpriced": None}, "STATISTICS_INVALID"),
        ({"dedupe_key": ""}, "STATISTICS_INVALID"),
        ({"session_id": ""}, "STATISTICS_INVALID"),
        ({"model_id": ""}, "STATISTICS_INVALID"),
        ({"display_name": 4}, "STATISTICS_INVALID"),
        ({"occurred_at": "now"}, "STATISTICS_INVALID"),
        ({"harness": "unknown"}, "UNKNOWN_HARNESS"),
    ],
)
def test_ledger_projection_is_validated(changes: dict[str, object], code: str) -> None:
    with pytest.raises(DomainError) as error:
        replace(row(1), **changes)
    assert error.value.code == code


def test_push_requires_a_ledger_projection() -> None:
    with pytest.raises(DomainError) as error:
        Aggregator(0, None, START, END).push(cast(LedgerRow, object()))
    assert error.value.code == "STATISTICS_INVALID"
