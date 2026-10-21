"""Billing quality flags, whole-rule replacement, and time/amount boundaries."""

from dataclasses import replace
from typing import Literal, cast

import pytest
from htd.domain.billing import CostEstimate, RuleApplied, estimate_cost
from htd.domain.common.errors import COST_OVERFLOW, PICO_INVALID, DomainError
from htd.domain.common.money import MAX_SCALED_RATE
from htd.domain.common.time import parse_instant
from htd.domain.pricing.model import BillingRule, ModelRate, PriceRates
from htd.domain.usage.model import MAX_TOKEN_COUNT, Tokens

AT = parse_instant("2026-09-21T01:30:00Z")


@pytest.mark.parametrize(
    ("tokens", "incomplete", "needed"),
    [
        (Tokens(input=0, cache_read=0, output=0), False, ()),
        (Tokens(input=10, cache_read=0, output=0), False, ("input",)),
        (Tokens(input=10, cache_read=10, output=0), False, ("cache",)),
        (Tokens(input=10, cache_read=4, output=0), False, ("input", "cache")),
        (Tokens(input=0, cache_read=0, output=7), False, ("output",)),
        (Tokens(input=10, cache_read=4, output=7), False, ("input", "cache", "output")),
        (Tokens(cache_read=0, output=0), True, ("input",)),
        (Tokens(output=0), True, ("input", "cache")),
        (Tokens(input=0, output=0), True, ("input", "cache")),
        (Tokens(input=10, output=0), True, ("input", "cache")),
        (Tokens(cache_read=4, output=0), True, ("input", "cache")),
        (Tokens(input=10, cache_read=10), True, ("cache", "output")),
        (Tokens(input=10, cache_read=0), True, ("input", "output")),
        (Tokens(input=0, cache_read=0), True, ("output",)),
        (Tokens(), True, ("input", "cache", "output")),
        (Tokens(input=10, cache_read=4), True, ("input", "cache", "output")),
        (Tokens(input=10, output=7), True, ("input", "cache", "output")),
        (Tokens(cache_read=0, output=7), True, ("input", "output")),
    ],
)
@pytest.mark.parametrize("missing", [(), ("input",), ("cache",), ("output",), ("input", "cache", "output")])
def test_only_required_missing_prices_mark_unpriced(
    tokens: Tokens, incomplete: bool, needed: tuple[str, ...], missing: tuple[str, ...]
) -> None:
    rate = replace(ModelRate("m", "M", "1", "1", "1"), **dict.fromkeys(missing))
    result = estimate_cost(tokens, rate, AT)
    assert result.incomplete is incomplete
    assert result.unpriced is bool(set(needed) & set(missing))
    assert result.rule_applied == "base"
    assert estimate_cost(tokens, None, AT) == CostEstimate(0, incomplete, True, "none")
    zero_rate = ModelRate("m", "M", "0", "0", "0")
    assert estimate_cost(tokens, zero_rate, AT) == CostEstimate(0, incomplete, False, "base")


@pytest.mark.parametrize("mode", ["peak", "tier"])
@pytest.mark.parametrize(
    ("rule_rates", "pico", "unpriced"),
    [
        (PriceRates(), 0, True),
        (PriceRates(None, "0.2", None), 208_000_000, True),
        (PriceRates("2", None, "4"), 160_000_000, True),
        (PriceRates("2", "0.2", None), 328_000_000, True),
        (PriceRates("0", "0", "0"), 0, False),
    ],
)
def test_rule_prices_replace_all_base_prices(
    mode: Literal["peak", "tier"], rule_rates: PriceRates, pico: int, unpriced: bool
) -> None:
    rule = BillingRule(mode, rule_rates, (1,), (9,), 480, 1)
    rate = ModelRate("m", "M", "7", "7", "7", rule)
    tokens = Tokens(input=1_100, cache_read=1_040, output=10)
    result = estimate_cost(tokens, rate, AT)
    assert result == CostEstimate(pico, False, unpriced, mode)


@pytest.mark.parametrize("cache_read", [None, 0, 40])
@pytest.mark.parametrize("output", [None, 0, 10])
@pytest.mark.parametrize("base_missing", [False, True])
def test_tier_unknown_input_suppresses_all_costs_and_uses_base_quality_flags(
    cache_read: int | None, output: int | None, base_missing: bool
) -> None:
    rule = BillingRule("tier", PriceRates("5", "5", "5"), (1,), (9,), 480, 1)
    base = None if base_missing else "1"
    rate = ModelRate("m", "M", base, base, base, rule)
    result = estimate_cost(Tokens(cache_read=cache_read, output=output), rate, AT)
    assert result == CostEstimate(0, True, base_missing, "base")


@pytest.mark.parametrize("threshold_k", [1, 1_000_000])
@pytest.mark.parametrize(("difference", "applied"), [(-1, "base"), (0, "base"), (1, "tier")])
def test_tier_strict_threshold_includes_cache_and_ignores_peak_fields(
    threshold_k: int, difference: int, applied: str
) -> None:
    count = threshold_k * 1_000 + difference
    rule = BillingRule("tier", PriceRates("5", "3", "7"), (7,), (23,), -840, threshold_k)
    rate = ModelRate("m", "M", "1", "2", "4", rule)
    result = estimate_cost(Tokens(input=count, cache_read=count, output=1), rate, AT)
    expected = count * (3_000_000 if applied == "tier" else 2_000_000)
    expected += 7_000_000 if applied == "tier" else 4_000_000
    assert result == CostEstimate(expected, False, False, cast(RuleApplied, applied))


