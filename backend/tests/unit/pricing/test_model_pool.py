from dataclasses import FrozenInstanceError
from typing import cast

import pytest
from htd.domain.common.errors import MODEL_NOT_FOUND, MODEL_POOL_LIMIT, DomainError
from htd.domain.pricing.model import PriceRates
from htd.domain.pricing.model_pool import (
    BasePrice,
    ModelPool,
    add_models,
    fill_by_multiplier,
    remove_model,
    set_base_price,
    sync_models,
)


def test_P12_model_pool_deduplicates_preserving_first_spelling() -> None:
    pool = add_models(ModelPool(), ("Model A", " mODELa ", "model\tA", "MODEL\u0085A", "Other"))
    assert pool.models == ("Model A", "Other")
    assert add_models(pool, (" MODELA ",)) is pool


def test_exclusion_blocks_sync_until_explicit_add() -> None:
    pool = add_models(ModelPool(), ("Model A", "Other"))
    pool = set_base_price(pool, "MODELA", PriceRates("1", "0", None))
    removed = remove_model(pool, " model a ")
    assert removed.models == ("Other",)
    assert removed.excluded == ("modela",)
    assert removed.base_for("Model A") is None
    assert sync_models(removed, ["MODELA", "Other"]) is removed
    restored = add_models(removed, ("Model A",))
    assert restored.models == ("Other", "Model A")
    assert restored.excluded == ()
    assert restored.base_for("Model A") is None
    assert remove_model(removed, "Model A") is removed


def test_auto_sync_ignores_invalid_values_and_never_adds_duplicates() -> None:
    original = ModelPool(("Original",))
    updated = sync_models(
        original, [None, True, 12, "", " ", "x" * 161, "\ufeff", "\ud800", " MODEL A ", "modela", "Other"]
    )
    assert updated.models == ("Original", "MODEL A", "Other")
    assert original.models == ("Original",)


def test_manual_limit_is_atomic_and_auto_sync_stops_at_capacity() -> None:
    full = ModelPool(tuple(f"m{i}" for i in range(5000)))
    with pytest.raises(DomainError) as failure:
        add_models(full, ("new",))
    assert failure.value.code == MODEL_POOL_LIMIT
    assert sync_models(full, ["new"]) is full
    assert add_models(full, ("m0",)) is full
    almost = ModelPool(full.models[:-1])
    assert sync_models(almost, ["first", "second"]).models[-1] == "first"
    with pytest.raises(DomainError):
        add_models(almost, ("valid", " "))
    assert len(almost.models) == 4999


def test_base_prices_use_model_key_and_all_null_removes() -> None:
    pool = ModelPool(("Model A", "Other"))
    assert set_base_price(pool, "modela", PriceRates()) is pool
    priced = set_base_price(pool, "modela", PriceRates("01.500", "0", None))
    assert priced.base_for("MODEL A") == PriceRates("1.5", "0", None)
    assert priced.base_prices[0].model_id == "Model A"
    assert set_base_price(priced, "Model A", PriceRates("1.5", "0", None)) is priced
    cleared = set_base_price(priced, "MODEL A", PriceRates())
    assert cleared.base_prices == ()
    assert priced.base_for("missing") is None
    with pytest.raises(DomainError) as failure:
        set_base_price(pool, "missing", PriceRates("1"))
    assert failure.value.code == MODEL_NOT_FOUND


def test_multiplier_fill_preserves_unknown_and_reports_overflow_atomically() -> None:
    rates = PriceRates("1.25", None, "0")
    assert fill_by_multiplier(rates, "1.5") == PriceRates("1.875", None, "0")
    overflowing = PriceRates("1", "2", "999999999")
    with pytest.raises(DomainError):
        fill_by_multiplier(overflowing, "2")
    assert overflowing == PriceRates("1", "2", "999999999")


def test_pool_is_immutable_and_rejects_conflicting_state() -> None:
    names = ["a"]
    pool = ModelPool(cast(tuple[str, ...], names))
    names.append("b")
    assert pool.models == ("a",)
    with pytest.raises(FrozenInstanceError):
        pool.models = ()  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(DomainError):
        ModelPool(("a",), ("A",))
    with pytest.raises(DomainError):
        ModelPool(("a",), base_prices=(BasePrice("missing", PriceRates("1")),))
    with pytest.raises(DomainError):
        ModelPool(("a",), base_prices=(BasePrice("a", PriceRates("1")), BasePrice("A", PriceRates("2"))))
