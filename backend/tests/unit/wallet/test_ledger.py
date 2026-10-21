"""Wallet ordering, late offsets, quality snapshots, and exact totals."""

from dataclasses import replace

import pytest
from htd.domain.common.errors import (
    OPERATION_ID_REUSED,
    WALLET_INVALID,
    WALLET_REVISION_CONFLICT,
    DomainError,
)
from htd.domain.common.money import PICO_PER_USD
from htd.domain.wallet.ledger import (
    apply,
    attribute_consumption,
    late_offsets,
    plan_calibration,
    plan_initial_balance,
    top_up,
)
from htd.domain.wallet.model import Vendor, VendorLink, WalletEntry
from hypothesis import given
from hypothesis import strategies as st

from .conftest import AFTER, BEFORE, T0, T1, T2, calibrated, entry, usage


@pytest.mark.parametrize("target", ["0", "-0", "50", "-12.345678"])
def test_calibration_targets_keep_recharge_and_consumption(target: str) -> None:
    entries = (entry("up", "top_up", "100", 1), entry("cost", "consumption", "30", 2))
    adjustment, audit = calibrated(entries, target, 3)
    totals = apply((*entries, adjustment))
    assert totals.r == 100 * PICO_PER_USD
    assert totals.c == 30 * PICO_PER_USD
    assert totals.b == audit.target_pico
    assert totals.last_calibrated_at == T1


def test_zero_difference_still_records_audited_calibration() -> None:
    entries = (entry("up", "top_up", "50", 1),)
    adjustment, audit = calibrated(entries, "50", 2)
    assert adjustment.amount_pico == 0
    assert audit.watermark_seq == 1
    totals = apply((*entries, adjustment), revision=12)
    assert totals.b == 50 * PICO_PER_USD
    assert totals.last_calibrated_at == T1
    assert totals.revision == 12


def test_calibration_uses_global_watermark_not_wallet_entry_count() -> None:
    totals = apply((entry("up", "top_up", "5", 2),), revision=10, watermark_seq=100)
    plan = plan_calibration(totals, 0, 10)
    adjustment, audit = plan.records(entry_id="cal", vendor_id="v", at=T1, seq=101, operation_id="cal")
    assert audit.watermark_seq == 100
    assert adjustment.seq == 101
    with pytest.raises(DomainError):
        plan.records(entry_id="bad", vendor_id="v", at=T1, seq=100, operation_id="bad")


@pytest.mark.parametrize("revision", [-1, True, 1, 1.0])
def test_bad_expected_revision_is_rejected_before_planning(revision: object) -> None:
    from typing import cast

    with pytest.raises(DomainError) as failure:
        plan_calibration(apply(()), 0, cast(int, revision))
    assert failure.value.code == WALLET_REVISION_CONFLICT


@pytest.mark.parametrize("value", [" ", "1e3", "1.0000000", "+1", "1000000000"])
def test_invalid_initial_amounts_are_not_silently_empty(value: str) -> None:
    with pytest.raises(DomainError):
        plan_initial_balance(value)


@pytest.mark.parametrize("at", [BEFORE, T1, AFTER])
@pytest.mark.parametrize("seq", [8, 9, 10, 11])
def test_late_offset_checks_both_completion_time_and_insertion_sequence(at: object, seq: int) -> None:
    from typing import cast

    from htd.domain.common.time import Instant

    _, audit = calibrated((entry("up", "top_up", "50", 8),), "50", 10)
    cost = entry("cost", "consumption", "3", seq, at=cast(Instant, at))
    plans = late_offsets((audit,), cost)
    assert len(plans) == int(seq > 10 and at != AFTER)


def test_multiple_calibrations_offset_once_and_only_for_matching_vendor() -> None:
    first, first_audit = calibrated((), "50", 1)
    second, second_audit = calibrated((first,), "60", 2, at=T2, entry_id="cal2")
    cost = entry("cost", "consumption", "3", 3, at=T1)
    plans = late_offsets((first_audit, second_audit), cost)
    assert len(plans) == 1
    offset = plans[0].record(entry_id="offset", at=T2, seq=4)
    totals = apply((second, offset, first, cost))
    assert totals.b == 60 * PICO_PER_USD
    assert totals.c == 3 * PICO_PER_USD
    assert late_offsets((replace(first_audit, vendor_id="other"),), cost) == ()
    assert late_offsets((first_audit,), cost, migration=True) == ()


