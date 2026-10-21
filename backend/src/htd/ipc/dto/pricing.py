"""Strict pricing, model-pool and editor contracts."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from htd.domain.pricing.model import BillingRule, ModelRate, PriceRates
from htd.ipc.dto.base import InstantStr, PicoStr, RateStr, SafeInt, StrictModel, WriteRequest
from htd.ipc.dto.events import HarnessName

type Identifier = Annotated[str, StringConstraints(min_length=1, max_length=160)]
type Name = Annotated[str, StringConstraints(min_length=1, max_length=60)]
type MultiplierStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,9}(?:\.[0-9]{1,3})?$")]


class PricingWrite(WriteRequest):
    expectedRevision: SafeInt


class Rates(StrictModel):
    input: RateStr | None = None
    cache: RateStr | None = None
    output: RateStr | None = None
    cacheWrite: RateStr | None = None

    def domain(self) -> PriceRates:
        return PriceRates(self.input, self.cache, self.output, self.cacheWrite)


class Rule(StrictModel):
    mode: Literal["peak", "tier"]
    rates: Rates
    weekdays: Annotated[list[Annotated[int, Field(ge=1, le=7)]], Field(min_length=1, max_length=7)]
    hours: Annotated[list[Annotated[int, Field(ge=0, le=23)]], Field(min_length=1, max_length=24)]
    offsetMinutes: Annotated[int, Field(ge=-840, le=840)]
    thresholdK: Annotated[int, Field(ge=1, le=1_000_000)]

    def domain(self) -> BillingRule:
        return BillingRule(
            self.mode,
            self.rates.domain(),
            tuple(self.weekdays),
            tuple(self.hours),
            self.offsetMinutes,
            self.thresholdK,
        )


class ModelPrice(Rates):
    modelId: Identifier
    billingRule: Rule | None = None

    def price(self) -> ModelRate:
        return ModelRate(
            self.modelId,
            self.modelId,
            self.input,
            self.cache,
            self.output,
            self.billingRule.domain() if self.billingRule else None,
            self.cacheWrite,
        )


class Hint(StrictModel):
    modelId: Identifier
    multiplier: MultiplierStr


class Version(StrictModel):
    id: Identifier
    number: Annotated[int, Field(ge=1, le=4294967295)]
    label: Annotated[str, StringConstraints(max_length=60)]
    displayName: str
    createdAt: InstantStr
    current: bool
    disabled: bool = False
    revisionId: Identifier
    revisionCount: SafeInt
    models: list[ModelPrice]
    hints: list[Hint]


class Group(StrictModel):
    id: Identifier
    name: Name
    historical: bool = False
    disabled: bool
    createdAt: InstantStr
    harnesses: list[HarnessName]
    canDelete: bool
    deleteReason: Literal["GROUP_REFERENCED"] | None
    vendorId: Identifier | None
    versions: list[Version]
    nextVersionNumber: SafeInt


class Selection(StrictModel):
    groupId: Identifier | None
    versionId: Identifier | None


class CatalogResponse(StrictModel):
    groups: list[Group]
    current: dict[HarnessName, Selection]
    revision: SafeInt


class CreateGroupRequest(PricingWrite):
    name: Name
    copyFromGroupId: Identifier | None = None


class GroupRequest(PricingWrite):
    groupId: Identifier


class ActivateVersionRequest(GroupRequest):
    versionId: Identifier


class RenameGroupRequest(GroupRequest):
    name: Name


class SetGroupDisabledRequest(GroupRequest):
    disabled: bool


class AddVersionRequest(GroupRequest):
    number: Annotated[int, Field(ge=1, le=4294967295)]
    label: Annotated[str, StringConstraints(max_length=60)] = ""
    copyFromVersionId: Identifier | None = None


class SetVersionLabelRequest(PricingWrite):
    versionId: Identifier
    label: Annotated[str, StringConstraints(max_length=60)]


class VersionRequest(PricingWrite):
    versionId: Identifier


class SetVersionDisabledRequest(VersionRequest):
    disabled: bool


class SaveVersionPricesRequest(PricingWrite):
    versionId: Identifier
    models: Annotated[list[ModelPrice], Field(max_length=500)]
    hints: Annotated[list[Hint], Field(max_length=500)] = Field(default_factory=list[Hint])


class PreviewSwitchRequest(StrictModel):
    harness: HarnessName
    groupId: Identifier
    versionId: Identifier


class SwitchCurrentRequest(PricingWrite):
    harness: HarnessName
    groupId: Identifier
    versionId: Identifier


class ChangeResponse(StrictModel):
    changed: bool
    revision: SafeInt
    groupId: Identifier | None = None
    versionId: Identifier | None = None
    affectedHarnesses: list[HarnessName] = Field(default_factory=list[HarnessName])


class BaseRates(StrictModel):
    modelId: Identifier
    rates: Rates


class PoolResponse(StrictModel):
    models: list[BaseRates]
    excluded: list[Identifier]
    revision: SafeInt


class AddModelsRequest(PricingWrite):
    modelIds: Annotated[list[Identifier], Field(min_length=1, max_length=5000)]


class RemoveModelRequest(PricingWrite):
    modelId: Identifier


class SaveBaseRatesRequest(PricingWrite):
    models: Annotated[list[BaseRates], Field(max_length=5000)]


class FillByMultiplierRequest(StrictModel):
    rates: Rates
    multiplier: MultiplierStr


class ScanModelsRequest(WriteRequest):
    harness: Literal["all", "claude", "codex", "zcode", "dsh"] = "all"


class UnpricedItem(StrictModel):
    id: str = ""
    ignored: bool = False
    price: ModelPrice | None = None
    multiplier: str = ""
    harness: HarnessName
    modelId: str | None
    groupId: str | None
    versionId: str | None
    groupName: str
    versionName: str
    recordCount: SafeInt
    readyCount: SafeInt
    reasons: list[str]
    firstAt: InstantStr
    lastAt: InstantStr
    editable: bool


class ArchivedUnpriced(StrictModel):
    harness: HarnessName
    modelId: str | None
    buckets: SafeInt
    firstAt: InstantStr
    lastAt: InstantStr


class UnpricedResponse(StrictModel):
    limited: bool = False
    ignoredCount: SafeInt = 0
    usageRevision: SafeInt = 0
    items: list[UnpricedItem]
    archived: list[ArchivedUnpriced]
    recordCount: SafeInt
    readyCount: SafeInt
    pricingRevision: SafeInt
    start: InstantStr
    end: InstantStr


class RecalculateUnpricedRequest(WriteRequest):
    expectedPricingRevision: SafeInt


class RecalculateUnpricedResponse(StrictModel):
    complete: bool = True
    errorCode: str | None = None
    recalculated: SafeInt
    remaining: SafeInt
    costDeltaPico: PicoStr
    revision: SafeInt


class ResolveUnpricedRequest(WriteRequest):
    itemIds: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]],
        Field(min_length=1, max_length=500),
    ]
    action: Literal["ignore", "restore", "assign", "price"]
    expectedPricingRevision: SafeInt
    expectedUsageRevision: SafeInt
    recordLimit: Annotated[int, Field(ge=1, le=2**53 - 1)] | None = None
    groupId: Identifier | None = None
    versionId: Identifier | None = None
    model: ModelPrice | None = None
    multiplier: MultiplierStr | Literal[""] = ""


class ResolveUnpricedResponse(StrictModel):
    changed: SafeInt
    revision: SafeInt


class UnpricedNotice(StrictModel):
    count: SafeInt
    harnesses: list[str]
    signature: str
