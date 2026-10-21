from decimal import ROUND_HALF_UP, Decimal
from typing import cast

import pytest
from htd.domain.common.errors import MULTIPLIER_INVALID, RATE_OVERFLOW, DomainError
from htd.domain.common.money import Rate
from htd.domain.pricing.multiplier import Multiplier, multiply_rate, parse_multiplier
from hypothesis import given
from hypothesis import strategies as st


@pytest.mark.parametrize(
    ("rate", "factor", "expected"),
    [
        ("1.25", "1.5", "1.875"),
        ("0.123456", "1.001", "0.123579"),
        ("1.5", "1.75", "2.625"),
        ("0.000001", "0.5", "0.000001"),
        ("0.000001", "0.499", "0"),
        ("999999999.999999", "1", "999999999.999999"),
        ("123", "0", "0"),
        ("0", "999999999.999", "0"),
        (None, "2", None),
    ],
)
def test_P11_exact_multiplier_vectors(rate: str | None, factor: str, expected: str | None) -> None:
    assert multiply_rate(rate, factor) == expected


@pytest.mark.parametrize(
    "text",
    [
        "1.0001",
        "-1",
        "+1",
        "1e3",
        ".5",
        "1.",
        " 1",
        "1 ",
        "\u0661",
        "\uff11",
        "1000000000",
        "",
        None,
        True,
        1.0,
    ],
)
def test_invalid_multiplier(text: object) -> None:
    with pytest.raises(DomainError) as failure:
        parse_multiplier(cast(str, text))
    assert failure.value.code == MULTIPLIER_INVALID


@pytest.mark.parametrize(
    ("text", "expected"), [("001.500", "1.5"), ("0.000", "0"), ("999999999.999", "999999999.999")]
)
def test_multiplier_normalization(text: str, expected: str) -> None:
    assert parse_multiplier(text).normalized == expected


def test_overflow_and_unknown_rate_still_validate_multiplier() -> None:
    with pytest.raises(DomainError) as failure:
        multiply_rate("999999999.999999", "1.001")
    assert failure.value.code == RATE_OVERFLOW
    with pytest.raises(DomainError):
        multiply_rate(None, "bad")


@given(st.integers(min_value=0, max_value=10**10), st.integers(min_value=0, max_value=10000))
def test_integer_formula_matches_decimal_oracle(micros: int, millis: int) -> None:
    rate, factor = Rate(micros).normalized, Multiplier(millis).normalized
    expected = (Decimal(rate) * Decimal(factor)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    actual = multiply_rate(rate, factor)
    assert actual is not None
    assert Decimal(actual) == expected