@pytest.mark.parametrize("amount", ["-2", "0", "4"])
def test_late_recalculation_delta_offsets_with_same_signed_amount(amount: str) -> None:
    original = entry("original", "consumption", "10", 1, at=T0)
    adjustment, audit = calibrated((original,), "50", 2)
    delta = entry(
        "delta",
        "consumption_delta",
        amount,
        3,
        at=T0,
        usage_ref=original.usage_ref,
        related_entry_id="original",
        recalc_batch_id="batch",
    )
    plans = late_offsets((audit,), delta)
    assert len(plans) == 1
    offset = plans[0].record(entry_id="offset", at=T2, seq=4)
    assert offset.amount_pico == delta.amount_pico
    totals = apply((original, adjustment, delta, offset))
    assert totals.b == 50 * PICO_PER_USD
    assert totals.c == original.amount_pico + delta.amount_pico


def test_migration_and_non_consumption_never_offset() -> None:
    adjustment, audit = calibrated((), "50", 1)
    cost = entry("cost", "consumption", "3", 2, at=T0)
    assert late_offsets((audit,), cost, migration=True) == ()
    assert late_offsets((audit,), adjustment) == ()
    assert late_offsets((audit,), entry("up", "top_up", "3", 2)) == ()


def test_zero_consumption_remains_traceable_and_offsets_once() -> None:
    adjustment, audit = calibrated((), "50", 1)
    cost = entry("zero", "consumption", "0", 2, at=T0, unpriced=True)
    (plan,) = late_offsets((audit,), cost)
    offset = plan.record(entry_id="offset", at=T2, seq=3)
    assert offset.amount_pico == 0
    totals = apply((adjustment, cost, offset))
    assert totals.b == 50 * PICO_PER_USD
    assert totals.unpriced_count == 1


def test_quality_counts_requests_not_deltas_or_adjustments() -> None:
    first = entry("a", "consumption", "0", 1, unpriced=True, incomplete=True)
    second = entry("b", "consumption", "3", 2, incomplete=True)
    totals = apply((first, second))
    assert (totals.unpriced_count, totals.incomplete_count) == (1, 2)
    assert totals.coverage_incomplete
    delta = entry(
        "delta",
        "consumption_delta",
        "2",
        3,
        at=first.occurred_at,
        usage_ref=first.usage_ref,
        related_entry_id="a",
        recalc_batch_id="batch",
    )
    repriced = apply((delta, second, first))
    assert (repriced.unpriced_count, repriced.incomplete_count) == (0, 1)
    assert repriced.coverage_incomplete
    resolved = replace(delta, usage_ref=second.usage_ref, related_entry_id="b", id="delta2", seq=4)
    complete = apply((first, second, delta, resolved))
    assert not complete.coverage_incomplete


def test_same_dedupe_key_in_different_harnesses_is_distinct() -> None:
    from htd.domain.usage.model import Harness

    first = entry("a", "consumption", "1", 1, usage_ref=usage("x", Harness.CODEX), unpriced=True)
    second = entry("b", "consumption", "1", 2, usage_ref=usage("x", Harness.ZCODE), incomplete=True)
    totals = apply((first, second))
    assert totals.unpriced_count == totals.incomplete_count == 1


def test_legacy_calibration_time_and_new_calibrations_take_latest() -> None:
    adjustment, _ = calibrated((), "50", 1)
    assert apply((adjustment,), legacy_calibrated_at=T2).last_calibrated_at == T2
    assert apply((adjustment,), legacy_calibrated_at=T0).last_calibrated_at == T1
    assert apply((), legacy_calibrated_at=T0).last_calibrated_at == T0


@pytest.mark.parametrize("bad_history", ["id", "seq", "usage", "vendor", "delta", "offset", "double_offset"])
def test_inconsistent_voucher_histories_are_rejected(bad_history: str) -> None:
    first = entry("cost", "consumption", "3", 1)
    if bad_history == "id":
        entries = (first, replace(first, seq=2))
    elif bad_history == "seq":
        entries = (first, entry("cost2", "consumption", "3", 1))
    elif bad_history == "usage":
        entries = (first, replace(first, id="cost2", seq=2))
    elif bad_history == "vendor":
        entries = (first, entry("cost2", "consumption", "3", 2, vendor_id="other"))
    elif bad_history == "delta":
        entries = (
            first,
            entry(
                "delta",
                "consumption_delta",
                "1",
                2,
                usage_ref=usage("other"),
                related_entry_id="cost",
                recalc_batch_id="b",
            ),
        )
    else:
        offset = entry(
            "offset",
            "adjustment",
            "3" if bad_history == "double_offset" else "4",
            2,
            related_entry_id="cost",
            note="late_offset",
        )
        entries = (first, offset)
        if bad_history == "double_offset":
            entries = (*entries, replace(offset, id="offset2", seq=3))
    with pytest.raises(DomainError) as failure:
        apply(entries)
    assert failure.value.code == WALLET_INVALID


