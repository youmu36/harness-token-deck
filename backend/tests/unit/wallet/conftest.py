"""Synthetic wallet records; no persistent ledger or real sources are read."""

from htd.domain.common.hashing import identity_key
from htd.domain.common.money import parse_usd_amount
from htd.domain.common.time import Instant, parse_instant
from htd.domain.usage.model import Harness
from htd.domain.wallet.ledger import apply, plan_calibration
from htd.domain.wallet.model import Calibration, EntryKind, UsageRef, WalletEntry

T0 = parse_instant("2026-09-30T09:00:00Z")
T1 = parse_instant("2026-09-30T10:00:00Z")
T2 = parse_instant("2026-09-30T11:00:00Z")
BEFORE = parse_instant("2026-09-30T09:59:59.999Z")
AFTER = parse_instant("2026-09-30T10:00:00.001Z")


def usage(key: str = "request", harness: Harness = Harness.CODEX) -> UsageRef:
    return UsageRef(harness, identity_key(["session", key]))


def entry(
    entry_id: str,
    kind: EntryKind,
    amount: str,
    seq: int,
    *,
    vendor_id: str = "v",
    at: Instant = T0,
    operation_id: str | None = None,
    usage_ref: UsageRef | None = None,
    related_entry_id: str | None = None,
    recalc_batch_id: str | None = None,
    note: str = "",
    unpriced: bool = False,
    incomplete: bool = False,
) -> WalletEntry:
    return WalletEntry(
        entry_id,
        vendor_id,
        kind,
        parse_usd_amount(amount, allow_negative=True),
        at,
        seq,
        operation_id or (f"op-{entry_id}" if kind == "top_up" or note == "calibration" else None),
        usage_ref or (usage(entry_id) if kind == "consumption" else None),
        related_entry_id,
        recalc_batch_id,
        note,
        unpriced,
        incomplete,
    )


def calibrated(
    entries: tuple[WalletEntry, ...], target: str, seq: int, *, at: Instant = T1, entry_id: str = "cal"
) -> tuple[WalletEntry, Calibration]:
    totals = apply(entries, revision=5)
    plan = plan_calibration(totals, parse_usd_amount(target, allow_negative=True), 5)
    return plan.records(entry_id=entry_id, vendor_id="v", at=at, seq=seq, operation_id=f"op-{entry_id}")
