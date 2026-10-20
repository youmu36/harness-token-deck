"""Validate remote observations and the conditions for exact wallet calibration."""

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from htu.domain.common.errors import DomainError
from htu.domain.common.redaction import redact_credentials
from htu.domain.common.time import Instant


@dataclass(frozen=True, slots=True)
class RemoteAccount:
    id: str
    vendor_id: str
    base_url: str
    credential_ref: str
    auth_mode: str = "api_key"
    user_id: str | None = None
    quota_per_unit: int | None = None
    refresh_minutes: int = 5
    enabled: bool = True
    adapter: str = "newapi"
    site_type: str = "auto"

    @property
    def configured(self) -> bool:
        return (self.site_type, self.auth_mode) in (
            ("newapi", "access_token"),
            ("sub2api", "api_key"),
            ("deepseek", "api_key"),
        )

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or not value.strip()
            for value in (self.id, self.vendor_id, self.credential_ref)
        ):
            raise ValueError("Invalid remote account identity or credential reference")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.id) is None or type(self.base_url) is not str:
            raise ValueError("Invalid remote account identity or URL")
        url = urlsplit(self.base_url)
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("Invalid remote URL port")
        if any(ord(character) <= 32 or character == "\\" for character in self.base_url):
            raise ValueError("Remote URL contains whitespace or backslashes")
        if (
            re.fullmatch(
                re.escape("HarnessTokenUsage/remote/" + self.id) + r"(?:/v_[0-9a-f]{32})?",
                self.credential_ref,
            )
            is None
        ):
            raise ValueError("Remote account stores a credential reference, never a credential")
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or not (
                url.scheme == "https" or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1"))
            )
        ):
            raise ValueError("Invalid remote base URL")
        if self.adapter != "newapi" or self.auth_mode not in ("api_key", "access_token", "billing"):
            raise ValueError("Invalid remote adapter or authentication mode")
        if (
            self.site_type not in ("auto", "newapi", "sub2api", "deepseek")
            or (self.site_type == "newapi" and self.auth_mode != "access_token")
            or (self.site_type == "sub2api" and self.auth_mode == "access_token")
            or (self.site_type == "deepseek" and self.auth_mode != "api_key")
        ):
            raise ValueError("Invalid remote site type or authentication mode")
        if self.auth_mode == "access_token" and (
            type(self.user_id) is not str
            or not self.user_id.isascii()
            or not self.user_id.isdecimal()
            or len(self.user_id) > 20
            or int(self.user_id) <= 0
        ):
            raise ValueError("Access-token mode requires a positive numeric user ID")
        if self.quota_per_unit is not None and (
            type(self.quota_per_unit) is not int
            or self.quota_per_unit <= 0
            or len(str(self.quota_per_unit)) > 40
        ):
            raise ValueError("Invalid quota conversion")
        if (
            type(self.refresh_minutes) is not int
            or not 1 <= self.refresh_minutes <= 60
            or type(self.enabled) is not bool
        ):
            raise ValueError("Invalid remote refresh settings")


class BalanceKind(StrEnum):
    TOKEN_REMAINING = "token_remaining"
    ACCOUNT_REMAINING = "account_remaining"
    SUBSCRIPTION_REMAINING = "subscription_remaining"
    UNLIMITED = "unlimited"
    QUOTA_UNITS = "quota_units"


def quota_to_pico(quota: int, quota_per_unit: int) -> int:
    if type(quota) is not int or type(quota_per_unit) is not int or quota_per_unit <= 0:
        raise DomainError("REMOTE_QUOTA_INVALID", "配额与换算系数须为整数，换算系数须大于零")
    amount = abs(quota) * 10**12 // quota_per_unit
    return -amount if quota < 0 else amount


@dataclass(frozen=True, slots=True)
class RemoteFailure:
    code: str
    message: str
    status_code: int | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", self.code) is None:
            raise ValueError("Invalid stable remote failure code")
        if self.status_code is not None and (
            type(self.status_code) is not int or not 100 <= self.status_code <= 599
        ):
            raise ValueError("Invalid HTTP status code")
        if self.retry_after_seconds is not None and (
            type(self.retry_after_seconds) is not int or self.retry_after_seconds < 0
        ):
            raise ValueError("Invalid Retry-After delay")
        object.__setattr__(self, "message", redact_credentials(self.message)[:200])


@dataclass(frozen=True, slots=True)
class BalanceObservation:
    account_id: str
    fetched_at: Instant
    kind: BalanceKind
    source: str
    currency: str | None = None
    amount_pico: int | None = None
    raw_quota: int | None = None
    quota_per_unit: int | None = None
    stale: bool = False
    currency_uncertain: bool = False
    display_in_currency: bool | None = None
    error: RemoteFailure | None = None
    cumulative_used_pico: int | None = None
    usage_scope: str | None = None
    original_currency: str | None = None
    conversion_rule: str | None = None
    key_today_pico: int | None = None
    key_total_pico: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.account_id) is not str
            or not self.account_id
            or type(self.fetched_at) is not Instant
            or type(self.kind) is not BalanceKind
            or type(self.source) is not str
            or not self.source
            or type(self.stale) is not bool
            or type(self.currency_uncertain) is not bool
            or (self.display_in_currency is not None and type(self.display_in_currency) is not bool)
            or (self.error is not None and type(self.error) is not RemoteFailure)
        ):
            raise ValueError("Invalid balance observation")
        for value in (self.amount_pico, self.raw_quota, self.quota_per_unit, self.cumulative_used_pico):
            if value is not None and (type(value) is not int or len(str(value)) > 40):
                raise ValueError("Invalid remote integer")
        if self.cumulative_used_pico is not None and (self.cumulative_used_pico < 0 or not self.usage_scope):
            raise ValueError("Cumulative usage needs a nonnegative amount and a verified scope")
        if self.quota_per_unit is not None and self.quota_per_unit <= 0:
            raise ValueError("Invalid quota conversion")
        for value in (self.key_today_pico, self.key_total_pico):
            if value is not None and (type(value) is not int or value < 0 or len(str(value)) > 40):
                raise ValueError("Invalid key usage hint")
        if self.original_currency is not None and re.fullmatch(r"[A-Z]{3}", self.original_currency) is None:
            raise ValueError("Invalid original currency")
        if (
            self.conversion_rule is not None
            and re.fullmatch(r"[A-Za-z0-9_:.\-]{1,80}", self.conversion_rule) is None
        ):
            raise ValueError("Invalid conversion rule")
        if self.amount_pico is not None and (
            self.error is not None or self.kind in (BalanceKind.UNLIMITED, BalanceKind.QUOTA_UNITS)
        ):
            raise ValueError("This observation cannot carry an amount")

    @property
    def can_calibrate(self) -> bool:
        return (
            self.error is None
            and not self.stale
            and self.kind in (BalanceKind.TOKEN_REMAINING, BalanceKind.ACCOUNT_REMAINING)
            and self.amount_pico is not None
            and self.currency == "USD"
            and not self.currency_uncertain
        )