def test_sequence_watermark_may_not_discard_existing_history() -> None:
    with pytest.raises(DomainError):
        apply((entry("up", "top_up", "1", 10),), watermark_seq=9)


def test_association_boundaries_unlink_switch_same_instant_and_unrelated_groups() -> None:
    links = (
        VendorLink("g", "v1", T0, 1),
        VendorLink("g", "v2", T1, 2),
        VendorLink("other", "v3", T2, 3),
        VendorLink("g", None, T2, 4),
    )
    assert attribute_consumption(tuple(reversed(links)), "g", BEFORE) == "v1"
    assert attribute_consumption(links, "g", T1) == "v2"
    assert attribute_consumption(links, "g", T2) is None
    assert attribute_consumption(links, None, T2) is None
    assert attribute_consumption(links, "missing", T2) is None
    assert attribute_consumption((*links, VendorLink("g", "v4", T1, 5)), "g", T1) == "v4"


def test_reassociation_does_not_move_stored_consumption() -> None:
    links = (VendorLink("g", "v1", T0, 1),)
    cost = entry("cost", "consumption", "3", 1, vendor_id="v1", at=T1)
    totals = apply((cost,))
    links = (*links, VendorLink("g", "v2", T2, 2))
    assert cost.vendor_id == "v1"
    assert apply((cost,)) == totals
    assert attribute_consumption(links, "g", T1) == "v1"
    assert attribute_consumption(links, "g", T2) == "v2"


@pytest.mark.parametrize("amount", ["0", "-0", "-1", "1e3", "1.1234567"])
def test_top_up_requires_strictly_positive_valid_amount(amount: str) -> None:
    with pytest.raises(DomainError):
        top_up((), Vendor("v", "V", "active", T0), amount, entry_id="up", at=T0, seq=1, operation_id="op")


def test_top_up_operation_id_reuse_with_different_target_fails_without_change() -> None:
    vendor = Vendor("v", "V", "active", T0)
    original = top_up((), vendor, "1", entry_id="up", at=T0, seq=1, operation_id="op")
    for amount, target in (("2", vendor), ("1", replace(vendor, id="other"))):
        with pytest.raises(DomainError) as failure:
            top_up(original, target, amount, entry_id="new", at=T1, seq=2, operation_id="op")
        assert failure.value.code == OPERATION_ID_REUSED
    assert apply(original).r == PICO_PER_USD


@given(
    st.integers(1, 2**200),
    st.integers(0, 2**200),
    st.integers(-(2**200), 2**200),
    st.integers(-(2**200), 2**200),
    st.integers(1, 2**200),
    st.integers(0, 2**200),
)
def test_calibration_preserves_exact_target_and_later_activity_regardless_of_history_order(
    recharge: int, consumption: int, migration: int, target: int, later_recharge: int, later_cost: int
) -> None:
    original = (
        WalletEntry("up", "v", "top_up", recharge, T0, 1, "up"),
        WalletEntry("cost", "v", "consumption", consumption, T0, 2, usage_ref=usage("old")),
        WalletEntry("migration", "v", "adjustment", migration, T0, 3, note="migration"),
    )
    plan = plan_calibration(apply(original), target, 0)
    adjustment, _ = plan.records(entry_id="cal", vendor_id="v", at=T1, seq=4, operation_id="cal")
    final = (
        *original,
        adjustment,
        WalletEntry("up2", "v", "top_up", later_recharge, T2, 5, "up2"),
        WalletEntry("cost2", "v", "consumption", later_cost, T2, 6, usage_ref=usage("new")),
    )
    totals = apply(final)
    assert totals.b == target + later_recharge - later_cost
    assert totals.r == recharge + later_recharge
    assert totals.c == consumption + later_cost
    assert type(totals.b) is int
    assert apply(tuple(reversed(final))) == totals
