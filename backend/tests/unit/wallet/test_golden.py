"""W-series wallet accounting, revision guards, and display vectors."""

import pytest
from htu.domain.common.errors import WALLET_REVISION_CONFLICT, DomainError
from htu.domain.common.money import PICO_PER_USD, format_pico, parse_usd_amount
from htu.domain.wallet.ledger import (
    apply,
    attribute_consumption,
    late_offsets,
    plan_calibration,
    plan_initial_balance,
    top_up,
)
from htu.domain.wallet.model import Vendor, VendorLink

from .conftest import AFTER, BEFORE, T0, T1, T2, calibrated, entry


@pytest.mark.parametrize(
    ("stage", "expected"),
    [(1, (100, 30, 0, 70)), (2, (100, 30, -5, 65)), (3, (120, 32, -5, 83))],
    ids=["W-01", "W-02", "W-03"],
)
def test_wallet_golden_accounting(stage: int, expected: tuple[int, int, int, int]) -> None:
    entries = (entry("up", "top_up", "100", 1), entry("cost", "consumption", "30", 2))
    if stage >= 2:
        adjustment, audit = calibrated(entries, "65", 3)
        assert audit.observed_balance_pico == 70 * PICO_PER_USD
        assert audit.watermark_seq == 2
        entries = (*entries, adjustment)
    if stage >= 3:
        entries = (
            *entries,
            entry("up2", "top_up", "20", 4, at=T2),
            entry("cost2", "consumption", "2", 5, at=T2),
        )
    totals = apply(entries)
    assert (totals.r, totals.c, totals.a, totals.b) == tuple(value * PICO_PER_USD for value in expected)


def test_W04_top_up_retry_is_idempotent() -> None:
    vendor = Vendor("v", "供应商", "active", T0)
    entries = (entry("up", "top_up", "100", 1), entry("cost", "consumption", "30", 2))
    adjustment, _ = calibrated(entries, "65", 3)
    entries = (*entries, adjustment)
    first = top_up(entries, vendor, "20", entry_id="new", at=T2, seq=4, operation_id="retry")
    first = (*first, entry("cost2", "consumption", "2", 5, at=T2))
    repeated = top_up(first, vendor, "020.000000", entry_id="unused", at=T2, seq=6, operation_id="retry")
    assert repeated is first
    totals = apply(repeated)
    assert (totals.r, totals.c, totals.a, totals.b) == (
        120 * PICO_PER_USD,
        32 * PICO_PER_USD,
        -5 * PICO_PER_USD,
        83 * PICO_PER_USD,
    )


def test_W05_negative_initial_balance_is_a_calibration() -> None:
    plan = plan_initial_balance("-0.5")
    assert plan is not None
    adjustment, audit = plan.records(entry_id="cal", vendor_id="v", at=T0, seq=1, operation_id="create")
    assert audit.target_pico == -500_000_000_000
    assert audit.observed_balance_pico == 0
    assert audit.watermark_seq == 0
    totals = apply((adjustment,))
    assert (totals.r, totals.c, totals.a, totals.b) == (0, 0, -500_000_000_000, -500_000_000_000)
    assert totals.last_calibrated_at == T0


@pytest.mark.parametrize("initial", [None, ""])
def test_W06_empty_initial_balance_has_no_voucher(initial: str | None) -> None:
    assert plan_initial_balance(initial) is None
    totals = apply(())
    assert (totals.r, totals.c, totals.a, totals.b) == (0, 0, 0, 0)
    assert totals.last_calibrated_at is None


def test_W07_late_consumption_preserves_confirmed_balance() -> None:
    adjustment, audit = calibrated((), "50", 1)
    consumption = entry("cost", "consumption", "3", 2, at=BEFORE)
    plans = late_offsets((audit,), consumption)
    assert len(plans) == 1
    offset = plans[0].record(entry_id="offset", at=T2, seq=3)
    assert offset.related_entry_id == consumption.id
    assert offset.note == "late_offset"
    totals = apply((adjustment, consumption, offset))
    assert (totals.r, totals.c, totals.a, totals.b) == (
        0,
        3 * PICO_PER_USD,
        53 * PICO_PER_USD,
        50 * PICO_PER_USD,
    )


def test_W08_attribution_uses_request_completion_time() -> None:
    links = (VendorLink("g", "v", T1, 1),)
    assert attribute_consumption(links, "g", BEFORE) is None
    assert attribute_consumption(links, "g", AFTER) == "v"


def test_W09_stale_revision_changes_nothing() -> None:
    entries = (entry("up", "top_up", "100", 1),)
    totals = apply(entries, revision=8)
    with pytest.raises(DomainError) as failure:
        plan_calibration(totals, 50 * PICO_PER_USD, 7)
    assert failure.value.code == WALLET_REVISION_CONFLICT
    assert apply(entries, revision=8) == totals


def test_W10_totals_and_coverage_do_not_depend_on_retained_usage_rows() -> None:
    entries = (entry("up", "top_up", "100", 1), entry("cost", "consumption", "30", 2, unpriced=True))
    before = apply(entries)
    usage_rows = [entries[1].usage_ref]
    usage_rows.clear()
    assert apply(entries) == before
    assert before.coverage_incomplete
    assert before.unpriced_count == 1
    assert (before.r, before.c, before.a, before.b) == (
        100 * PICO_PER_USD,
        30 * PICO_PER_USD,
        0,
        70 * PICO_PER_USD,
    )


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("1.239", "1.23"), ("-1.239", "-1.23"), ("0.009", "0.00"), ("-0.009", "0.00"), ("-0.5", "-0.50")],
)
def test_W11_balance_truncation(amount: str, expected: str) -> None:
    assert format_pico(parse_usd_amount(amount, allow_negative=True), "trunc2") == expected