def test_tier_prices_may_be_lower_than_base() -> None:
    rule = BillingRule("tier", PriceRates("0", "0", "0"), (1,), (9,), 480, 1)
    rate = ModelRate("m", "M", "1", "1", "1", rule)
    assert estimate_cost(Tokens(input=1_000, cache_read=0, output=0), rate, AT).pico_usd == 1_000_000_000
    assert estimate_cost(Tokens(input=1_001, cache_read=0, output=0), rate, AT).pico_usd == 0


@pytest.mark.parametrize(
    ("timestamp", "offset", "weekday", "hour", "applied"),
    [
        ("2026-09-21T01:00:00Z", 480, 1, 9, "peak"),
        ("2026-09-21T01:59:59.999Z", 480, 1, 9, "peak"),
        ("2026-09-21T02:00:00Z", 480, 1, 9, "base"),
        ("2026-09-21T09:30:00+08:00", 480, 1, 9, "peak"),
        ("2026-09-20T16:00:00Z", 480, 1, 0, "peak"),
        ("2026-09-20T15:59:59.999Z", 480, 1, 0, "base"),
        ("2026-09-21T07:59:59.999Z", -480, 7, 23, "peak"),
        ("2026-09-21T08:00:00Z", -480, 7, 23, "base"),
        ("2026-09-21T00:00:00Z", 0, 1, 0, "peak"),
        ("2026-09-21T03:30:00Z", 330, 1, 9, "peak"),
        ("2026-09-21T03:29:59.999Z", 330, 1, 9, "base"),
        ("2026-09-21T09:29:59.999Z", -30, 1, 9, "base"),
        ("2026-09-21T09:30:00Z", -30, 1, 9, "peak"),
        ("2026-09-20T10:00:00Z", 840, 1, 0, "peak"),
        ("2026-09-21T13:59:59.999Z", -840, 7, 23, "peak"),
        ("2026-09-21T14:00:00Z", -840, 7, 23, "base"),
        ("0001-01-01T00:00:00Z", -840, 7, 10, "peak"),
        ("9999-12-31T23:59:59.999Z", 840, 6, 13, "peak"),
        ("2026-01-05T01:00:00Z", 480, 1, 9, "peak"),
        ("2026-07-06T01:00:00Z", 480, 1, 9, "peak"),
    ],
)
def test_peak_fixed_offset_and_local_weekday(
    timestamp: str, offset: int, weekday: int, hour: int, applied: str
) -> None:
    rule = BillingRule("peak", PriceRates("3", "3", "3"), (weekday,), (hour,), offset, 1)
    rate = ModelRate("m", "M", "1", "1", "1", rule)
    tokens = Tokens(input=10, cache_read=0, output=0)
    result = estimate_cost(tokens, rate, parse_instant(timestamp))
    assert result == CostEstimate(
        30_000_000 if applied == "peak" else 10_000_000, False, False, cast(RuleApplied, applied)
    )


@pytest.mark.parametrize("cache_write", [None, 0, 60])
@pytest.mark.parametrize("reasoning", [None, 0, 10])
def test_cache_write_defaults_to_zero_and_reasoning_stays_in_output(
    cache_write: int | None, reasoning: int | None
) -> None:
    tokens = Tokens(input=100, cache_read=40, cache_write=cache_write, output=10, reasoning=reasoning)
    rate = ModelRate("m", "M", "1", "0.1", "2")
    assert estimate_cost(tokens, rate, AT) == CostEstimate(
        84_000_000 - (cache_write or 0) * 1_000_000, False, False, "base"
    )


def test_unknown_input_or_cache_only_bills_known_output() -> None:
    rate = ModelRate("m", "M", "5", "2", "3")
    for tokens in (Tokens(cache_read=40, output=10), Tokens(input=100, output=10)):
        assert estimate_cost(tokens, rate, AT) == CostEstimate(30_000_000, True, False, "base")
    assert estimate_cost(Tokens(input=100, cache_read=40), rate, AT) == CostEstimate(
        380_000_000, True, False, "base"
    )


@pytest.mark.parametrize("cache_read", [0, MAX_TOKEN_COUNT // 2, MAX_TOKEN_COUNT])
def test_maximum_valid_tokens_and_prices_remain_exact(cache_read: int) -> None:
    rate = ModelRate("m", "M", "999999999.999999", "999999999.999999", "999999999.999999")
    tokens = Tokens(input=MAX_TOKEN_COUNT, cache_read=cache_read, output=MAX_TOKEN_COUNT)
    result = estimate_cost(tokens, rate, AT)
    assert result == CostEstimate(2 * MAX_TOKEN_COUNT * MAX_SCALED_RATE, False, False, "base")


def test_pico_above_safe_integer_range_keeps_single_pico_increment() -> None:
    tokens = Tokens(input=MAX_TOKEN_COUNT, cache_read=0, output=2)
    rate = ModelRate("m", "M", "0.000001", "0", "0.000001")
    assert estimate_cost(tokens, rate, AT).pico_usd == 9_007_199_254_740_993


@pytest.mark.parametrize("pico", [0, 1, 2**127 - 1])
def test_cost_result_in_range(pico: int) -> None:
    assert CostEstimate(pico, False, False, "base").pico_usd == pico


@pytest.mark.parametrize("pico", [2**127, 2**127 + 1, 2**128])
def test_cost_overflow_is_structured(pico: int) -> None:
    with pytest.raises(DomainError) as failure:
        CostEstimate(pico, False, False, "base")
    assert failure.value.code == COST_OVERFLOW
    assert failure.value.field == "pico_usd"


@pytest.mark.parametrize("pico", [-1, True, 1.0, "1", None])
def test_cost_result_rejects_invalid_pico(pico: object) -> None:
    with pytest.raises(DomainError) as failure:
        CostEstimate(cast(int, pico), False, False, "base")
    assert failure.value.code == PICO_INVALID
