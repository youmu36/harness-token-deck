"""Bounded New API requests; authentication never follows another origin."""

import asyncio
import json
import math
import ssl
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import cast
from urllib.parse import urlsplit

import httpx

from htd.adapters.providers.deepseek import balance as deepseek_balance
from htd.application.remote.ports import validate_credential
from htd.domain.common.redaction import redact_credentials
from htd.domain.common.time import Clock
from htd.domain.remote.model import (
    BalanceKind,
    BalanceObservation,
    RemoteAccount,
    RemoteFailure,
    quota_to_pico,
)

MAX_RESPONSE_BYTES = 256 * 1024
TOTAL_TIMEOUT_SECONDS = 15
STATUS_CACHE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class _Status:
    quota_per_unit: int
    display_in_currency: bool | None
    expires_at: float


class _RequestFailure(Exception):
    def __init__(self, error: RemoteFailure) -> None:
        super().__init__(error.message)
        self.error = error


def _fail(code: str, message: str) -> _RequestFailure:
    return _RequestFailure(RemoteFailure(code, message))


def _object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _fail("invalid_response", "站点返回的数据结构无效")
    return cast(dict[str, object], value)


def _integer(value: object, *, positive: bool = False) -> int:
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and value.adjusted() <= 39
        and value == value.to_integral_value()
    ):
        value = int(value)
    if type(value) is not int or len(str(value)) > 40 or (positive and value <= 0):
        raise _fail("invalid_response", "站点返回的配额或换算系数无效")
    return value


def _decimal(value: object) -> Decimal:
    if type(value) is int:
        value = Decimal(value)
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value.adjusted() > 30
        or len(value.as_tuple().digits) > 40
        or abs(int(value.as_tuple().exponent)) > 40
    ):
        raise _fail("invalid_response", "站点返回的账单金额无效")
    return value


def _invalid_constant(value: str) -> object:
    raise ValueError("Non-finite JSON number")


