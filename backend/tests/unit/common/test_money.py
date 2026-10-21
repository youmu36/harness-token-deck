import json
from dataclasses import FrozenInstanceError
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import cast

import pytest
from htd.domain.common.errors import AMOUNT_INVALID, RATE_INVALID, DomainError
from htd.domain.common.money import (
    MAX_SCALED_RATE,
    MoneyFormat,
    Rate,
    format_pico,
    parse_rate,
    parse_usd_amount,
)
from hypothesis import given
from hypothesis import strategies as st

VECTORS = cast(
    list[dict[str, str]],
    json.loads(
        (Path(__file__).resolve().parents[4] / "fixtures/format/money.json").read_text(encoding="utf-8")
    ),
)


@pytest.mark.parametrize(
    ("text", "normalized", "scaled"),
    [
        ("1.50", "1.5", 1_500_000),
        ("007", "7", 7_000_000),
        ("0.000000", "0", 0),
        ("000.010000", "0.01", 10_000),
        ("0.000001", "0.000001", 1),
        ("999999999.999999", "999999999.999999", MAX_SCALED_RATE),
    ],
)
def test_rate_normalization(text: str, normalized: str, scaled: int) -> None:
    rate = parse_rate(text)
    assert rate.normalized == normalized
    assert rate.scaled == scaled


@pytest.mark.parametrize(
    "invalid",
    [
        "1.2345678",
        "1.2345670",
        "-1",
        "-0",
        "1e3",
        "+1",
        "1.",
        ".5",
        "",
        " 1",
        "1 ",
        "1\n",
        "1,000",
        "\uff11\uff12",
        "\u0661",
        "NaN",
        "Infinity",
        "1000000000",
        "0000000000",
        None,
        True,
        1,
    ],
)
def test_rate_rejects_invalid_input(invalid: object) -> None:
    with pytest.raises(DomainError) as failure:
        parse_rate(cast(str, invalid))
    assert failure.value.code == RATE_INVALID
    assert failure.value.field == "rate"


@pytest.mark.parametrize(
    ("text", "pico"),
    [
        ("-0.5", -500_000_000_000),
        ("-12.345678", -12_345_678_000_000),
        ("0.000001", 1_000_000),
        ("-0.000000", 0),
        ("007.050", 7_050_000_000_000),
        ("999999999.999999", 999_999_999_999_999_000_000),
    ],
)
def test_amount_conversion(text: str, pico: int) -> None:
    assert parse_usd_amount(text, allow_negative=True) == pico


@pytest.mark.parametrize(
    "invalid",
    [
        "-0",
        "-0.000001",
        "+1",
        "1e3",
        "1.0000000",
        "1000000000",
        " 1",
        "1\n",
        ".5",
        "1.",
        "\uff11\uff12",
        None,
        True,
        1.5,
    ],
)
def test_amount_rejects_invalid_input(invalid: object) -> None:
    with pytest.raises(DomainError) as failure:
        parse_usd_amount(cast(str, invalid))
    assert failure.value.code == AMOUNT_INVALID


@pytest.mark.parametrize("vector", VECTORS, ids=[item["pico"] for item in VECTORS])
def test_shared_format_vectors(vector: dict[str, str]) -> None:
    for mode in ("usd4", "trunc2", "full6"):
        assert format_pico(int(vector["pico"]), mode) == vector[mode]


def test_values_cannot_bypass_validation_or_mutate() -> None:
    for invalid in (-1, MAX_SCALED_RATE + 1, True, 1.5):
        with pytest.raises(DomainError, match="价格"):
            Rate(cast(int, invalid))
    rate = parse_rate("1")
    with pytest.raises(FrozenInstanceError):
        rate.scaled = 2  # pyright: ignore[reportAttributeAccessIssue] - test frozen runtime guard
    with pytest.raises(DomainError) as failure:
        format_pico(True, "usd4")
    assert failure.value.code == "PICO_INVALID"
    with pytest.raises(DomainError) as failure:
        format_pico(0, cast(MoneyFormat, "unknown"))
    assert failure.value.code == "MONEY_FORMAT_INVALID"


@given(st.integers(min_value=0, max_value=MAX_SCALED_RATE))
def test_rate_and_amount_roundtrip(scaled: int) -> None:
    rate = Rate(scaled)
    assert parse_rate(rate.normalized) == rate
    assert parse_usd_amount(rate.normalized) == scaled * 1_000_000
    assert parse_usd_amount("-" + rate.normalized, allow_negative=True) == -scaled * 1_000_000


@given(st.integers(min_value=-(2**127), max_value=2**127 - 1))
def test_display_matches_independent_decimal_oracle(pico: int) -> None:
    with localcontext() as context:
        context.prec = 80
        value = Decimal(pico) / Decimal(10**12)
        for mode, precision in (("usd4", "0.0001"), ("full6", "0.000001")):
            expected = value.quantize(Decimal(precision), rounding=ROUND_HALF_UP)
            rendered = format_pico(pico, cast(MoneyFormat, mode))
            actual = Decimal(rendered.replace("$", "").replace(",", "").removesuffix(" USD"))
            assert actual == expected
            assert actual != 0 or not rendered.startswith("-")
        truncated = Decimal(format_pico(pico, "trunc2"))
        assert abs(truncated) <= abs(value)
        assert abs(value - truncated) < Decimal("0.01")
