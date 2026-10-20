"""Exact B-series billing vectors and peak boundary examples."""

from dataclasses import FrozenInstanceError

import pytest
from htu.domain.billing import CostEstimate, estimate_cost
from htu.domain.common.time import Instant, parse_instant
from htu.domain.pricing.model import BillingRule, ModelRate, PriceRates
from htu.domain.usage.model import MAX_TOKEN_COUNT, Tokens

AT = parse_instant("2026-09-21T01:30:00Z")
TIER = BillingRule("tier", PriceRates("2", "2", "2"), (1,), (0,), -480, 200)
PEAK = BillingRule("peak", PriceRates("3", "3", "3"), (1, 2, 3, 4, 5), tuple(range(9, 18)), 480, 200)
PEAK_A = BillingRule("peak", PriceRates("3", "0.3", "6"), (1, 2, 3, 4, 5), (9,), 480, 200)
TIER_A = BillingRule("tier", PriceRates("3", "0.3", "6"), (1,), (0,), -480, 200)


@pytest.mark.parametrize(
    ("tokens", "rate", "timestamp", "expected"),
    [
        pytest.param(
            Tokens(input=1_000_000, cache_read=0, output=0),
            ModelRate("m", "M", "1", "0.1", "2"),
            AT,
            CostEstimate(1_000_000_000_000, False, False, "base"),
            id="B-01",
        ),
        pytest.param(
            Tokens(input=1_000, cache_read=200, output=500),
            ModelRate("m", "M", "2.5", "0.25", "10"),
            AT,
            CostEstimate(7_050_000_000, False, False, "base"),
            id="B-02",
        ),
        pytest.param(
            Tokens(input=100, output=10),
            ModelRate("m", "M", "1", "1", "1"),
            AT,
            CostEstimate(10_000_000, True, False, "base"),
            id="B-03",
        ),
        pytest.param(
            Tokens(input=100, cache_read=0, output=0),
            ModelRate("m", "M", "1"),
            AT,
            CostEstimate(100_000_000, False, False, "base"),
            id="B-04",
        ),
        pytest.param(
            Tokens(input=100, cache_read=50, output=0),
            ModelRate("m", "M", "1", None, "1"),
            AT,
            CostEstimate(50_000_000, False, True, "base"),
            id="B-05",
        ),
        pytest.param(
            Tokens(input=100, cache_read=0, output=5),
            None,
            AT,
            CostEstimate(0, False, True, "none"),
            id="B-06",
        ),
        pytest.param(
            Tokens(input=200_001, cache_read=0, output=0),
            ModelRate("m", "M", "1", "1", "1", TIER),
            AT,
            CostEstimate(400_002_000_000, False, False, "tier"),
            id="B-07",
        ),
        pytest.param(
            Tokens(input=200_000, cache_read=0, output=0),
            ModelRate("m", "M", "1", "1", "1", TIER),
            AT,
            CostEstimate(200_000_000_000, False, False, "base"),
            id="B-08",
        ),
        pytest.param(
            Tokens(output=10),
            ModelRate("m", "M", "1", "1", "1", TIER),
            AT,
            CostEstimate(0, True, False, "base"),
            id="B-09",
        ),
        pytest.param(
            Tokens(input=10, cache_read=0, output=0),
            ModelRate("m", "M", "1", "1", "1", PEAK),
            AT,
            CostEstimate(30_000_000, False, False, "peak"),
            id="B-10",
        ),
        pytest.param(
            Tokens(input=10, cache_read=0, output=0),
            ModelRate("m", "M", "1", "1", "1", PEAK),
            parse_instant("2026-09-21T00:59:59.999Z"),
            CostEstimate(10_000_000, False, False, "base"),
            id="B-11",
        ),
        pytest.param(
            Tokens(input=10, cache_read=0, output=0),
            ModelRate("m", "M", "1", "1", "1", BillingRule("peak", PriceRates(), (1,), (9,), 480, 200)),
            AT,
            CostEstimate(0, False, True, "peak"),
            id="B-12",
        ),
        pytest.param(
            Tokens(input=MAX_TOKEN_COUNT, cache_read=0, output=0),
            ModelRate("m", "M", "999999999.999999", "0", "0"),
            AT,
            CostEstimate(MAX_TOKEN_COUNT * 999_999_999_999_999, False, False, "base"),
            id="B-13",
        ),
        pytest.param(
            Tokens(input=1_000_000, cache_read=200_000, output=100_000),
            ModelRate("m", "M", "2.5", "0.25", "10"),
            AT,
            CostEstimate(3_050_000_000_000, False, False, "base"),
            id="B-14",
        ),
        pytest.param(
            Tokens(input=1_000_000, cache_read=200_000, output=100_000),
            ModelRate("m", "M", "1", "0.1", "2", PEAK_A),
            parse_instant("2026-09-14T01:00:00Z"),
            CostEstimate(3_060_000_000_000, False, False, "peak"),
            id="B-15",
        ),
        pytest.param(
            Tokens(input=200_001, cache_read=200_000, output=0),
            ModelRate("m", "M", "1", "0.1", "2", TIER_A),
            AT,
            CostEstimate(60_003_000_000, False, False, "tier"),
            id="B-16",
        ),
        pytest.param(
            Tokens(input=1, cache_read=0, output=0),
            ModelRate("m", "M", "0.000001", "0", "0"),
            AT,
            CostEstimate(1, False, False, "base"),
            id="B-17",
        ),
    ],
)
def test_billing_golden_vectors(
    tokens: Tokens, rate: ModelRate | None, timestamp: Instant, expected: CostEstimate
) -> None:
    assert estimate_cost(tokens, rate, timestamp) == expected


@pytest.mark.parametrize(
    "timestamp",
    ["2026-09-14T00:59:59.999Z", "2026-09-14T02:00:00Z", "2026-09-13T01:00:00Z"],
)
def test_B15_peak_misses(timestamp: str) -> None:
    tokens = Tokens(input=1_000_000, cache_read=200_000, output=100_000)
    rate = ModelRate("m", "M", "1", "0.1", "2", PEAK_A)
    assert estimate_cost(tokens, rate, parse_instant(timestamp)) == CostEstimate(
        1_020_000_000_000, False, False, "base"
    )


def test_B10_reference_date_is_monday() -> None:
    assert AT.value.isoweekday() == 1


def test_estimate_is_immutable_and_does_not_change_inputs() -> None:
    tokens = Tokens(input=100, cache_read=40, output=10)
    rate = ModelRate("m", "M", "1", "0.1", "2", PEAK_A)
    result = estimate_cost(tokens, rate, AT)
    assert estimate_cost(tokens, rate, AT) == result
    assert tokens == Tokens(input=100, cache_read=40, output=10)
    assert rate == ModelRate("m", "M", "1", "0.1", "2", PEAK_A)
    with pytest.raises(FrozenInstanceError):
        result.pico_usd = 0  # pyright: ignore[reportAttributeAccessIssue] - test frozen runtime guard