def _json(body: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(body, parse_float=Decimal, parse_constant=_invalid_constant)
        return _object(value)
    except (ValueError, UnicodeError, RecursionError):
        raise _fail("invalid_response", "站点响应不是有效的 JSON 对象") from None


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


class NewApiProvider:
    """Use one instance per background event loop; cancel fetch tasks on shutdown."""

    def __init__(self, clock: Clock, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._clock = clock
        self._status_cache: dict[str, _Status] = {}
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._closed = False

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._closed:
            raise RuntimeError("Remote provider is closed")
        if self._http is None:
            self._http = httpx.AsyncClient(
                verify=True,
                timeout=httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=5),
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2, keepalive_expiry=30),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
        return self._http

    async def __aenter__(self) -> "NewApiProvider":
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def fetch(self, account: RemoteAccount, credential: str) -> BalanceObservation:
        validate_credential(credential)
        kind = BalanceKind.ACCOUNT_REMAINING
        source = urlsplit(account.base_url).hostname or ""
        if not account.configured:
            return BalanceObservation(
                account.id,
                self._clock.now_utc(),
                kind,
                source,
                error=RemoteFailure("configuration_required", "请编辑远程账户，选择接口类型并填写对应凭据"),
            )
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                return await self._fetch(account, credential, kind, source)
        except _RequestFailure as error:
            failure = error.error
        except (TimeoutError, httpx.TimeoutException):
            failure = RemoteFailure("timeout", "远程请求超时，请稍后重试")
        except (httpx.HTTPError, ssl.SSLError) as error:
            # Never retain a transport exception, its URL, request headers, or cause in the observation.
            cause: BaseException | None = error
            tls = False
            seen: set[int] = set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                tls |= isinstance(cause, ssl.SSLError)
                cause = cause.__cause__ or cause.__context__
            failure = RemoteFailure(
                "tls_failed" if tls else "offline",
                "站点证书校验失败" if tls else "无法连接远程站点，请检查网络",
            )
        except (ValueError, OverflowError, httpx.InvalidURL):
            failure = RemoteFailure("invalid_response", "站点返回的数据无效")
        return BalanceObservation(account.id, self._clock.now_utc(), kind, source, error=failure)

    async def _fetch(
        self, account: RemoteAccount, credential: str, kind: BalanceKind, source: str
    ) -> BalanceObservation:
        headers = {"Authorization": "Bearer " + credential}
        if account.site_type == "deepseek":
            # Official model callers also use /v1; balance lives at the API root.
            base = account.base_url.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            response = await self._request(base, "/user/balance", headers, credential)
            return deepseek_balance(response, account.id, self._clock.now_utc(), source)
        if account.site_type == "sub2api":
            return await self._sub2api(account, headers, credential, source)
        return await self._newapi(account, credential, kind, source)

    async def _newapi(
        self, account: RemoteAccount, credential: str, kind: BalanceKind, source: str
    ) -> BalanceObservation:
        headers = {"Authorization": "Bearer " + credential}
        headers["New-Api-User"] = account.user_id or ""
        response = await self._request(account.base_url, "/api/user/self", headers, credential)
        if response.get("success") is not True:
            raise self._remote_failure(response, credential)
        data = _object(response.get("data"))
        quota = _integer(data.get("quota"))
        status = await self._status(account.base_url, credential)
        conversion = status.quota_per_unit if status else account.quota_per_unit
        used = None
        if conversion and "used_quota" in data:
            try:
                raw_used = _integer(data["used_quota"])
                if raw_used >= 0:
                    used = quota_to_pico(raw_used, conversion)
            except _RequestFailure:
                pass  # Optional capability failure never discards a valid balance.
        return BalanceObservation(
            account.id,
            self._clock.now_utc(),
            kind if conversion else BalanceKind.QUOTA_UNITS,
            source,
            currency="USD" if conversion else None,
            amount_pico=quota_to_pico(quota, conversion) if conversion else None,
            raw_quota=quota,
            quota_per_unit=conversion,
            display_in_currency=status.display_in_currency if status else None,
            cumulative_used_pico=used,
            usage_scope="newapi:user:used_quota:v1" if used is not None else None,
        )

    async def _sub2api(
        self, account: RemoteAccount, headers: dict[str, str], credential: str, source: str
    ) -> BalanceObservation:
        response = await self._request(account.base_url, "/v1/usage", headers, credential)
        if response.get("isValid") is False:
            raise _fail("auth_failed", "API 令牌无效或已过期")
        if response.get("isValid") is not True or response.get("unit") != "USD":
            raise _fail("invalid_response", "sub2api 未返回有效的 USD 账户余额")
        # remaining can be a key or subscription quota. Only balance is the account balance.
        numerator, denominator = _decimal(response.get("balance")).as_integer_ratio()
        amount = abs(numerator) * 10**12 // denominator
        hints: dict[str, int | None] = {"today": None, "total": None}
        usage = response.get("usage")
        if type(usage) is dict:
            details = _object(cast(object, usage))
            for period in hints:
                part = details.get(period)
                if type(part) is dict:
                    try:
                        value = _decimal(_object(cast(object, part)).get("actual_cost"))
                        if value >= 0:
                            num, den = value.as_integer_ratio()
                            candidate = num * 10**12 // den
                            if len(str(candidate)) <= 40:
                                hints[period] = candidate
                    except _RequestFailure:
                        pass
        return BalanceObservation(
            account.id,
            self._clock.now_utc(),
            BalanceKind.ACCOUNT_REMAINING,
            source,
            currency="USD",
            amount_pico=-amount if numerator < 0 else amount,
            # /v1/usage scopes these counters to this API key. Never feed them
            # into account-wide U settlement or combine them with wallet totals.
            key_today_pico=hints["today"],
            key_total_pico=hints["total"],
        )

    async def _status(self, base_url: str, credential: str) -> _Status | None:
        cache_key = str(httpx.URL(base_url.rstrip("/")))
        cached = self._status_cache.get(cache_key)
        if cached is not None and self._clock.monotonic() < cached.expires_at:
            return cached
        self._status_cache.pop(cache_key, None)
        try:
            response = await self._request(base_url, "/api/status", {}, credential)
            if response.get("success") is not True:
                return None
            data = _object(response.get("data"))
            display = data.get("display_in_currency")
            if display is not None and type(display) is not bool:
                raise _fail("invalid_response", "站点返回的展示币种设置无效")
            status = _Status(
                _integer(data.get("quota_per_unit"), positive=True),
                display,
                self._clock.monotonic() + STATUS_CACHE_SECONDS,
            )
        except (_RequestFailure, httpx.HTTPError, httpx.InvalidURL, ssl.SSLError):
            return None
        self._status_cache[cache_key] = status
        return status

    def _remote_failure(
        self,
        response: dict[str, object],
        credential: str,
        *,
        status: int | None = None,
        retry_after: int | None = None,
    ) -> _RequestFailure:
        code = "auth_failed" if status in (401, 403) else "rate_limited" if status == 429 else "remote_failed"
        message = response.get("message")
        safe = (
            redact_credentials(message, (credential,))[:200] if type(message) is str else "远程站点拒绝了请求"
        )
        return _RequestFailure(RemoteFailure(code, safe, status, retry_after))

    def _retry_after(self, value: str | None) -> int | None:
        if value is None or len(value) > 100:
            return None
        if value.isascii() and value.isdecimal():
            return int(value)
        try:
            at = parsedate_to_datetime(value)
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            return max(0, math.ceil((at - self._clock.now_utc().value).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None

    async def _request(
        self, base_url: str, path: str, headers: dict[str, str], credential: str
    ) -> dict[str, object]:
        url = httpx.URL(base_url.rstrip("/") + path)
        origin = _origin(url)
        for redirects in range(3):
            async with self._client.stream("GET", url, headers=headers) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location or redirects == 2:
                        raise _fail("redirect_rejected", "站点重定向无效或超过两次")
                    target = url.join(location)
                    if (
                        _origin(target) != origin
                        or target.username
                        or target.password
                        or target.query
                        or target.fragment
                    ):
                        raise _fail("redirect_rejected", "站点重定向到其他主机、端口或不安全的地址，已拒绝")
                    url = target
                    continue
                length = response.headers.get("Content-Length", "")
                if length.isdecimal() and int(length) > MAX_RESPONSE_BYTES:
                    raise _fail("response_too_large", "站点响应超过 256 KiB，已中止")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise _fail("invalid_response", "站点未遵守不压缩 JSON 响应的请求")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_raw(chunk_size=16 * 1024):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise _fail("response_too_large", "站点响应超过 256 KiB，已中止")
                    chunks.append(chunk)
                body = b"".join(chunks)
                if not 200 <= response.status_code < 300:
                    try:
                        payload = _json(body)
                    except _RequestFailure:
                        payload = {}
                    raise self._remote_failure(
                        payload,
                        credential,
                        status=response.status_code,
                        retry_after=self._retry_after(response.headers.get("Retry-After")),
                    )
                return _json(body)
        raise _fail("redirect_rejected", "站点重定向超过两次")
