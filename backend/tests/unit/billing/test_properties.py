"""Billing properties over valid token/rate domains with an independent Decimal oracle."""

from datetime import timedelta
from decimal import Decimal, localcontext

from htu.domain.billing import estimate_cost
from htu.domain.common.money import MAX_SCALED_RATE, Rate
from htu.domain.common.time import from_epoch_ms, parse_instant
from htu.domain.pricing.model import BillingRule, ModelRate, PriceRates
from htu.domain.usage.model import MAX_TOKEN_COUNT, Tokens
from hypothesis import given
from hypothesis import strategies as st

AT = parse_instant("2026-09-21T01:30:00Z")
COUNTS = st.integers(min_value=0, max_value=MAX_TOKEN_COUNT)
SCALED_RATES = st.integers(min_value=0, max_value=MAX_SCALED_RATE)
RATES = st.one_of(st.none(), SCALED_RATES.map(lambda value: Rate(value).normalized))
PRICE_RATES = st.builds(PriceRates, RATES, RATES, RATES)


@st.composite
def token_values(draw: st.DrawFn) -> Tokens:
    input_count = draw(st.one_of(st.none(), COUNTS))
    upper = MAX_TOKEN_COUNT if input_count is None else input_count
    cache = draw(st.one_of(st.none(), st.integers(min_value=0, max_value=upper)))
    return Tokens(input=input_count, cache_read=cache, output=draw(st.one_of(st.none(), COUNTS)))


@st.composite
def model_rates(draw: st.DrawFn) -> ModelRate | None:
    if draw(st.booleans()):
        return None
    base = draw(PRICE_RATES)
    mode = draw(st.sampled_from(("base", "peak", "tier")))
    rule = None
    if mode != "base":
        rule = BillingRule(
            mode,
            draw(PRICE_RATES),
            tuple(draw(st.lists(st.integers(1, 7), min_size=1, max_size=7))),
            tuple(draw(st.lists(st.integers(0, 23), min_size=1, max_size=24))),
            draw(st.integers(-840, 840)),
            draw(st.integers(1, 1_000_000)),
        )
    return ModelRate("m", "M", base.input, base.cache, base.output, rule)


@given(token_values(), model_rates(), st.integers(1_700_000_000_000, 1_900_000_000_000))
def test_cost_is_int_and_matches_independent_decimal_oracle(
    tokens: Tokens, rate: ModelRate | None, epoch_ms: int
) -> None:
    timestamp = from_epoch_ms(epoch_ms)
    result = estimate_cost(tokens, rate, timestamp)
    assert type(result.pico_usd) is int
    assert type(result.incomplete) is bool
    assert type(result.unpriced) is bool
    assert 0 <= result.pico_usd < 2**127
    if rate is None or (
        rate.billing_rule is not None and rate.billing_rule.mode == "tier" and tokens.input is None
    ):
        assert result.pico_usd == 0
        return
    prices: PriceRates | ModelRate = rate
    rule = rate.billing_rule
    if rule is not None:
        if rule.mode == "tier":
            if tokens.input is not None and tokens.input > rule.threshold_k * 1_000:
                prices = rule.rates
        else:
            local_time = timestamp.value + timedelta(minutes=rule.offset_minutes)
            if local_time.isoweekday() in rule.weekdays and local_time.hour in rule.hours:
                prices = rule.rates
    with localcontext() as context:
        context.prec = 80
        expected_usd = Decimal(0)
        if tokens.input is not None and tokens.cache_read is not None:
            expected_usd += Decimal(tokens.input - tokens.cache_read) * Decimal(prices.input or "0") / 10**6
            expected_usd += Decimal(tokens.cache_read) * Decimal(prices.cache or "0") / 10**6
        expected_usd += Decimal(tokens.output or 0) * Decimal(prices.output or "0") / 10**6
        assert Decimal(result.pico_usd) == expected_usd * 10**12


@given(COUNTS, COUNTS, COUNTS, COUNTS, COUNTS, PRICE_RATES)
def test_fixed_prices_are_monotone_in_ordinary_input_and_output(
    input_a: int, input_b: int, cache: int, output_a: int, output_b: int, prices: PriceRates
) -> None:
    lower_input, upper_input = sorted((input_a, input_b))
    lower_output, upper_output = sorted((output_a, output_b))
    cache = min(cache, lower_input)
    rate = ModelRate("m", "M", prices.input, prices.cache, prices.output)
    start = Tokens(input=lower_input, cache_read=cache, output=lower_output)
    more_input = Tokens(input=upper_input, cache_read=cache, output=lower_output)
    more_output = Tokens(input=upper_input, cache_read=cache, output=upper_output)
    assert (
        estimate_cost(start, rate, AT).pico_usd
        <= estimate_cost(more_input, rate, AT).pico_usd
        <= estimate_cost(more_output, rate, AT).pico_usd
    )


@given(COUNTS, COUNTS, SCALED_RATES, SCALED_RATES)
def test_all_cached_input_never_needs_an_input_price(
    input_count: int, output: int, cache_scaled: int, output_scaled: int
) -> None:
    tokens = Tokens(input=input_count, cache_read=input_count, output=output)
    rate = ModelRate("m", "M", None, Rate(cache_scaled).normalized, Rate(output_scaled).normalized)
    result = estimate_cost(tokens, rate, AT)
    assert not result.unpriced
    assert not result.incomplete
    assert result.pico_usd == input_count * cache_scaled + output * output_scaled
