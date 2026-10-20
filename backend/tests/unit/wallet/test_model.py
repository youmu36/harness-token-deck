"""Validation and immutability of wallet values."""

from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from htu.domain.common.errors import WALLET_INVALID, DomainError
from htu.domain.common.time import Instant
from htu.domain.wallet.model import Calibration, Totals, UsageRef, Vendor, VendorLink

from .conftest import T0, entry, usage


def test_vendor_name_trimmed_and_archived_name_preserved() -> None:
    assert Vendor("v", "  供应商  ", "active", T0).name == "供应商"
    assert Vendor("v", "a" * 60, "archived", T0).status == "archived"


@pytest.mark.parametrize("name", ["", " ", "x" * 61, "\ud800", None, True, 1])
def test_invalid_vendor_names(name: object) -> None:
    with pytest.raises(DomainError) as failure:
        Vendor("v", cast(str, name), "active", T0)
    assert failure.value.code == WALLET_INVALID
    assert failure.value.field == "name"


@pytest.mark.parametrize("value", ["", " ", "\ud800", None, True, 1])
@pytest.mark.parametrize("field", ["id", "vendor_id", "operation_id", "related_entry_id", "recalc_batch_id"])
def test_entry_identifier_validation(field: str, value: object) -> None:
    if value is None and field in ("related_entry_id", "recalc_batch_id"):
        assert replace(entry("up", "top_up", "1", 1), **{field: value}) == entry("up", "top_up", "1", 1)
        return
    with pytest.raises(DomainError):
        replace(entry("up", "top_up", "1", 1), **{field: value})


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1", None])
def test_sequence_must_be_positive_integer(value: object) -> None:
    with pytest.raises(DomainError):
        VendorLink("g", "v", T0, cast(int, value))
    with pytest.raises(DomainError):
        replace(entry("up", "top_up", "1", 1), seq=cast(int, value))


@pytest.mark.parametrize("key", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, None, True])
def test_usage_reference_hash_is_lowercase_sha256(key: object) -> None:
    with pytest.raises(DomainError):
        UsageRef(usage().harness, cast(str, key))


@pytest.mark.parametrize("value", [None, "2026-09-30T09:00:00Z", True, 0])
def test_entity_time_requires_instant(value: object) -> None:
    with pytest.raises(DomainError):
        Vendor("v", "V", "active", cast(Instant, value))
    with pytest.raises(DomainError):
        VendorLink("g", "v", cast(Instant, value), 1)
    with pytest.raises(DomainError):
        replace(entry("up", "top_up", "1", 1), occurred_at=cast(Instant, value))


@pytest.mark.parametrize("value", [True, 1.0, "1", None])
def test_amount_requires_int_without_boolean(value: object) -> None:
    with pytest.raises(DomainError):
        replace(entry("up", "top_up", "1", 1), amount_pico=cast(int, value))


@pytest.mark.parametrize("kind", ["top_up", "consumption"])
def test_entry_amount_signs(kind: str) -> None:
    base = entry("up", "top_up", "1", 1) if kind == "top_up" else entry("cost", "consumption", "1", 1)
    with pytest.raises(DomainError):
        replace(base, amount_pico=-1)
    if kind == "top_up":
        with pytest.raises(DomainError):
            replace(base, amount_pico=0)
    else:
        assert replace(base, amount_pico=0).amount_pico == 0


@pytest.mark.parametrize("value", ["unknown", "TOP_UP", None, True])
def test_invalid_kinds(value: object) -> None:
    with pytest.raises(DomainError):
        replace(entry("up", "top_up", "1", 1), kind=value)


@pytest.mark.parametrize("field", ["unpriced", "incomplete"])
@pytest.mark.parametrize("value", [1, "true", None])
def test_quality_flags_must_be_bool(field: str, value: object) -> None:
    with pytest.raises(DomainError):
        replace(entry("cost", "consumption", "1", 1), **{field: value})


def test_voucher_specific_required_metadata() -> None:
    with pytest.raises(DomainError):
        replace(entry("cost", "consumption", "0", 1), usage_ref=None)
    with pytest.raises(DomainError):
        replace(entry("up", "top_up", "1", 1), operation_id=None)
    with pytest.raises(DomainError):
        entry("delta", "consumption_delta", "1", 1, usage_ref=usage())
    with pytest.raises(DomainError):
        entry("cal", "adjustment", "1", 1)
    with pytest.raises(DomainError):
        entry("offset", "adjustment", "1", 1, note="late_offset")
    with pytest.raises(DomainError):
        replace(entry("up", "top_up", "1", 1), unpriced=True)
    with pytest.raises(DomainError):
        replace(entry("up", "top_up", "1", 1), related_entry_id="up")


@pytest.mark.parametrize("value", [-1, True, 1.0, None])
@pytest.mark.parametrize("field", ["revision", "watermark_seq", "unpriced_count", "incomplete_count"])
def test_total_metadata_must_be_nonnegative_integer(field: str, value: object) -> None:
    with pytest.raises(DomainError):
        replace(Totals(), **{field: value})


def test_calibration_watermark_precedes_new_entry_seq() -> None:
    assert Calibration("cal", "v", T0, 6, 50, 55, 5).amount_pico == -5
    for watermark in (6, 7, -1):
        with pytest.raises(DomainError):
            Calibration("cal", "v", T0, 6, 50, 55, watermark)


def test_models_are_frozen() -> None:
    vendor = Vendor("v", "V", "active", T0)
    with pytest.raises(FrozenInstanceError):
        vendor.name = "Changed"  # pyright: ignore[reportAttributeAccessIssue] - test frozen runtime guard
    totals = Totals(r=10, c=12, a=-3)
    assert totals.b == -5
    with pytest.raises(FrozenInstanceError):
        totals.r = 0  # pyright: ignore[reportAttributeAccessIssue] - test frozen runtime guard
