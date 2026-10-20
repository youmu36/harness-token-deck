"""Official DeepSeek account endpoint and exact USD string amounts with synthetic HTTP."""

from dataclasses import replace
from typing import cast

import httpx
import pytest
import respx

from .test_newapi_provider import ACCOUNT, BASE, SECRET, fetch

DEEPSEEK = replace(ACCOUNT, auth_mode="api_key", user_id=None, site_type="deepseek")


@pytest.mark.parametrize(
    "available,amount,expected",
    [
        (True, "123.456789012345", 123_456_789_012_345),
        (False, "0.00", 0),
        (False, "-0.000000000001", -1),
    ],
)
def test_deepseek_uses_bearer_and_account_total_only(
    respx_mock: respx.MockRouter, available: bool, amount: str, expected: int
) -> None:
    route = respx_mock.get(BASE + "/user/balance").respond(
        json={
            "is_available": available,
            "balance_infos": [{"currency": "USD", "total_balance": amount, "topped_up_balance": "99"}],
        }
    )
    result = fetch(DEEPSEEK)
    assert result.can_calibrate and result.amount_pico == expected
    request = cast(httpx.Request, route.calls[0][0])
    assert request.headers["Authorization"] == "Bearer " + SECRET
    assert "New-Api-User" not in request.headers
    assert len(respx_mock.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"is_available": True, "balance_infos": [{"currency": "EUR", "total_balance": "10"}]},
        {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "NaN"}]},
        {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "1e9999"}]},
        {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": 123}]},
        {"is_available": "true", "balance_infos": []},
        {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "1"},
                {"currency": "USD", "total_balance": "2"},
            ],
        },
    ],
)
def test_malformed_or_ambiguous_balance_never_calibrates(
    respx_mock: respx.MockRouter, payload: dict[str, object]
) -> None:
    respx_mock.get(BASE + "/user/balance").respond(json=payload)
    result = fetch(DEEPSEEK)
    assert result.error and result.amount_pico is None and not result.can_calibrate


def test_authentication_failure_does_not_expose_secret(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(BASE + "/user/balance").respond(401, json={"message": SECRET})
    result = fetch(DEEPSEEK)
    assert result.error and result.error.code == "auth_failed"
    assert SECRET not in repr(result)


def test_official_document_cny_example_uses_fixed_one_to_one_usd(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(BASE + "/user/balance").respond(
        json={
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "CNY",
                    "total_balance": "110.00",
                    "granted_balance": "10.00",
                    "topped_up_balance": "100.00",
                }
            ],
        }
    )
    result = fetch(replace(DEEPSEEK, base_url=BASE + "/v1/"))
    assert result.error is None and result.currency == "USD" and result.amount_pico == 110 * 10**12
    assert result.can_calibrate
    assert result.original_currency == "CNY" and result.conversion_rule == "fixed-CNY-USD-1:1-v1"
    request = cast(httpx.Request, route.calls[0][0])
    assert request.headers["Authorization"] == "Bearer " + SECRET and not request.url.query
    assert "New-Api-User" not in request.headers and len(respx_mock.calls) == 1


def test_usd_is_selected_without_converting_or_summing_cny(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(BASE + "/user/balance").respond(
        json={
            "is_available": True,
            "balance_infos": [
                {"currency": "CNY", "total_balance": "110"},
                {"currency": "USD", "total_balance": "1.234"},
            ],
        }
    )
    result = fetch(DEEPSEEK)
    assert result.currency == "USD" and result.amount_pico == 1_234_000_000_000 and result.can_calibrate
    assert result.original_currency == "USD" and result.conversion_rule == "native-USD-v1"
