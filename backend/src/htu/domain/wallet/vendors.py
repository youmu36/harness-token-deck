"""Pure vendor name/capacity rules and deletion plans that preserve historical attribution."""

from collections.abc import Iterable
from dataclasses import replace

from htu.domain.common.errors import (
    VENDOR_ARCHIVED,
    VENDOR_LIMIT,
    VENDOR_NAME_CONFLICT,
    VENDOR_NOT_FOUND,
    WALLET_INVALID,
    DomainError,
)
from htu.domain.common.time import Instant
from htu.domain.wallet.model import (
    Vendor,
    VendorDeletionPlan,
    VendorLink,
    WalletEntry,
    entry_history,
    link_history,
    validate_instant,
    validate_sequence,
    vendor_name,
)


def _vendors(vendors: Iterable[Vendor]) -> tuple[Vendor, ...]:
    records = tuple(vendors)
    if any(type(vendor) is not Vendor for vendor in records) or len({vendor.id for vendor in records}) != len(
        records
    ):
        raise DomainError(WALLET_INVALID, "供应商列表类型无效或标识重复", "vendors")
    if len(records) > 200:
        raise DomainError(VENDOR_LIMIT, "最多保存 200 个供应商，含归档项", "vendors")
    names = [vendor.name for vendor in records if vendor.status == "active"]
    if len(set(names)) != len(names):
        raise DomainError(VENDOR_NAME_CONFLICT, "活跃供应商名称重复", "name")
    return records


def _vendor(vendors: tuple[Vendor, ...], vendor_id: str) -> Vendor:
    result = next((vendor for vendor in vendors if vendor.id == vendor_id), None)
    if result is None:
        raise DomainError(VENDOR_NOT_FOUND, "供应商不存在", "vendor_id")
    return result


def require_active(vendor: Vendor) -> None:
    if type(vendor) is not Vendor:
        raise DomainError(WALLET_INVALID, "供应商须为已校验的 Vendor", "vendor")
    if vendor.status == "archived":
        raise DomainError(VENDOR_ARCHIVED, "已归档供应商只读，请先恢复", "vendor_id")


def validate_vendor_name(
    vendors: Iterable[Vendor], name: str, *, excluding_vendor_id: str | None = None
) -> str:
    normalized = vendor_name(name)
    if any(
        vendor.status == "active" and vendor.id != excluding_vendor_id and vendor.name == normalized
        for vendor in _vendors(vendors)
    ):
        raise DomainError(VENDOR_NAME_CONFLICT, "活跃供应商已使用该名称", "name")
    return normalized


def create_vendor(vendors: Iterable[Vendor], *, vendor_id: str, name: str, at: Instant) -> tuple[Vendor, ...]:
    records = _vendors(vendors)
    if len(records) >= 200:
        raise DomainError(VENDOR_LIMIT, "最多保存 200 个供应商，含归档项", "vendors")
    vendor = Vendor(vendor_id, validate_vendor_name(records, name), "active", at)
    return _vendors((*records, vendor))


def rename_vendor(vendors: Iterable[Vendor], vendor_id: str, name: str) -> tuple[Vendor, ...]:
    records = _vendors(vendors)
    original = _vendor(records, vendor_id)
    require_active(original)
    updated = replace(original, name=validate_vendor_name(records, name, excluding_vendor_id=vendor_id))
    if updated == original:
        return records
    return tuple(updated if vendor.id == vendor_id else vendor for vendor in records)


def restore_vendor(vendors: Iterable[Vendor], vendor_id: str) -> tuple[Vendor, ...]:
    records = _vendors(vendors)
    original = _vendor(records, vendor_id)
    if original.status == "active":
        return records
    validate_vendor_name(records, original.name, excluding_vendor_id=vendor_id)
    return tuple(replace(vendor, status="active") if vendor.id == vendor_id else vendor for vendor in records)


def plan_delete_vendor(
    vendor: Vendor,
    entries: Iterable[WalletEntry],
    links: Iterable[VendorLink],
    *,
    at: Instant,
    next_seq: int,
) -> VendorDeletionPlan:
    if type(vendor) is not Vendor:
        raise DomainError(WALLET_INVALID, "供应商须为已校验的 Vendor", "vendor")
    records = entry_history(entries)
    associations = link_history(links)
    validate_instant(at, "at")
    validate_sequence(next_seq, "next_seq", minimum=max((link.seq for link in associations), default=0) + 1)
    latest: dict[str, VendorLink] = {}
    current: dict[str, VendorLink] = {}
    for link in sorted(associations, key=lambda item: (item.effective_at, item.seq)):
        latest[link.group_id] = link
        if link.effective_at <= at:
            current[link.group_id] = link
    if any(link.vendor_id == vendor.id and link.effective_at > at for link in latest.values()):
        raise DomainError(WALLET_INVALID, "解除关联的时间不能早于当前关联", "effective_at")
    owned = sorted(
        (link for link in current.values() if link.vendor_id == vendor.id), key=lambda link: link.group_id
    )
    unlinks = tuple(VendorLink(link.group_id, None, at, next_seq + index) for index, link in enumerate(owned))
    if any(record.vendor_id == vendor.id for record in records):
        return VendorDeletionPlan("archive", replace(vendor, status="archived"), unlinks)
    return VendorDeletionPlan("delete", None, unlinks)
