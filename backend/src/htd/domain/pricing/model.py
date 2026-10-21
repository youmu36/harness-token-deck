"""Validated immutable pricing entities. Time and IDs are supplied by the caller."""

from dataclasses import dataclass
from typing import Literal

from htd.domain.common.errors import MODEL_DUPLICATE, MODEL_LIMIT, PRICING_INVALID, DomainError
from htd.domain.common.money import parse_rate
from htd.domain.common.time import Instant
from htd.domain.pricing.multiplier import parse_multiplier
from htd.domain.usage.model import Harness
from htd.domain.usage.model_key import model_key


def text_field(value: str, maximum: int, field: str, *, empty: bool = False) -> str:
    if type(value) is not str or not (0 if empty else 1) <= len(value.strip()) <= maximum:
        raise DomainError(PRICING_INVALID, f"文本长度须在 {0 if empty else 1} 至 {maximum} 字符之间", field)
    result = value.strip()
    try:
        result.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DomainError(PRICING_INVALID, "文本包含无效 Unicode 字符", field) from error
    return result


def model_name(value: str) -> str:
    result = text_field(value, 160, "model_id")
    model_key(result)
    return result


def _identifier(value: str, field: str) -> None:
    if type(value) is not str or not value:
        raise DomainError(PRICING_INVALID, "标识不能为空", field)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DomainError(PRICING_INVALID, "标识包含无效 Unicode 字符", field) from error


def _instant(value: Instant) -> None:
    if type(value) is not Instant:
        raise DomainError(PRICING_INVALID, "时间须为已规范化的 Instant", "effective_at")


def _sequence(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise DomainError(PRICING_INVALID, "历史序号须为正整数", "seq")


def _normalize_rates(value: "PriceRates | ModelRate") -> None:
    for name in ("input", "cache", "output", "cache_write"):
        rate = getattr(value, name)
        if rate is not None:
            object.__setattr__(value, name, parse_rate(rate).normalized)


@dataclass(frozen=True, slots=True)
class PriceRates:
    input: str | None = None
    cache: str | None = None
    output: str | None = None
    cache_write: str | None = None

    def __post_init__(self) -> None:
        _normalize_rates(self)


@dataclass(frozen=True, slots=True)
class BillingRule:
    mode: Literal["peak", "tier"]
    rates: PriceRates
    weekdays: tuple[int, ...]
    hours: tuple[int, ...]
    offset_minutes: int
    threshold_k: int

    def __post_init__(self) -> None:
        if self.mode not in ("peak", "tier") or type(self.rates) is not PriceRates:
            raise DomainError(PRICING_INVALID, "计费规则模式或价格无效", "billing_rule")
        for name, upper, lower in (("weekdays", 7, 1), ("hours", 23, 0)):
            values = getattr(self, name)
            if (
                type(values) not in (tuple, list)
                or not values
                or any(type(value) is not int or not lower <= value <= upper for value in values)
            ):
                raise DomainError(PRICING_INVALID, "计费规则时间集合无效", name)
            object.__setattr__(self, name, tuple(sorted(set(values))))
        if type(self.offset_minutes) is not int or not -840 <= self.offset_minutes <= 840:
            raise DomainError(PRICING_INVALID, "时区偏移须在 -840 至 840 分钟之间", "offset_minutes")
        if type(self.threshold_k) is not int or not 1 <= self.threshold_k <= 1_000_000:
            raise DomainError(PRICING_INVALID, "阶梯阈值须在 1 至 1000000 之间", "threshold_k")


@dataclass(frozen=True, slots=True)
class ModelRate:
    model_id: str
    display_name: str
    input: str | None = None
    cache: str | None = None
    output: str | None = None
    billing_rule: BillingRule | None = None
    cache_write: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", model_name(self.model_id))
        object.__setattr__(self, "display_name", text_field(self.display_name, 160, "display_name"))
        _normalize_rates(self)
        if self.billing_rule is not None and type(self.billing_rule) is not BillingRule:
            raise DomainError(PRICING_INVALID, "计费规则无效", "billing_rule")

    @property
    def key(self) -> str:
        return model_key(self.model_id)


def normalize_models(models: tuple[ModelRate, ...]) -> tuple[ModelRate, ...]:
    if type(models) not in (tuple, list) or any(type(model) is not ModelRate for model in models):
        raise DomainError(PRICING_INVALID, "价格快照须为模型价格列表", "models")
    if len(models) > 500:
        raise DomainError(MODEL_LIMIT, "每个价格修订最多包含 500 个模型", "models")
    if len({model.key for model in models}) != len(models):
        raise DomainError(MODEL_DUPLICATE, "模型名称规范化后重复", "models")
    return tuple(sorted(models, key=lambda model: model.key))


@dataclass(frozen=True, slots=True)
class PriceGroup:
    id: str
    name: str
    disabled: bool
    created_at: Instant
    deleted_at: Instant | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, "id")
        object.__setattr__(self, "name", text_field(self.name, 60, "name"))
        if type(self.disabled) is not bool:
            raise DomainError(PRICING_INVALID, "停用标志须为布尔值", "disabled")
        _instant(self.created_at)
        if self.deleted_at is not None:
            _instant(self.deleted_at)


