"""Synthetic New API HTTP contracts, bounded streaming and credential boundaries."""

import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import httpx
import pytest
import respx
from htd.adapters.providers import newapi
from htd.adapters.providers.newapi import MAX_RESPONSE_BYTES, NewApiProvider
from htd.domain.common.testing import FakeClock
from htd.domain.remote.model import BalanceKind, BalanceObservation, RemoteAccount

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/remote/newapi"
BASE = "https://example.test"
SECRET = "synthetic-p10-http-credential-1234"
ACCOUNT = RemoteAccount(
    "account", "vendor", BASE, "HarnessTokenDeck/remote/account", "access_token", "42", site_type="newapi"
)


def sample(name: str) -> bytes:
    return (FIXTURES / (name + ".json")).read_bytes()


def payload(name: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads(sample(name)))


def mock_status(mock: respx.MockRouter, *, status: int = 200) -> respx.Route:
    return mock.get(BASE + "/api/status").respond(status, content=sample("status"))


def fetch(account: RemoteAccount = ACCOUNT, clock: FakeClock | None = None) -> BalanceObservation:
    async def run() -> BalanceObservation:
        async with NewApiProvider(clock or FakeClock()) as provider:
            return await provider.fetch(account, SECRET)

    return asyncio.run(run())


@pytest.mark.parametrize(
    "mode,path,name,kind",
    [
        ("access_token", "/api/user/self", "user-self", BalanceKind.ACCOUNT_REMAINING),
    ],
)
def test_modes_success_and_public_status_has_no_authentication(
    respx_mock: respx.MockRouter, mode: str, path: str, name: str, kind: BalanceKind
) -> None:
    route = respx_mock.get(BASE + path).respond(content=sample(name))
    status = mock_status(respx_mock)
    observation = fetch(replace(ACCOUNT, auth_mode=mode, user_id="42"))
    assert observation.error is None and observation.kind == kind
    assert observation.raw_quota == 987655 and observation.quota_per_unit == 500000
    assert observation.amount_pico == 1_975_310_000_000 and observation.can_calibrate
    assert observation.source == "example.test" and observation.currency == "USD"
    assert observation.display_in_currency is True
    request = cast(httpx.Request, route.calls[0][0])
    public_request = cast(httpx.Request, status.calls[0][0])
    assert request.headers["Authorization"] == "Bearer " + SECRET
    assert request.headers.get("New-Api-User") == ("42" if mode == "access_token" else None)
    assert "Authorization" not in public_request.headers
    assert "New-Api-User" not in public_request.headers
    assert SECRET not in repr(observation)


@pytest.mark.parametrize("conversion", [None, 250000])
@pytest.mark.parametrize("failure", ["http", "json", "negative", "false", "network"])
def test_status_failure_uses_only_explicit_override(
    respx_mock: respx.MockRouter, conversion: int | None, failure: str
) -> None:
    respx_mock.get(BASE + "/api/user/self").respond(content=sample("user-self"))
    route = respx_mock.get(BASE + "/api/status")
    if failure == "network":
        route.side_effect = httpx.ConnectError(SECRET)
    elif failure == "http":
        route.respond(503)
    elif failure == "json":
        route.respond(content=b"not json")
    elif failure == "false":
        route.respond(content=sample("error"))
    else:
        route.respond(json={"success": True, "data": {"quota_per_unit": -1}})
    result = fetch(replace(ACCOUNT, quota_per_unit=conversion))
    assert result.error is None and result.raw_quota == 987655
    assert result.quota_per_unit == conversion
    assert result.kind == (BalanceKind.ACCOUNT_REMAINING if conversion else BalanceKind.QUOTA_UNITS)
    assert result.amount_pico == (3_950_620_000_000 if conversion else None)
    assert result.can_calibrate == bool(conversion)


