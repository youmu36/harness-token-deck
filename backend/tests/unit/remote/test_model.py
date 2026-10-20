"""Quota signs, exact integer division and calibration eligibility."""

from dataclasses import replace

import pytest
from htu.domain.common.errors import DomainError
from htu.domain.common.time import parse_instant
from htu.domain.remote.model import BalanceKind, BalanceObservation, RemoteFailure, quota_to_pico


@pytest.mark.parametrize(
    "quota,conversion,expected",
    [
        (987655, 500000, 1_975_310_000_000),
        (1, 3, 333_333_333_333),
        (-1, 3, -333_333_333_333),
        (0, 3, 0),
        (10**25 + 1, 1, (10**25 + 1) * 10**12),
    ],
)
def test_quota_to_pico_is_exact_and_truncates_towards_zero(
    quota: int, conversion: int, expected: int
) -> None:
    assert quota_to_pico(quota, conversion) == expected


@pytest.mark.parametrize("quota,conversion", [(True, 1), (1, True), (1, 0), (1, -1)])
def test_invalid_quota_conversion(quota: int, conversion: int) -> None:
    with pytest.raises(DomainError, match="换算系数"):
        quota_to_pico(quota, conversion)


@pytest.mark.parametrize("kind", list(BalanceKind))
def test_calibration_only_accepts_successful_usd_remaining(kind: BalanceKind) -> None:
    amount = None if kind in (BalanceKind.UNLIMITED, BalanceKind.QUOTA_UNITS) else 1
    observation = BalanceObservation(
        "account", parse_instant("2026-09-01T00:00:00Z"), kind, "example.test", "USD", amount
    )
    assert observation.can_calibrate == (kind in (BalanceKind.TOKEN_REMAINING, BalanceKind.ACCOUNT_REMAINING))
    assert not replace(observation, stale=True).can_calibrate
    assert not replace(observation, currency_uncertain=True).can_calibrate
    assert not replace(observation, currency="CNY").can_calibrate
    assert not replace(observation, amount_pico=None).can_calibrate
    assert not replace(observation, amount_pico=None, error=RemoteFailure("offline", "离线")).can_calibrate
