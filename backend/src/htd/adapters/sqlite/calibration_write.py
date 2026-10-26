"""Atomic target ownership, explicit allocation, wallet delta and original-day audit."""

import json
import sqlite3
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.calibration_plan import SCOPE_KEYS, Plan
from htd.adapters.sqlite.calibration_projection import audit
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.local_wallet import post, summary
from htd.adapters.sqlite.reconciliation_read import member_rows
from htd.adapters.sqlite.wallet_closing import bounds, mark, timezone
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.application.ports import UnitOfWork
from htd.domain.common.time import Instant, format_instant
from htd.domain.wallet.calibration import DIMENSIONS


def _scope_counts(
    db: sqlite3.Connection, identity: str, target: str, start: str, end: str, *, own_members: bool
) -> None:
    clause = "c.bucket_id=?" if own_members else "c.target_id=? AND m.occurred_at>=? AND m.occurred_at<?"
    args = (identity,) if own_members else (target, start, end)
    for dimension, name in DIMENSIONS.items():
        db.execute(
            "INSERT INTO calibration_scope_count SELECT ?,?,COALESCE(m." + name + ",''),COUNT(*) "
            "FROM calibration_member c JOIN reconciliation_member m USING(harness,dedupe_key) WHERE "
            + clause
            + " GROUP BY m."
            + name,
            (identity, dimension, *args),
        )
    db.execute(
        "INSERT INTO calibration_scope_count SELECT ?,'eligibility','known',COUNT(*) "
        "FROM calibration_member c JOIN reconciliation_member m USING(harness,dedupe_key) WHERE " + clause,
        (identity, *args),
    )
    db.execute(
        "INSERT INTO calibration_scope_count SELECT ?,'eligibility','unavailable',"
        "COALESCE(SUM(m.incomplete OR m.unpriced),0) FROM calibration_member c "
        "JOIN reconciliation_member m USING(harness,dedupe_key) WHERE " + clause,
        (identity, *args),
    )