def test_status_cache_is_per_site_and_expires_after_24_monotonic_hours(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(BASE + "/api/user/self").respond(content=sample("user-self"))
    status = mock_status(respx_mock)
    second = "https://second.test"
    respx_mock.get(second + "/api/user/self").respond(content=sample("user-self"))
    second_status = respx_mock.get(second + "/api/status").respond(
        json={"success": True, "data": {"quota_per_unit": 1000000, "display_in_currency": False}}
    )
    clock = FakeClock()

    async def run() -> None:
        async with NewApiProvider(clock) as provider:
            assert (await provider.fetch(ACCOUNT, SECRET)).quota_per_unit == 500000
            clock.advance(24 * 60 * 60 * 1000 - 1)
            assert (await provider.fetch(ACCOUNT, "changed-credential")).quota_per_unit == 500000
            other = await provider.fetch(replace(ACCOUNT, base_url=second), SECRET)
            assert other.quota_per_unit == 1000000 and other.display_in_currency is False
            assert status.call_count == second_status.call_count == 1
            clock.advance(1)
            status.respond(503)
            expired = await provider.fetch(ACCOUNT, SECRET)
            assert expired.kind == BalanceKind.QUOTA_UNITS and status.call_count == 2

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["api_key"])
def test_explicit_sub2api_requests_only_its_balance_endpoint(respx_mock: respx.MockRouter, mode: str) -> None:
    respx_mock.get(BASE + "/v1/usage").respond(content=sample("sub2api-usage"))
    result = fetch(replace(ACCOUNT, site_type="sub2api", auth_mode=mode, user_id=None))
    assert result.error is None and result.amount_pico == 1_234_567_890_123 and result.can_calibrate
    assert len(respx_mock.calls) == 1


def test_explicit_newapi_does_not_try_sub2api_when_its_endpoint_is_missing(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(BASE + "/api/user/self").respond(404)
    other = respx_mock.get(BASE + "/v1/usage").respond(content=sample("sub2api-usage"))
    result = fetch(replace(ACCOUNT, site_type="newapi", auth_mode="access_token", user_id="42"))
    assert result.error is not None and result.error.status_code == 404 and not result.can_calibrate
    assert other.call_count == 0 and len(respx_mock.calls) == 1


def test_explicit_sub2api_does_not_try_newapi_when_its_endpoint_is_missing(
    respx_mock: respx.MockRouter,
) -> None:
    respx_mock.get(BASE + "/v1/usage").respond(404)
    other = respx_mock.get(BASE + "/api/user/self").respond(content=sample("user-self"))
    result = fetch(replace(ACCOUNT, site_type="sub2api", auth_mode="api_key", user_id=None))
    assert result.error is not None and result.error.status_code == 404 and not result.can_calibrate
    assert other.call_count == 0 and len(respx_mock.calls) == 1


def test_explicit_newapi_requests_user_and_public_conversion_only(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(BASE + "/api/user/self").respond(content=sample("user-self"))
    mock_status(respx_mock)
    result = fetch(replace(ACCOUNT, site_type="newapi", auth_mode="access_token", user_id="42"))
    assert result.error is None and result.amount_pico == 1_975_310_000_000 and result.can_calibrate
    assert len(respx_mock.calls) == 2


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("isValid", False, "auth_failed"),
        ("isValid", 1, "invalid_response"),
        ("isValid", "true", "invalid_response"),
        ("isValid", None, "invalid_response"),
        ("unit", "CNY", "invalid_response"),
        ("unit", "tokens", "invalid_response"),
        ("unit", None, "invalid_response"),
        ("balance", "1.25", "invalid_response"),
        ("balance", True, "invalid_response"),
        ("balance", None, "invalid_response"),
    ],
)
def test_sub2api_requires_valid_key_explicit_usd_and_numeric_balance(
    respx_mock: respx.MockRouter, field: str, value: object, code: str
) -> None:
    response = payload("sub2api-usage")
    response[field] = value
    respx_mock.get(BASE + "/v1/usage").respond(json=response)
    result = fetch(replace(ACCOUNT, site_type="sub2api", auth_mode="api_key", user_id=None))
    assert result.error is not None and result.error.code == code
    assert result.amount_pico is None and not result.can_calibrate