@dataclass(frozen=True, slots=True)
class PriceVersion:
    id: str
    group_id: str
    number: int
    label: str
    created_at: Instant
    disabled: bool = False
    deleted_at: Instant | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, "id")
        _identifier(self.group_id, "group_id")
        if type(self.number) is not int or not 1 <= self.number <= 4294967295:
            raise DomainError(PRICING_INVALID, "版本号须为 1 至 4294967295 的整数", "number")
        object.__setattr__(self, "label", text_field(self.label, 60, "label", empty=True))
        _instant(self.created_at)
        if type(self.disabled) is not bool:
            raise DomainError(PRICING_INVALID, "停用标志须为布尔值", "disabled")
        if self.deleted_at is not None:
            _instant(self.deleted_at)

    @property
    def display_name(self) -> str:
        return self.label or f"v{self.number}"


@dataclass(frozen=True, slots=True)
class PriceRevision:
    id: str
    version_id: str
    effective_at: Instant
    seq: int
    models: tuple[ModelRate, ...]

    def __post_init__(self) -> None:
        _identifier(self.id, "id")
        _identifier(self.version_id, "version_id")
        _instant(self.effective_at)
        _sequence(self.seq)
        object.__setattr__(self, "models", normalize_models(self.models))


@dataclass(frozen=True, slots=True)
class VersionActivation:
    group_id: str
    version_id: str
    effective_at: Instant
    seq: int

    def __post_init__(self) -> None:
        _identifier(self.group_id, "group_id")
        _identifier(self.version_id, "version_id")
        _instant(self.effective_at)
        _sequence(self.seq)


@dataclass(frozen=True, slots=True)
class HarnessSelection:
    harness: Harness
    group_id: str
    effective_at: Instant
    seq: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "harness", Harness(self.harness))
        _identifier(self.group_id, "group_id")
        _instant(self.effective_at)
        _sequence(self.seq)


@dataclass(frozen=True, slots=True)
class VersionDisplay:
    version_id: str
    label: str

    def __post_init__(self) -> None:
        _identifier(self.version_id, "version_id")
        object.__setattr__(self, "label", text_field(self.label, 60, "label", empty=True))


@dataclass(frozen=True, slots=True)
class EditorHints:
    version_id: str
    model_id: str
    multiplier: str

    def __post_init__(self) -> None:
        _identifier(self.version_id, "version_id")
        object.__setattr__(self, "model_id", model_name(self.model_id))
        object.__setattr__(self, "multiplier", parse_multiplier(self.multiplier).normalized)


@dataclass(frozen=True, slots=True)
class SwitchPlan:
    operation_id: str
    harness: Harness
    group_id: str
    version_id: str
    expected_revision: int
    effective_at: Instant
    selections: tuple[HarnessSelection, ...] = ()
    activations: tuple[VersionActivation, ...] = ()
    affected_harnesses: tuple[Harness, ...] = ()

    def __post_init__(self) -> None:
        for name in ("operation_id", "group_id", "version_id"):
            _identifier(getattr(self, name), name)
        object.__setattr__(self, "harness", Harness(self.harness))
        _instant(self.effective_at)
        if type(self.expected_revision) is not int or self.expected_revision < 0:
            raise DomainError(PRICING_INVALID, "目录修订须为非负整数", "expected_revision")
        for name, kind in (("selections", HarnessSelection), ("activations", VersionActivation)):
            values = getattr(self, name)
            if (
                type(values) not in (tuple, list)
                or len(values) > 1
                or any(type(value) is not kind for value in values)
            ):
                raise DomainError(PRICING_INVALID, "组合切换每类最多追加一条记录", name)
            object.__setattr__(self, name, tuple(values))
        if type(self.affected_harnesses) not in (tuple, list):
            raise DomainError(PRICING_INVALID, "受影响来源必须为列表", "affected_harnesses")
        affected = {Harness(harness) for harness in self.affected_harnesses}
        object.__setattr__(
            self, "affected_harnesses", tuple(harness for harness in Harness if harness in affected)
        )

    @property
    def changed(self) -> bool:
        return bool(self.selections or self.activations)
