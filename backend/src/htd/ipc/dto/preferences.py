"""Typed preference snapshot and shallow patch; filters merge by page."""

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from htd.domain.common.time import parse_instant
from htd.domain.statistics.window import Window
from htd.ipc.dto.base import InstantStr, SafeInt, StrictModel, WriteRequest

type UsagePage = Literal["overview", "claude", "codex", "zcode", "dsh"]
type HarnessPage = Literal["claude", "codex", "zcode", "dsh"]
type Page = Literal["overview", "claude", "codex", "zcode", "dsh", "wallet", "pricing", "settings"]
type RoutePath = Annotated[
    str,
    StringConstraints(
        max_length=512,
        pattern=r"^(?:|/overview|/harness/(?:claude|codex|zcode|dsh)|/wallet(?:/[^/?#\s]+)?|/pricing(?:/versions)?|/models|/unpriced|/settings(?:/background)?)$",
    ),
]


class UsageFilter(StrictModel):
    range: Literal["today", "24hours", "7days", "30days", "custom"] = "today"
    harness: Literal["all", "claude", "codex", "zcode", "dsh"] = "all"
    start: InstantStr | None = None
    end: InstantStr | None = None
    grain: Literal["hour", "day"] = "hour"
    metric: Literal["tokens", "cost", "hitRate"] = "tokens"
    monitorMode: Literal["minute", "hour"] = "minute"
    modelKey: str | None = None
    startNow: bool = False
    endNow: bool = False

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.range == "custom":
            if self.start is None or self.end is None:
                raise ValueError("自定义范围须包含开始和结束时间")
            Window(parse_instant(self.start), parse_instant(self.end), 0)
        return self


class PreferenceValues(StrictModel):
    theme: Literal["light", "dark", "system"] = "system"
    timezone: Literal["system", "UTC"] = "system"
    showScanCountdown: bool = False
    hiddenHarnesses: list[HarnessPage] = Field(default_factory=list[HarnessPage], max_length=4)
    settingsSection: Literal["display", "sources", "background", "data", "about"] = "display"
    walletDefaultScope: Literal["local", "remote"] = "local"
    collapsed: bool = False
    lastPage: Page = "codex"
    lastRoute: RoutePath = ""
    accountingTimezone: str = "Asia/Shanghai"
    unpricedNoticeDismissed: str = ""
    guideSeen: bool = False
    guideDismissed: bool = False
    closeToTray: bool = True
    startAtLogin: bool = False
    filters: dict[UsagePage, UsageFilter] = Field(default_factory=dict[UsagePage, UsageFilter])
    historyPrompted: bool = False
    migrationHintDismissed: bool = False


class PreferencePatch(StrictModel):
    theme: Literal["light", "dark", "system"] | None = None
    timezone: Literal["system", "UTC"] | None = None
    showScanCountdown: bool | None = None
    hiddenHarnesses: Annotated[list[HarnessPage], Field(max_length=4)] | None = None
    settingsSection: Literal["display", "sources", "background", "data", "about"] | None = None
    walletDefaultScope: Literal["local", "remote"] | None = None
    collapsed: bool | None = None
    lastPage: Page | None = None
    lastRoute: RoutePath | None = None
    unpricedNoticeDismissed: Annotated[str, StringConstraints(max_length=100)] | None = None
    guideSeen: bool | None = None
    guideDismissed: bool | None = None
    closeToTray: bool | None = None
    startAtLogin: bool | None = None
    filters: dict[UsagePage, UsageFilter] | None = None
    historyPrompted: bool | None = None
    migrationHintDismissed: bool | None = None

    @field_validator("*")
    @classmethod
    def reject_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("偏好字段不能为 null")
        return value


class PreferencesUpdateRequest(WriteRequest):
    patch: PreferencePatch
    expectedRevision: SafeInt


class PreferencesResponse(StrictModel):
    preferences: PreferenceValues
    revision: SafeInt
