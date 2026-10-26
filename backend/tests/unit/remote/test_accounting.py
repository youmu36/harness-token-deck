"""Whole-unit automatic funding keeps exact endpoint and consumption evidence."""

import pytest
from htd.domain.remote.accounting import CENT, USD, settle


@pytest.mark.parametrize("reliable", [False, True])
@pytest.mark.parametrize(
    "candidate,expected",
    [
        (-USD, 0),
        (0, 0),
        (USD - 1, 0),
        (USD, USD),
        (USD + 1, 2 * USD),
        (2 * USD - 1, 2 * USD),
        (2 * USD, 2 * USD),
        (2 * USD + 123_456_789_012, 3 * USD),
        (10**28 + 1, 10**28 + USD),
    ],
)
def test_automatic_recharge_rounds_up_after_recognition_threshold(
    reliable: bool, candidate: int, expected: int
) -> None:
    b0, consumption = 10 * USD, 2 * USD
    b1 = b0 + candidate - consumption
    result = settle(b0, b1, consumption, used=consumption if reliable else None)
    assert result.recharge == expected
    assert result.recharge == 0 or (result.recharge >= USD and result.recharge % USD == 0)
    assert result.candidate == candidate
    assert b1 == b0 + result.recharge - result.consumption + result.adjustment + result.unresolved
    if reliable:
        assert result.consumption == consumption


@pytest.mark.parametrize("reliable", [False, True])
def test_manual_funding_is_matched_before_automatic_remainder(reliable: bool) -> None:
    result = settle(10 * USD, 13 * USD + CENT, USD, used=USD if reliable else None, manual_r=3 * USD)
    assert result.recharge == 2 * USD
    assert (
        10 * USD + 3 * USD + result.recharge - result.consumption + result.adjustment + result.unresolved
    ) == 13 * USD + CENT
    covered = settle(10 * USD, 12 * USD, USD, used=USD if reliable else None, manual_r=3 * USD)
    assert covered.recharge == 0


def test_rounding_does_not_replace_reliable_usage_or_hide_sub_cent_remainder() -> None:
    result = settle(10 * USD, 11 * USD - 1, USD, used=USD)
    assert (result.recharge, result.consumption, result.adjustment, result.unresolved) == (
        2 * USD,
        USD,
        0,
        -1,
    )
