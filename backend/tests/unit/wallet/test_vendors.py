"""Active-name uniqueness, capacity, archiving, and unlink plans."""

from dataclasses import replace

import pytest
from htu.domain.common.errors import (
    VENDOR_ARCHIVED,
    VENDOR_LIMIT,
    VENDOR_NAME_CONFLICT,
    VENDOR_NOT_FOUND,
    DomainError,
)
from htu.domain.wallet.ledger import attribute_consumption, top_up
from htu.domain.wallet.model import Vendor, VendorLink
from htu.domain.wallet.vendors import (
    create_vendor,
    plan_delete_vendor,
    rename_vendor,
    require_active,
    restore_vendor,
)

from .conftest import T0, T1, T2, entry


def test_names_unique_only_among_active_vendors_and_case_sensitive() -> None:
    original = (Vendor("old", "V", "archived", T0),)
    vendors = create_vendor(original, vendor_id="new", name=" V ", at=T1)
    assert vendors[-1].name == "V"
    vendors = create_vendor(vendors, vendor_id="other", name="v", at=T1)
    assert len(vendors) == 3
    with pytest.raises(DomainError) as failure:
        create_vendor(vendors, vendor_id="duplicate", name=" V ", at=T2)
    assert failure.value.code == VENDOR_NAME_CONFLICT
    assert original == (Vendor("old", "V", "archived", T0),)


def test_capacity_includes_archived_vendors() -> None:
    vendors = tuple(Vendor(str(i), str(i), "archived", T0) for i in range(199))
    full = create_vendor(vendors, vendor_id="last", name="Last", at=T1)
    assert len(full) == 200
    with pytest.raises(DomainError) as failure:
        create_vendor(full, vendor_id="overflow", name="Overflow", at=T1)
    assert failure.value.code == VENDOR_LIMIT


def test_rename_checks_trimmed_names_and_is_pure_or_unchanged() -> None:
    vendors = (Vendor("v1", "First", "active", T0), Vendor("v2", "Second", "active", T0))
    assert rename_vendor(vendors, "v1", " First ") is vendors
    changed = rename_vendor(vendors, "v1", " New ")
    assert changed[0].name == "New"
    assert vendors[0].name == "First"
    with pytest.raises(DomainError) as failure:
        rename_vendor(vendors, "v1", "Second")
    assert failure.value.code == VENDOR_NAME_CONFLICT


def test_restore_rechecks_active_name_and_keeps_links_unrestored() -> None:
    archived = Vendor("v", "V", "archived", T0)
    vendors = (archived, Vendor("other", "V", "active", T0))
    with pytest.raises(DomainError) as failure:
        restore_vendor(vendors, "v")
    assert failure.value.code == VENDOR_NAME_CONFLICT
    restored = restore_vendor((archived,), "v")
    assert restored == (replace(archived, status="active"),)
    assert restore_vendor(restored, "v") is restored


def test_archived_user_mutations_are_read_only() -> None:
    vendor = Vendor("v", "V", "archived", T0)
    for operation in (
        lambda: require_active(vendor),
        lambda: rename_vendor((vendor,), "v", "New"),
        lambda: top_up((), vendor, "1", entry_id="up", at=T1, seq=1, operation_id="up"),
    ):
        with pytest.raises(DomainError) as failure:
            operation()
        assert failure.value.code == VENDOR_ARCHIVED


@pytest.mark.parametrize("has_records", [False, True])
def test_delete_physically_or_archive_and_unlink_only_current_groups(has_records: bool) -> None:
    vendor = Vendor("v", "V", "active", T0)
    links = (
        VendorLink("g2", "v", T0, 1),
        VendorLink("g1", "v", T0, 2),
        VendorLink("g1", "other", T1, 3),
        VendorLink("g3", "v", T1, 4),
        VendorLink("g4", "v", T0, 5),
        VendorLink("g4", None, T1, 6),
    )
    entries = (entry("zero", "consumption", "0", 1),) if has_records else ()
    plan = plan_delete_vendor(vendor, entries, links, at=T2, next_seq=7)
    assert plan.action == ("archive" if has_records else "delete")
    assert plan.vendor == (replace(vendor, status="archived") if has_records else None)
    assert plan.unlinks == (VendorLink("g2", None, T2, 7), VendorLink("g3", None, T2, 8))
    assert links[0].vendor_id == "v"


def test_any_voucher_including_zero_calibration_prevents_physical_deletion() -> None:
    vendor = Vendor("v", "V", "active", T0)
    record = entry("cal", "adjustment", "0", 1, note="calibration")
    assert plan_delete_vendor(vendor, (record,), (), at=T1, next_seq=1).action == "archive"


def test_deletion_does_not_count_another_vendors_records() -> None:
    vendor = Vendor("v", "V", "active", T0)
    record = entry("up", "top_up", "1", 1, vendor_id="other")
    assert plan_delete_vendor(vendor, (record,), (), at=T1, next_seq=1).action == "delete"


def test_deletion_cannot_write_unlink_before_current_association_or_reuse_sequence() -> None:
    vendor = Vendor("v", "V", "active", T0)
    links = (VendorLink("g", "v", T1, 2),)
    for at, seq in ((T0, 3), (T2, 2)):
        with pytest.raises(DomainError):
            plan_delete_vendor(vendor, (), links, at=at, next_seq=seq)


def test_delete_unlinks_at_execution_time_even_if_other_vendor_has_later_history() -> None:
    vendor = Vendor("v", "V", "active", T0)
    links = (VendorLink("g", "v", T0, 1), VendorLink("g", "other", T2, 2))
    plan = plan_delete_vendor(vendor, (), links, at=T1, next_seq=3)
    assert plan.unlinks == (VendorLink("g", None, T1, 3),)
    combined = (*links, *plan.unlinks)
    assert attribute_consumption(combined, "g", T0) == "v"
    assert attribute_consumption(combined, "g", T1) is None
    assert attribute_consumption(combined, "g", T2) == "other"


def test_unknown_vendor_is_structured_failure() -> None:
    for operation in (lambda: rename_vendor((), "missing", "Name"), lambda: restore_vendor((), "missing")):
        with pytest.raises(DomainError) as failure:
            operation()
        assert failure.value.code == VENDOR_NOT_FOUND


def test_duplicate_vendor_ids_rejected_even_if_names_differ() -> None:
    original = (Vendor("v", "First", "active", T0),)
    with pytest.raises(DomainError):
        create_vendor(original, vendor_id="v", name="Second", at=T1)
