"""Pure model-pool, exclusion-list, base-price, and multiplier-fill operations."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import cast

from htu.domain.common.errors import MODEL_NOT_FOUND, MODEL_POOL_LIMIT, PRICING_INVALID, DomainError
from htu.domain.pricing.model import PriceRates, model_name
from htu.domain.pricing.multiplier import multiply_rate, parse_multiplier
from htu.domain.usage.model_key import model_key


@dataclass(frozen=True, slots=True)
class BasePrice:
    model_id: str
    rates: PriceRates

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", model_name(self.model_id))
        if type(self.rates) is not PriceRates:
            raise DomainError(PRICING_INVALID, "基准价必须包含已规范化的三项价格", "rates")


def _names(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) not in (tuple, list):
        raise DomainError(PRICING_INVALID, "模型池必须为名称列表", "models")
    names: dict[str, str] = {}
    for value in values:
        name = model_name(value)
        names.setdefault(model_key(name), name)
    if len(names) > 5000:
        raise DomainError(MODEL_POOL_LIMIT, "模型池最多包含 5000 个模型", "models")
    return tuple(names.values())


@dataclass(frozen=True, slots=True)
class ModelPool:
    models: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    base_prices: tuple[BasePrice, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "models", _names(self.models))
        keys = {model_key(name) for name in self.models}
        if type(self.excluded) not in (tuple, list):
            raise DomainError(PRICING_INVALID, "排除名单必须为模型键列表", "excluded")
        excluded = tuple(sorted({model_key(name) for name in self.excluded}))
        if keys.intersection(excluded):
            raise DomainError(PRICING_INVALID, "池内模型不能同时在排除名单中", "excluded")
        object.__setattr__(self, "excluded", excluded)
        if type(self.base_prices) not in (tuple, list) or any(
            type(base) is not BasePrice for base in self.base_prices
        ):
            raise DomainError(PRICING_INVALID, "基准价列表无效", "base_prices")
        base_keys = [model_key(base.model_id) for base in self.base_prices]
        if len(set(base_keys)) != len(base_keys) or not set(base_keys) <= keys:
            raise DomainError(PRICING_INVALID, "基准价模型重复或不在模型池中", "base_prices")
        object.__setattr__(
            self,
            "base_prices",
            tuple(
                sorted(
                    (base for base in self.base_prices if base.rates != PriceRates()),
                    key=lambda base: model_key(base.model_id),
                )
            ),
        )

    def base_for(self, name: str) -> PriceRates | None:
        key = model_key(name)
        return next((base.rates for base in self.base_prices if model_key(base.model_id) == key), None)


def add_models(pool: ModelPool, names: tuple[str, ...]) -> ModelPool:
    added = _names(names)
    keys = {model_key(name) for name in added}
    updated = replace(
        pool,
        models=_names((*pool.models, *added)),
        excluded=tuple(key for key in pool.excluded if key not in keys),
    )
    return pool if updated == pool else updated


def remove_model(pool: ModelPool, name: str) -> ModelPool:
    key = model_key(model_name(name))
    updated = replace(
        pool,
        models=tuple(item for item in pool.models if model_key(item) != key),
        excluded=(*pool.excluded, key),
        base_prices=tuple(base for base in pool.base_prices if model_key(base.model_id) != key),
    )
    return pool if updated == pool else updated


def sync_models(pool: ModelPool, names: Iterable[object]) -> ModelPool:
    if isinstance(names, (str, bytes)):
        raise DomainError(PRICING_INVALID, "自动同步需要模型名称集合", "models")
    models = list(pool.models)
    seen = {model_key(name) for name in models}
    excluded = set(pool.excluded)
    for value in names:
        try:
            name = model_name(cast(str, value))
        except DomainError:
            continue
        key = model_key(name)
        if key not in seen and key not in excluded and len(models) < 5000:
            models.append(name)
            seen.add(key)
    return pool if tuple(models) == pool.models else replace(pool, models=tuple(models))


def set_base_price(pool: ModelPool, name: str, rates: PriceRates) -> ModelPool:
    key = model_key(model_name(name))
    original = next((item for item in pool.models if model_key(item) == key), None)
    if original is None:
        raise DomainError(MODEL_NOT_FOUND, "模型不在模型池中", "model_id")
    base = BasePrice(original, rates)
    others = tuple(item for item in pool.base_prices if model_key(item.model_id) != key)
    updated = replace(pool, base_prices=others if rates == PriceRates() else (*others, base))
    return pool if updated == pool else updated


def fill_by_multiplier(rates: PriceRates, multiplier: str) -> PriceRates:
    factor = parse_multiplier(multiplier)
    return PriceRates(
        *(multiply_rate(rate, factor) for rate in (rates.input, rates.cache, rates.output, rates.cache_write))
    )