@pytest.mark.parametrize(
    "balance,expected", [("0", 0), ("-0.0000000000019", -1), ("1.0000000000009", 10**12)]
)
def test_sub2api_balance_is_exact_and_ignores_remaining_even_for_limited_keys(
    respx_mock: respx.MockRouter, balance: str, expected: int
) -> None:
    respx_mock.get(BASE + "/v1/usage").respond(
        content=(
            '{"isValid":true,"unit":"USD","mode":"quota","remaining":99,"balance":' + balance + "}"
        ).encode()
    )
    result = fetch(replace(ACCOUNT, site_type="sub2api", auth_mode="api_key", user_id=None))
    assert result.error is None and result.amount_pico == expected and result.can_calibrate


def test_sub2api_missing_balance_does_not_use_remaining(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(BASE + "/v1/usage").respond(json={"isValid": True, "unit": "USD", "remaining": 999})
    result = fetch(replace(ACCOUNT, site_type="sub2api", auth_mode="api_key", user_id=None))
    assert result.error is not None and result.error.code == "invalid_response" and not result.can_calibrate


def test_total_timeout_also_bounds_sub2api(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(newapi, "TOTAL_TIMEOUT_SECONDS", 0.02)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.03)
        return httpx.Response(200, content=sample("sub2api-usage"))

    respx_mock.get(BASE + "/v1/usage").side_effect = slow
    result = fetch(replace(ACCOUNT, site_type="sub2api", auth_mode="api_key", user_id=None))
    assert result.error is not None and result.error.code == "timeout" and not result.can_calibrate


@pytest.mark.parametrize("status,code", [(401, "auth_failed"), (403, "auth_failed"), (429, "rate_limited")])
def test_http_failures_keep_status_and_retry_after_but_redact_message(
    respx_mock: respx.MockRouter, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx_mock.get(BASE + "/api/user/self").respond(
        status,
        headers={"Retry-After": "120"},
        json={
            "message": SECRET + " Authorization: Bearer other-secret api_key=third-secret " + "x" * 300,
            "data": {"unrelated": SECRET},
        },
    )
    result = fetch()
    assert result.error is not None and result.error.code == code and result.error.status_code == status
    assert result.error.retry_after_seconds == 120 and len(result.error.message) == 200
    assert result.amount_pico is None and not result.can_calibrate and route.call_count == 1
    serialized = repr(result) + caplog.text
    assert all(secret not in serialized for secret in (SECRET, "other-secret", "third-secret", "unrelated"))


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Tue, 01 Sep 2026 00:02:00 GMT", 120),
        ("Mon, 31 Aug 2026 23:59:00 GMT", 0),
        ("not-a-date", None),
        ("-3", None),
    ],
)
def test_retry_after_http_date_and_invalid_header(
    respx_mock: respx.MockRouter, header: str, expected: int | None
) -> None:
    respx_mock.get(BASE + "/api/user/self").respond(429, headers={"Retry-After": header})
    error = fetch().error
    assert error is not None and error.retry_after_seconds == expected


