from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from htu.domain.common.errors import MODEL_DUPLICATE, MODEL_LIMIT, PRICING_INVALID, DomainError
from htu.domain.common.time import parse_instant
from htu.domain.pricing.model import (
    BillingRule,
    ModelRate,
    PriceGroup,
    PriceRates,
    PriceRevision,
    PriceVersion,
    normalize_models,
)

AT = parse_instant("2026-09-01T00:00:00Z")


def rule() -> BillingRule:
    return BillingRule("peak", PriceRates("01.50", "0", None), (7, 1, 1), (23, 0, 0), 480, 100)


def test_entities_normalize_without_losing_unknown_or_zero_prices() -> None:
    rates = PriceRates("001.500000", "0.000000", None)
    assert rates == PriceRates("1.5", "0", None)
    model = ModelRate(" Model A ", " Display A ", "007", "0", None, rule())
    assert (model.model_id, model.display_name, model.key) == ("Model A", "Display A", "modela")
    assert model.input == "7"
    assert model.billing_rule == BillingRule("peak", PriceRates("1.5", "0"), (1, 7), (0, 23), 480, 100)
    assert PriceGroup("g", " Name ", False, AT).name == "Name"
    assert PriceVersion("v", "g", 7, " Label ", AT).display_name == "Label"
    assert PriceVersion("v", "g", 7, " ", AT).display_name == "v7"


@pytest.mark.parametrize("value", ["", " ", "x" * 161, None, True, "\ud800", "\ufeff"])
def test_invalid_model_names(value: object) -> None:
    with pytest.raises(DomainError):
        ModelRate(cast(str, value), "Display")


@pytest.mark.parametrize("value", ["", " ", "x" * 161, None, True, "\ud800"])
def test_invalid_display_name(value: object) -> None:
    with pytest.raises(DomainError):
        ModelRate("m", cast(str, value))


@pytest.mark.parametrize("value", ["", " ", "x" * 61, None, 1])
def test_invalid_group_names(value: object) -> None:
    with pytest.raises(DomainError):
        PriceGroup("g", cast(str, value), False, AT)


@pytest.mark.parametrize("number", [0, -1, 4294967296, True, 1.0, "1"])
def test_invalid_version_number(number: object) -> None:
    with pytest.raises(DomainError):
        PriceVersion("v", "g", cast(int, number), "", AT)


@pytest.mark.parametrize("number", [1, 4294967295])
def test_version_number_boundaries(number: int) -> None:
    assert PriceVersion("v", "g", number, "x" * 60, AT).number == number


@pytest.mark.parametrize("field", ["input", "cache", "output"])
@pytest.mark.parametrize("value", [True, 1.5, "1e3", "-1", "1.2345678", " 1 "])
def test_invalid_prices(field: str, value: object) -> None:
    with pytest.raises(DomainError):
        PriceRates(**{field: cast(str, value)})
    with pytest.raises(DomainError):
        replace(ModelRate("m", "M"), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "other"),
        ("rates", {}),
        ("weekdays", ()),
        ("weekdays", (0,)),
        ("weekdays", (8,)),
        ("weekdays", (True,)),
        ("hours", ()),
        ("hours", (-1,)),
        ("hours", (24,)),
        ("hours", (1.0,)),
        ("offset_minutes", -841),
        ("offset_minutes", 841),
        ("offset_minutes", True),
        ("threshold_k", 0),
        ("threshold_k", 1000001),
        ("threshold_k", 1.0),
    ],
)
def test_billing_rule_validation(field: str, value: object) -> None:
    with pytest.raises(DomainError) as failure:
        replace(rule(), **{field: value})
    assert failure.value.code == PRICING_INVALID


@pytest.mark.parametrize("mode", ["peak", "tier"])
@pytest.mark.parametrize(("offset", "threshold"), [(-840, 1), (840, 1_000_000)])
def test_rule_boundaries_and_tier_metadata(mode: str, offset: int, threshold: int) -> None:
    validated = replace(rule(), mode=mode, offset_minutes=offset, threshold_k=threshold)
    assert validated.weekdays == (1, 7)
    assert validated.offset_minutes == offset


def test_P10_duplicate_model_keys() -> None:
    with pytest.raises(DomainError) as failure:
        PriceRevision("r", "v", AT, 1, (ModelRate("Model A", "A"), ModelRate("mODELa", "B")))
    assert failure.value.code == MODEL_DUPLICATE


def test_model_limit_and_immutable_snapshot() -> None:
    models = [ModelRate(f"m{i}", f"M{i}") for i in range(500)]
    revision = PriceRevision("r", "v", AT, 1, cast(tuple[ModelRate, ...], models))
    models.append(ModelRate("extra", "Extra"))
    assert len(revision.models) == 500
    with pytest.raises(DomainError) as failure:
        normalize_models(cast(tuple[ModelRate, ...], models))
    assert failure.value.code == MODEL_LIMIT
    with pytest.raises(FrozenInstanceError):
        revision.seq = 2  # pyright: ignore[reportAttributeAccessIssue]
    assert not hasattr(revision, "__dict__")


@pytest.mark.parametrize("seq", [0, -1, True, 1.0])
def test_history_sequence_validation(seq: object) -> None:
    with pytest.raises(DomainError):
        PriceRevision("r", "v", AT, cast(int, seq), ())
