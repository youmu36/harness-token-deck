"""Pure pico-USD billing with whole-rule price selection and usage quality flags."""

from dataclasses import dataclass
from typing import Literal

from htu.domain.common.errors import COST_OVERFLOW, PICO_INVALID, DomainError
from htu.domain.common.money import parse_rate
from htu.domain.common.time import Instant
from htu.domain.pricing.model import BillingRule, ModelRate, PriceRates
from htu.domain.usage.model import Tokens

type RuleApplied = Literal["none", "base", "peak", "tier"]


@dataclass(frozen=True, slots=True)
class CostEstimate:
    pico_usd: int
    incomplete: bool
    unpriced: bool
    rule_applied: RuleApplied

    def __post_init__(self) -> None:
        if type(self.pico_usd) is not int or self.pico_usd < 0:
            raise DomainError(PICO_INVALID, "计费金额须为非负整数 pico", "pico_usd")
        if self.pico_usd >= 2**127:
            raise DomainError(COST_OVERFLOW, "计费金额超出范围", "pico_usd")


def _peak_matches(rule: BillingRule, occurred_at: Instant) -> bool:
    utc = occurred_at.value
    # Calculate only weekday/hour, avoiding overflow at the first/last supported date.
    day_offset, minute_of_day = divmod(utc.hour * 60 + utc.minute + rule.offset_minutes, 1_440)
    weekday = (utc.weekday() + day_offset) % 7 + 1
    return weekday in rule.weekdays and minute_of_day // 60 in rule.hours


def select_prices(
    rate: ModelRate, tokens: Tokens, occurred_at: Instant
) -> tuple[ModelRate | PriceRates, RuleApplied]:
    rule = rate.billing_rule
    if rule is not None:
        if rule.mode == "tier":
            if tokens.input is not None and tokens.input > rule.threshold_k * 1_000:
                return rule.rates, "tier"
        elif _peak_matches(rule, occurred_at):
            return rule.rates, "peak"
    return rate, "base"


def _scaled(price: str | None) -> int:
    return 0 if price is None else parse_rate(price).scaled


def estimate_cost(tokens: Tokens, rate: ModelRate | None, occurred_at: Instant) -> CostEstimate:
    input_count, cache_read, output = tokens.input, tokens.cache_read, tokens.output
    incomplete = input_count is None or cache_read is None or output is None
    if rate is None:
        return CostEstimate(0, incomplete, True, "none")

    prices, applied = select_prices(rate, tokens, occurred_at)
    unpriced = (
        (
            prices.input is None
            and (
                input_count is None
                or cache_read is None
                or input_count > cache_read + (tokens.cache_write or 0)
            )
        )
        or (prices.cache is None and cache_read != 0)
        or (prices.output is None and output != 0)
    )
    if rate.billing_rule is not None and rate.billing_rule.mode == "tier" and input_count is None:
        # The unknown tier suppresses even the known output portion of this request.
        return CostEstimate(0, True, unpriced, applied)

    input_cost = 0
    unknown_write = tokens.cache_write is None and _scaled(prices.cache_write) != 0
    incomplete |= unknown_write
    if input_count is not None and cache_read is not None and not unknown_write:
        writes = tokens.cache_write or 0
        input_cost = (
            (input_count - cache_read - writes) * _scaled(prices.input)
            + cache_read * _scaled(prices.cache)
            + writes * _scaled(prices.cache_write)
        )
    output_cost = (output or 0) * _scaled(prices.output)
    return CostEstimate(input_cost + output_cost, incomplete, unpriced, applied)
