"""Strict remote settings and safe observation snapshots."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from htu.ipc.dto.base import InstantStr, PicoStr, SafeInt, StrictModel, WriteRequest
from htu.ipc.dto.pricing import Identifier

type AuthMode = Literal["api_key", "access_token", "billing"]
type SiteType = Literal["auto", "newapi", "sub2api", "deepseek"]
type PositiveInteger = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,39}$")]


class RemoteRequest(StrictModel):
    vendorId: Identifier


class RemoteWrite(WriteRequest):
    vendorId: Identifier


class RemoteSettings(RemoteRequest):
    baseUrl: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    authMode: AuthMode = "api_key"
    siteType: Literal["newapi", "sub2api", "deepseek"]
    userId: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,20}$")] | None = None
    # Allow a pasted Bearer prefix/outer whitespace; the normalized token still
    # has the 2048-character limit in the remote credential port.
    credential: Annotated[str, StringConstraints(max_length=4096)] | None = Field(default=None, repr=False)
    quotaPerUnit: PositiveInteger | None = None
    refreshMinutes: Annotated[int, Field(ge=1, le=60)] = 5
    enabled: bool = True


class SaveRemoteRequest(RemoteSettings, WriteRequest):
    pass


class RemoteAccountDto(StrictModel):
    id: Identifier
    vendorId: Identifier
    baseUrl: str
    authMode: AuthMode
    siteType: SiteType
    userId: str | None
    quotaPerUnit: PositiveInteger | None
    refreshMinutes: Annotated[int, Field(ge=1, le=60)]
    enabled: bool
    maskedCredential: str | None


class Observation(StrictModel):
    accountId: Identifier
    fetchedAt: InstantStr
    kind: Literal[
        "token_remaining", "account_remaining", "subscription_remaining", "unlimited", "quota_units"
    ]
    source: str
    currency: str | None
    amountPico: PicoStr | None
    rawQuota: PicoStr | None
    quotaPerUnit: PositiveInteger | None
    stale: bool
    currencyUncertain: bool
    displayInCurrency: bool | None
    canCalibrate: bool
    cumulativeUsedPico: PicoStr | None
    usageScope: str | None
    originalCurrency: str | None
    conversionRule: str | None


class RemoteError(StrictModel):
    code: str
    message: str
    statusCode: Annotated[int, Field(ge=100, le=599)] | None
    retryAfterSeconds: SafeInt | None


class RemoteSnapshot(StrictModel):
    account: RemoteAccountDto | None
    observation: Observation | None
    status: Literal[
        "unconfigured", "pending", "ok", "stale", "offline", "auth_failed", "rate_limited", "failed"
    ]
    error: RemoteError | None
    nextRefreshAt: InstantStr | None
    refreshing: bool
    revision: SafeInt


class RemoteTestResponse(StrictModel):
    observation: Observation | None
    error: RemoteError | None
