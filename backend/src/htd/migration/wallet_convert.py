"""Plan deterministic wallet vouchers while preserving each legacy vendor balance."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from htd.adapters.sqlite.usage_codec import dumps
from htd.adapters.sqlite.wallet_store import WalletStore
from htd.adapters.sqlite.writer import UnitOfWork
from htd.domain.common.errors import DomainError
from htd.domain.common.money import parse_usd_amount
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.wallet.ledger import apply
from htd.domain.wallet.model import UsageRef, Vendor, WalletEntry
from htd.domain.wallet.vendors import create_vendor
from htd.migration.legacy.merge import LegacyMerged

NAMESPACE = UUID("c29fa0db-4f91-531c-932b-89e4e94ecfa0")


def stable_id(table: str, old_key: str) -> str:
    return str(uuid5(NAMESPACE, f"legacy:{table}:{old_key}"))


@dataclass(frozen=True, slots=True)
class WalletPlan:
    vendor: Vendor
    old_balance: int
    entries: tuple[WalletEntry, ...]
    audits: tuple[dict[str, Any], ...]
    coverage_start: Instant
    incomplete_history: bool
    group_ids: tuple[str, ...]


def plan_wallets(
    merged: LegacyMerged, ledger: tuple[dict[str, Any], ...], *, at: Instant, incomplete_history: bool
) -> tuple[WalletPlan, ...]:
    plans: list[WalletPlan] = []
    vendors: tuple[Vendor, ...] = ()
    seq = 0
    earliest = min((parse_instant(row["occurred_at"]) for row in ledger), default=at)
    for vi, data in enumerate(merged.vendors):
        try:
            created = parse_instant(data["createdAt"])
            vendors = create_vendor(vendors, vendor_id=data["id"], name=data["name"], at=created)
            vendor = vendors[-1]
            groups = tuple(g for g, v in merged.group_vendors.items() if v == vendor.id)
            calibrations = data["balanceCalibrations"]
            latest = calibrations[-1] if calibrations else None
            bound = parse_instant(latest["at"]) if latest else created
            balance = parse_usd_amount(latest["balance"], allow_negative=True) if latest else 0

            def after(stamp: str, bound: Instant = bound, strict: bool = latest is not None) -> bool:
                instant = parse_instant(stamp)
                return instant > bound if strict else instant >= bound

            old = (
                balance
                + sum(parse_usd_amount(t["amount"]) for t in data["topUps"] if after(t["at"]))
                - sum(
                    int(r["pico_usd"])
                    for r in ledger
                    if r["price_group_id"] in groups and after(r["occurred_at"])
                )
            )
            entries: list[WalletEntry] = []
            for ti, top in enumerate(data["topUps"]):
                seq += 1
                key = f"{vendor.id}:{ti}"
                entries.append(
                    WalletEntry(
                        stable_id("top_up", key),
                        vendor.id,
                        "top_up",
                        parse_usd_amount(top["amount"]),
                        parse_instant(top["at"]),
                        seq,
                        stable_id("top_up_operation", key),
                    )
                )
            for row in ledger:
                if row["price_group_id"] not in groups or parse_instant(row["occurred_at"]) < created:
                    continue
                seq += 1
                key = f"{row['harness']}:{row['dedupe_key']}"
                entries.append(
                    WalletEntry(
                        stable_id("consumption", key),
                        vendor.id,
                        "consumption",
                        int(row["pico_usd"]),
                        parse_instant(row["occurred_at"]),
                        seq,
                        usage_ref=UsageRef(row["harness"], row["dedupe_key"]),
                        unpriced=bool(row["unpriced"]),
                        incomplete=bool(row["incomplete"]),
                    )
                )
            raw = apply(entries)
            seq += 1
            entries.append(
                WalletEntry(
                    stable_id("adjustment", vendor.id),
                    vendor.id,
                    "adjustment",
                    old - raw.b,
                    at,
                    seq,
                    note="migration",
                )
            )
            for calibration in calibrations:
                parse_instant(calibration["at"])
                parse_usd_amount(calibration["balance"], allow_negative=True)
            plans.append(
                WalletPlan(
                    vendor,
                    old,
                    tuple(entries),
                    tuple(calibrations),
                    max(created, earliest),
                    incomplete_history,
                    groups,
                )
            )
        except DomainError as error:
            raise DomainError(
                error.code, error.message, f"vendors[{vi}].{error.field or 'record'}"
            ) from error
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError("LEGACY_CONFIGURATION_INVALID", "供应商记录无效", f"vendors[{vi}]") from error
    return tuple(plans)


def convert_wallet(work: UnitOfWork, plans: tuple[WalletPlan, ...]) -> None:
    db = work.connection
    repo = WalletStore(db)
    for vi, plan in enumerate(plans, 1):
        v = plan.vendor
        db.execute(
            "INSERT INTO vendor VALUES (?,?,?,?,?)",
            (v.id, v.name, v.status, format_instant(v.created_at), vi),
        )
        db.execute("INSERT INTO wallet_total VALUES (?,'0','0','0',0)", (v.id,))
        for group in plan.group_ids:
            seq = db.execute("INSERT INTO history_seq DEFAULT VALUES").lastrowid
            db.execute(
                "INSERT INTO vendor_link VALUES (?,?,?,?)", (seq, group, v.id, format_instant(v.created_at))
            )
        for entry in plan.entries:
            repo.append(work, entry)
        for ci, audit in enumerate(plan.audits):
            db.execute(
                "INSERT INTO vendor_legacy_record VALUES (?,?,?,?,?,?)",
                (
                    stable_id("vendor_legacy_record", f"{v.id}:{ci}"),
                    v.id,
                    "calibration",
                    format_instant(parse_instant(audit["at"])),
                    str(parse_usd_amount(audit["balance"], allow_negative=True)),
                    dumps(audit),
                ),
            )
        for key, value in (
            ("history_coverage_start", format_instant(plan.coverage_start)),
            ("history_incomplete", "true" if plan.incomplete_history else "false"),
        ):
            db.execute("INSERT INTO runtime_state VALUES (?,?)", (f"{key}:{v.id}", value))