def test_success_false_is_a_safe_remote_failure(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(BASE + "/api/user/self").respond(
        json={"success": False, "message": "rejected " + SECRET, "data": {"quota": 987655}}
    )
    result = fetch(replace(ACCOUNT, auth_mode="access_token", user_id="42"))
    assert result.error is not None and result.error.code == "remote_failed"
    assert SECRET not in repr(result) and result.amount_pico is None


@pytest.mark.parametrize("body", [b"not json", b"[]", b"null", b'{"code":NaN}', b"\xff"])
def test_non_json_or_invalid_json_is_not_echoed(respx_mock: respx.MockRouter, body: bytes) -> None:
    respx_mock.get(BASE + "/api/user/self").respond(content=body)
    error = fetch().error
    assert error is not None and error.code == "invalid_response"


@pytest.mark.parametrize(
    "error,code",
    [
        (httpx.ReadTimeout(SECRET), "timeout"),
        (httpx.ConnectError(SECRET), "offline"),
        (ssl.SSLCertVerificationError(SECRET), "tls_failed"),
    ],
)
def test_network_failures_never_keep_exception_details(
    respx_mock: respx.MockRouter, error: Exception, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    respx_mock.get(BASE + "/api/user/self").side_effect = error
    result = fetch()
    assert result.error is not None and result.error.code == code
    assert SECRET not in repr(result) + caplog.text


@pytest.mark.parametrize(
    "location",
    [
        "https://other.test/api/user/self",
        "https://example.test:8443/api/user/self",
        "http://example.test/api/user/self",
        "https://user:password@example.test/other",
        "https://example.test/other?token=value",
        "//other.test/api/user/self",
    ],
)
def test_unsafe_redirect_is_rejected_before_a_second_request(
    respx_mock: respx.MockRouter, location: str
) -> None:
    respx_mock.get(BASE + "/api/user/self").respond(302, headers={"Location": location})
    result = fetch()
    assert result.error is not None and result.error.code == "redirect_rejected"
    assert len(respx_mock.calls) == 1


@pytest.mark.parametrize("redirects", [1, 2, 3])
def test_same_origin_redirects_are_limited_to_two(respx_mock: respx.MockRouter, redirects: int) -> None:
    for index in range(redirects):
        path = "/api/user/self" if index == 0 else f"/hop{index}"
        respx_mock.get(BASE + path).respond(302, headers={"Location": f"/hop{index + 1}"})
    final = respx_mock.get(BASE + f"/hop{redirects}").respond(content=sample("user-self"))
    if redirects <= 2:
        mock_status(respx_mock)
    result = fetch()
    if redirects <= 2:
        assert result.can_calibrate and final.call_count == 1
        assert cast(httpx.Request, final.calls[0][0]).headers["Authorization"] == "Bearer " + SECRET
    else:
        assert result.error is not None and result.error.code == "redirect_rejected"
        assert final.call_count == 0 and len(respx_mock.calls) == 3


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.delivered = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.delivered += len(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def test_oversized_stream_is_aborted_and_closed_before_reading_all(respx_mock: respx.MockRouter) -> None:
    stream = ByteStream([b"x" * 16 * 1024] * 100)
    respx_mock.get(BASE + "/api/user/self").return_value = httpx.Response(200, stream=stream)
    error = fetch().error
    assert error is not None and error.code == "response_too_large"
    assert stream.closed and stream.delivered == MAX_RESPONSE_BYTES + 16 * 1024


def test_content_length_rejects_oversized_body_without_reading(respx_mock: respx.MockRouter) -> None:
    stream = ByteStream([b"sensitive body"])
    respx_mock.get(BASE + "/api/user/self").return_value = httpx.Response(
        200, headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}, stream=stream
    )
    error = fetch().error
    assert error is not None and error.code == "response_too_large"
    assert stream.closed and stream.delivered == 0


def test_exactly_256_kib_is_accepted(respx_mock: respx.MockRouter) -> None:
    body = sample("user-self")
    respx_mock.get(BASE + "/api/user/self").respond(content=body + b" " * (MAX_RESPONSE_BYTES - len(body)))
    mock_status(respx_mock)
    assert fetch().can_calibrate


def test_compressed_response_cannot_bypass_the_size_bound(respx_mock: respx.MockRouter) -> None:
    import gzip

    body = gzip.compress(b"x" * (MAX_RESPONSE_BYTES * 10))
    stream = ByteStream([body])
    respx_mock.get(BASE + "/api/user/self").return_value = httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=stream
    )
    error = fetch().error
    assert error is not None and error.code == "invalid_response" and stream.delivered == 0 and stream.closed


def test_total_timeout_includes_all_requests(
    respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(newapi, "TOTAL_TIMEOUT_SECONDS", 0.02)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.015)
        return httpx.Response(
            200, content=sample("token" if request.url.path.endswith("token") else "status")
        )

    respx_mock.get(BASE + "/api/user/self").side_effect = slow
    respx_mock.get(BASE + "/api/status").side_effect = slow
    error = fetch().error
    assert error is not None and error.code == "timeout"


def test_cancelling_fetch_closes_in_progress_response(respx_mock: respx.MockRouter) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self) -> None:
            closed.set()

    respx_mock.get(BASE + "/api/user/self").return_value = httpx.Response(200, stream=BlockingStream())

    async def run() -> None:
        async with NewApiProvider(FakeClock()) as provider:
            task = asyncio.create_task(provider.fetch(ACCOUNT, SECRET))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert closed.is_set()

    asyncio.run(run())