def persist(
    work: UnitOfWork,
    db: sqlite3.Connection,
    plan: Plan,
    operation: str,
    at: Instant,
    *,
    legacy_flow: str | None = None,
) -> str:
    revoke = plan.view["action"] == "revoke"
    stamp = format_instant(at)
    for old in plan.replacements:
        state = "revoked" if revoke else "replaced"
        db.execute("UPDATE calibration_target SET state=?,revision=revision+1 WHERE id=?", (state, old["id"]))
        db.execute("DELETE FROM calibration_owner WHERE target_id=?", (old["id"],))
        audit(
            db,
            old["id"],
            state,
            stamp,
            operation + ":" + old["id"],
            int(old["estimate_pico"]),
            int(old["estimate_pico"]),
            int(old["delta_pico"]),
            0,
            "按当前估算撤销目标" if revoke else "显式完整替换原目标",
        )
    if revoke:
        identity = plan.replacements[0]["id"]
    else:
        identity = str(uuid4())
        scope = {key: plan.query[key] for key in SCOPE_KEYS if key in plan.query}
        estimate = sum(basis.estimate for basis in plan.day_basis.values())
        target = int(plan.view["targetPico"])
        evidence = {
            "kind": "legacy_flow" if legacy_flow else plan.proposal.get("basis", "manual"),
            "legacyFlowId": legacy_flow,
            "proposal": plan.proposal,
            "remoteIntervals": plan.view["remoteIntervals"],
            "snapshot": plan.view["snapshot"],
        }
        db.execute(
            "INSERT INTO calibration_target(id,vendor_id,state,target_pico,estimate_pico,delta_pico,"
            "scope_json,evidence_json,member_digest,member_count,created_at,operation_id) "
            "VALUES(?,?,'active',?,?,?,?,?,?,?,?,?)",
            (
                identity,
                plan.query["vendorId"],
                str(target),
                str(estimate),
                str(target - estimate),
                json.dumps(scope, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
                plan.view["memberDigest"],
                int(plan.view["memberCount"]),
                stamp,
                operation,
            ),
        )
        zone = timezone(db)
        for part in plan.buckets:
            if not part.basis.count and not part.target:
                continue
            start, end = map(format_instant, bounds(part.day, zone))
            start, end = max(start, plan.query["start"]), min(end, plan.query["end"])
            basis = part.basis if part.basis.count else plan.day_basis[part.day]
            bucket_id = identity + ":" + part.key
            db.execute(
                "INSERT INTO calibration_bucket VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
                (
                    bucket_id,
                    identity,
                    part.day,
                    str(part.target),
                    str(part.basis.estimate),
                    str(part.target - part.basis.estimate),
                    json.dumps(basis.scope(), sort_keys=True),
                    int(part.model_allocated),
                    start,
                    end,
                    part.basis.count,
                ),
            )
        pending: list[tuple[str, str, bytes, str, int]] = []
        for member in member_rows(db, plan.query):
            bucket = plan.bucket_for(member, ZoneInfo(zone))
            pending.append(
                (
                    identity,
                    member["harness"],
                    key_bytes(member["dedupe_key"]),
                    identity + ":" + bucket.key,
                    member["revision"],
                )
            )
            if len(pending) == 500:
                _save_members(db, pending)
                pending.clear()
        _save_members(db, pending)
        for part in plan.buckets:
            if not part.basis.count and not part.target:
                continue
            start, end = map(format_instant, bounds(part.day, zone))
            _scope_counts(
                db, identity + ":" + part.key, identity, start, end, own_members=part.basis.count > 0
            )
        if legacy_flow:
            db.execute(
                "INSERT INTO calibration_legacy_replacement VALUES(?,?,?) "
                "ON CONFLICT(flow_id) DO UPDATE SET "
                "target_id=excluded.target_id,occurred_at=excluded.occurred_at",
                (legacy_flow, identity, stamp),
            )
        audit(
            db,
            identity,
            "legacy_import" if legacy_flow else "applied",
            stamp,
            operation,
            estimate,
            estimate,
            int(plan.view["currentEffectivePico"]) - estimate,
            target - estimate,
            plan.proposal.get("note", "") or "人工确认消费目标与适用成员",
        )
    work.touch("cost")
    work.touch("wallet")
    return identity


def _save_members(db: sqlite3.Connection, rows: list[tuple[str, str, bytes, str, int]]) -> None:
    db.executemany("INSERT INTO calibration_member VALUES(?,?,?,?,?)", rows)
    db.executemany("INSERT INTO calibration_owner VALUES(?,?,?)", ((row[1], row[2], row[0]) for row in rows))


def move_money(
    work: UnitOfWork, db: sqlite3.Connection, plan: Plan, target: str, operation: str, at: Instant
) -> None:
    vendor = plan.query["vendorId"]
    before = summary(db, vendor)
    delta = int(plan.view["deltaPico"])
    repo = WalletRepository(db)
    if delta:
        totals = repo.totals(vendor)
        repo.calibrate(
            work,
            vendor,
            "0",
            target_pico=totals.b - delta,
            expected_revision=totals.revision,
            at=at,
            operation_id=operation + ":balance",
            record_operation=False,
            journal=False,
        )
    for day in plan.view["days"]:
        amount = int(day["deltaPico"])
        if not amount:
            continue
        _, end = bounds(day["day"], timezone(db))
        occurred = Instant(min(at.value, end.value - timedelta(milliseconds=1)))
        # The rollback-compatible balance voucher already moved B. Classify its
        # independent local effect as consumption, keeping recharge unchanged.
        post(
            db,
            work,
            vendor,
            occurred,
            operation + ":" + day["day"],
            0,
            amount,
            amount,
            "撤销消费目标" if plan.view["action"] == "revoke" else "人工锁定消费目标",
            "calibration:" + target,
            received=at,
            journal=False,
        )
        mark(db, vendor, format_instant(occurred))
    after = summary(db, vendor)
    if (
        int(after["R"]) != int(before["R"])
        or int(after["C"]) - int(before["C"]) != delta
        or int(after["B"]) - int(before["B"]) != -delta
    ):
        raise RuntimeError("Calibration wallet conservation failed")
    r, c, b = (int(before[key]) for key in ("R", "C", "B"))
    for day in plan.view["days"]:
        amount = int(day["deltaPico"])
        _, end = bounds(day["day"], timezone(db))
        occurred = format_instant(Instant(min(at.value, end.value - timedelta(milliseconds=1))))
        note = (
            ("撤销目标，恢复当前估算" if plan.view["action"] == "revoke" else "锁定人工目标") + " · " + target
        )
        db.execute(
            "INSERT INTO wallet_flow(id,vendor_id,scope,day,kind,occurred_at,posted_at,r,c,a,"
            "before_r,before_c,before_b,after_r,after_c,after_b,note) "
            "VALUES(?,?,'local',?,'adjustment',?,?,'0',?,'0',?,?,?,?,?,?,?)",
            (
                "f:" + str(uuid4()),
                vendor,
                day["day"],
                occurred,
                format_instant(at),
                str(amount),
                str(r),
                str(c),
                str(b),
                str(r),
                str(c + amount),
                str(b - amount),
                note,
            ),
        )
        c += amount
        b -= amount
