"""Creation tokens replace their ordinary-input portion; all arithmetic stays integer pico."""

from dataclasses import replace

import pytest
from htd.domain.billing.estimate import estimate_cost
from htd.domain.common.time import parse_instant
from htd.domain.pricing.model import BillingRule, ModelRate, PriceRates
from htd.domain.usage.model import Tokens

AT = parse_instant("2026-09-21T01:30:00Z")
TOKENS = Tokens(1000, 200, 300, 50, 0)


@pytest.mark.parametrize("price,expected", [(None, 620_000_000), ("0", 620_000_000), ("1.25", 995_000_000)])
def test_creation_has_a_separate_price_and_defaults_to_zero(price: str | None, expected: int) -> None:
    cost = estimate_cost(TOKENS, ModelRate("m", "m", "1", "0.1", "2", cache_write=price), AT)
    assert cost.pico_usd == expected and not cost.unpriced and not cost.incomplete


def test_all_creation_does_not_require_ordinary_input_price() -> None:
    cost = estimate_cost(Tokens(300, 0, 300, 0), ModelRate("m", "m", cache_write="2"), AT)
    assert cost.pico_usd == 600_000_000 and not cost.unpriced


@pytest.mark.parametrize("mode", ["peak", "tier"])
def test_rule_creation_price_overrides_base(mode: str) -> None:
    rule = BillingRule(mode, PriceRates("2", "0.2", "4", "3"), (1,), (1,), 0, 1)  # type: ignore[arg-type]
    tokens = replace(TOKENS, input=1001)
    cost = estimate_cost(tokens, ModelRate("m", "m", "1", "0.1", "2", rule, "99"), AT)
    assert cost.pico_usd == 2_142_000_000
    assert cost.rule_applied == mode


def test_unknown_creation_quantity_with_nonzero_price_is_incomplete() -> None:
    cost = estimate_cost(Tokens(100, 0, None, 10), ModelRate("m", "m", "1", "0", "2", cache_write="3"), AT)
    assert cost.pico_usd == 20_000_000 and cost.incomplete