def test_base_url_path_is_preserved(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(BASE + "/newapi/api/user/self").respond(content=sample("user-self"))
    respx_mock.get(BASE + "/newapi/api/status").respond(content=sample("status"))
    assert fetch(replace(ACCOUNT, base_url=BASE + "/newapi/")).can_calibrate


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1:8080"])
def test_http_is_allowed_only_for_local_test_sites(respx_mock: respx.MockRouter, url: str) -> None:
    respx_mock.get(url + "/api/user/self").respond(content=sample("user-self"))
    respx_mock.get(url + "/api/status").respond(503)
    assert fetch(replace(ACCOUNT, base_url=url)).error is None


@pytest.mark.parametrize("quota", [True, "987655", None, -1.5])
def test_invalid_quota_does_not_become_an_amount(respx_mock: respx.MockRouter, quota: object) -> None:
    value = payload("user-self")
    data = cast(dict[str, object], value["data"])
    data["quota"] = quota
    respx_mock.get(BASE + "/api/user/self").respond(json=value)
    error = fetch().error
    assert error is not None and error.code == "invalid_response"


@pytest.mark.parametrize("site_type", ["newapi", "sub2api"])
def test_extreme_json_exponents_are_rejected_before_big_integer_allocation(
    respx_mock: respx.MockRouter, site_type: str
) -> None:
    if site_type == "newapi":
        respx_mock.get(BASE + "/api/user/self").respond(
            content=b'{"success":true,"data":{"quota":1e-999999999}}'
        )
        account = ACCOUNT
    else:
        respx_mock.get(BASE + "/v1/usage").respond(
            content=b'{"isValid":true,"unit":"USD","balance":1e-999999999}'
        )
        account = replace(ACCOUNT, site_type="sub2api", auth_mode="api_key", user_id=None)
    error = fetch(account).error
    assert error is not None and error.code == "invalid_response"


@pytest.mark.parametrize("mode", ["api_key", "billing", "access_token"])
def test_legacy_auto_account_never_tries_any_authenticated_endpoint(
    respx_mock: respx.MockRouter, mode: str
) -> None:
    legacy = replace(ACCOUNT, site_type="auto", auth_mode=mode)
    result = fetch(legacy)
    assert result.error is not None and result.error.code == "configuration_required"
    assert len(respx_mock.calls) == 0


@pytest.mark.parametrize("today,total", [("0.0123456789019", "2.5"), ("true", '"invalid"'), ("1e30", "1e30")])
def test_sub2api_key_usage_is_optional_and_never_used_as_account_cumulative(
    respx_mock: respx.MockRouter, today: str, total: str
) -> None:
    route = respx_mock.get(BASE + "/v1/usage").respond(
        content=(
            f'{{"isValid":true,"unit":"USD","balance":3,"usage":'
            f'{{"today":{{"actual_cost":{today}}},"total":{{"actual_cost":{total}}}}}}}'
        ).encode()
    )
    observation = fetch(replace(ACCOUNT, site_type="sub2api", auth_mode="api_key", user_id=None))
    assert observation.error is None and observation.amount_pico == 3 * 10**12
    assert observation.cumulative_used_pico is None and observation.usage_scope is None
    assert observation.key_today_pico == (12_345_678_901 if today.startswith("0.") else None)
    assert observation.key_total_pico == (2_500_000_000_000 if today.startswith("0.") else None)
    assert route.call_count == 1
