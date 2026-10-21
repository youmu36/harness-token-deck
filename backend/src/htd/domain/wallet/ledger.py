"""Pure voucher totals, calibration plans, attribution, and late-consumption offsets."""

from collections.abc import Iterable

from htd.domain.common.errors import (
    AMOUNT_INVALID,
    OPERATION_ID_REUSED,
    WALLET_INVALID,
    WALLET_REVISION_CONFLICT,
    DomainError,
)
from htd.domain.common.money import parse_usd_amount
from htd.domain.common.time import Instant
from htd.domain.wallet.model import (
    Calibration,
    CalibrationPlan,
    LateOffsetPlan,
    Totals,
    UsageRef,
    Vendor,
    VendorLink,
    WalletEntry,
    entry_history,
    link_history,
    validate_identifier,
    validate_instant,
    validate_pico,
    validate_sequence,
)
from htd.domain.wallet.vendors import require_active


def apply(
    entries: Iterable[WalletEntry],
    *,
    revision: int = 0,
    watermark_seq: int | None = None,
    legacy_calibrated_at: Instant | None = None,
) -> Totals:
    records = entry_history(entries)
    if len({record.vendor_id for record in records}) > 1:
        raise DomainError(WALLET_INVALID, "只能汇总同一供应商的钱包", "vendor_id")
    by_id = {record.id: record for record in records}
    offsets: set[str] = set()
    quality: dict[UsageRef, WalletEntry] = {}
    r = c = a = 0
    last_calibrated_at = legacy_calibrated_at
    if legacy_calibrated_at is not None:
        validate_instant(legacy_calibrated_at, "legacy_calibrated_at")
    for record in sorted(records, key=lambda item: item.seq):
        if record.kind == "consumption_delta" or (
            record.kind == "adjustment" and record.note == "late_offset"
        ):
            related = by_id.get(record.related_entry_id or "")
            if (
                related is None
                or related.seq >= record.seq
                or related.kind not in ("consumption", "consumption_delta")
            ):
                raise DomainError(WALLET_INVALID, "关联的消费凭证缺失或顺序无效", "related_entry_id")
            if record.kind == "consumption_delta":
                if (
                    related.kind != "consumption"
                    or related.usage_ref != record.usage_ref
                    or related.occurred_at != record.occurred_at
                ):
                    raise DomainError(WALLET_INVALID, "重算差额须关联同一请求的原消费", "related_entry_id")
            elif related.id in offsets or related.amount_pico != record.amount_pico:
                raise DomainError(WALLET_INVALID, "迟到消费只能按其金额抵消一次", "related_entry_id")
            else:
                offsets.add(related.id)
        if record.kind == "top_up":
            r += record.amount_pico
        elif record.kind in ("consumption", "consumption_delta"):
            c += record.amount_pico
            if record.usage_ref is not None:
                quality[record.usage_ref] = record
        else:
            a += record.amount_pico
            if record.note == "calibration":
                last_calibrated_at = (
                    max(last_calibrated_at, record.occurred_at)
                    if last_calibrated_at is not None
                    else record.occurred_at
                )
    last_seq = max((record.seq for record in records), default=0)
    if watermark_seq is None:
        watermark_seq = last_seq
    validate_sequence(watermark_seq, "watermark_seq", minimum=last_seq)
    return Totals(
        r,
        c,
        a,
        last_calibrated_at,
        revision,
        watermark_seq,
        sum(record.unpriced for record in quality.values()),
        sum(record.incomplete for record in quality.values()),
    )


def plan_calibration(totals: Totals, target: int, revision: int) -> CalibrationPlan:
    if type(totals) is not Totals:
        raise DomainError(WALLET_INVALID, "校准须使用已校验的钱包快照", "totals")
    if type(revision) is not int or revision != totals.revision:
        raise DomainError(WALLET_REVISION_CONFLICT, "钱包余额已更新，请重新读取后确认", "expected_revision")
    validate_pico(target, "target")
    return CalibrationPlan(target, totals.b, totals.watermark_seq, revision)


def plan_initial_balance(initial_balance: str | None) -> CalibrationPlan | None:
    if initial_balance is None or initial_balance == "":
        return None
    return plan_calibration(Totals(), parse_usd_amount(initial_balance, allow_negative=True), 0)


def top_up(
    entries: Iterable[WalletEntry],
    vendor: Vendor,
    amount: str,
    *,
    entry_id: str,
    at: Instant,
    seq: int,
    operation_id: str,
) -> tuple[WalletEntry, ...]:
    records = entry_history(entries)
    validate_identifier(operation_id, "operation_id")
    pico = parse_usd_amount(amount)
    if pico <= 0:
        raise DomainError(AMOUNT_INVALID, "充值金额须大于零", "amount")
    for prior in records:
        if prior.operation_id == operation_id:
            if prior.kind != "top_up" or (prior.vendor_id, prior.amount_pico) != (vendor.id, pico):
                raise DomainError(OPERATION_ID_REUSED, "操作标识已用于其他资金操作", "operation_id")
            return records
    require_active(vendor)
    validate_sequence(seq, minimum=max((record.seq for record in records), default=0) + 1)
    record = WalletEntry(entry_id, vendor.id, "top_up", pico, at, seq, operation_id)
    return entry_history((*records, record))


def attribute_consumption(
    links: Iterable[VendorLink], group_id: str | None, occurred_at: Instant
) -> str | None:
    records = link_history(links)
    validate_instant(occurred_at, "occurred_at")
    if group_id is None:
        return None
    validate_identifier(group_id, "group_id")
    current = max(
        (link for link in records if link.group_id == group_id and link.effective_at <= occurred_at),
        key=lambda link: (link.effective_at, link.seq),
        default=None,
    )
    return None if current is None else current.vendor_id


def late_offsets(
    calibrations: Iterable[Calibration], consumption: WalletEntry, *, migration: bool = False
) -> tuple[LateOffsetPlan, ...]:
    if type(consumption) is not WalletEntry or type(migration) is not bool:
        raise DomainError(WALLET_INVALID, "迟到抵消的凭证或迁移模式无效", "consumption")
    records = tuple(calibrations)
    if any(type(calibration) is not Calibration for calibration in records):
        raise DomainError(WALLET_INVALID, "校准历史须为已校验的审计记录", "calibrations")
    if migration or consumption.kind not in ("consumption", "consumption_delta"):
        return ()
    if any(
        calibration.vendor_id == consumption.vendor_id
        and consumption.seq > calibration.seq
        and consumption.occurred_at <= calibration.at
        for calibration in records
    ):
        return (LateOffsetPlan(consumption.id, consumption.vendor_id, consumption.amount_pico),)
    return ()
