"""Carry already-authorized flow targets into the same owner and cost overlay without posting twice."""

import json
import sqlite3
from typing import Any

from htd.adapters.sqlite.calibration_plan import build
from htd.adapters.sqlite.calibration_write import persist
from htd.adapters.sqlite.reconciliation_read import member_rows
from htd.adapters.sqlite.usage_codec import object_json
from htd.adapters.sqlite.wallet_closing import bounds, timezone
from htd.application.ports import CostChanged, UnitOfWork
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant


def assert_editable(db: sqlite3.Connection, flow: sqlite3.Row) -> None:
    if not flow["source_first"]:
        return
    start, end = map(format_instant, bounds(flow["day"], timezone(db)))
    owners = db.execute(
        "SELECT DISTINCT t.id,t.state,t.evidence_json FROM reconciliation_member m "
        "JOIN calibration_owner o USING(harness,dedupe_key) JOIN calibration_target t ON t.id=o.target_id "
        "WHERE m.vendor_id=? AND m.voucher_seq BETWEEN ? AND ? AND m.occurred_at>=? AND m.occurred_at<? "
        "AND (NOT EXISTS(SELECT 1 FROM wallet_flow_member f WHERE f.flow_id=?) OR EXISTS "
        "(SELECT 1 FROM wallet_flow_member f WHERE f.flow_id=? AND f.source_seq=m.voucher_seq))",
        (flow["vendor_id"], flow["source_first"], flow["source_last"], start, end, flow["id"], flow["id"]),
    ).fetchall()
    for row in owners:
        if json.loads(row["evidence_json"]).get("legacyFlowId") != flow["id"]:
            raise DomainError(
                "CALIBRATION_SCOPE_OWNED", "此流水成员已采用新的锁定目标，请在校准记录中更改或撤销"
            )
    replacement = db.execute(
        "SELECT t.* FROM calibration_legacy_replacement r "
        "JOIN calibration_target t ON t.id=r.target_id WHERE r.flow_id=?",
        (flow["id"],),
    ).fetchone()
    if replacement is not None and replacement["state"] != "active":
        raise DomainError("CALIBRATION_SCOPE_OWNED", "原流水目标已被替换或撤销，请使用当前核对范围创建目标")


def capture(work: UnitOfWork, db: sqlite3.Connection, flow_id: str, at: Instant) -> bool:
    backfill = db.execute("SELECT value FROM runtime_state WHERE key='reconciliation_backfill'").fetchone()
    if not backfill or not object_json(backfill[0])["done"]:
        return False
    flow = db.execute(
        "SELECT * FROM wallet_flow WHERE id=? AND scope='local' AND kind='consumption'", (flow_id,)
    ).fetchone()
    if flow is None or not flow["source_first"]:
        return False
    start, end = map(format_instant, bounds(flow["day"], timezone(db)))
    query: dict[str, Any] = {
        "vendorId": flow["vendor_id"],
        "start": start,
        "end": min(end, format_instant(at)),
        "legacyFlowId": flow_id,
    }
    count = sum(1 for _ in member_rows(db, query))
    exact = db.execute("SELECT COUNT(*) FROM wallet_flow_member WHERE flow_id=?", (flow_id,)).fetchone()[0]
    original = (
        exact
        or db.execute(
            "SELECT COUNT(*) FROM wallet_entry WHERE vendor_id=? AND kind='consumption' AND seq "
            "BETWEEN ? AND ? "
            "AND occurred_at>=? AND occurred_at<?",
            (flow["vendor_id"], flow["source_first"], flow["source_last"], start, end),
        ).fetchone()[0]
    )
    if not count or count != original:
        return False
    if (
        not exact
        and db.execute(
            "SELECT 1 FROM wallet_rollup WHERE vendor_id=? AND bucket>=? AND bucket<? LIMIT 1",
            (flow["vendor_id"], start, end),
        ).fetchone()
    ):
        return False
    previous = db.execute(
        "SELECT target_id FROM calibration_legacy_replacement WHERE flow_id=?", (flow_id,)
    ).fetchone()
    proposal = dict(
        query,
        action="apply",
        targetPico=flow["c"],
        basis="manual",
        confirmLocalScope=True,
        acknowledgeIncomplete=True,
        confirmReplacement=True,
        replaceTargetIds=[previous[0]] if previous else [],
        note="承接原人工流水目标",
    )
    plan = build(db, proposal, at, legacy=True)
    persist(work, db, plan, f"legacy:{flow_id}:{flow['revision']}", at, legacy_flow=flow_id)
    work.emit(CostChanged(work.revision("cost")))
    return True


def capture_pending(work: UnitOfWork, db: sqlite3.Connection, at: Instant) -> int:
    backfill = db.execute("SELECT value FROM runtime_state WHERE key='reconciliation_backfill'").fetchone()
    if not backfill or not object_json(backfill[0])["done"]:
        return 0
    revision = work.revision("usage")
    rows = db.execute(
        "SELECT o.flow_id FROM local_wallet_flow_override o WHERE NOT EXISTS "
        "(SELECT 1 FROM calibration_legacy_replacement r WHERE r.flow_id=o.flow_id) AND NOT EXISTS "
        "(SELECT 1 FROM calibration_legacy_deferred d WHERE d.flow_id=o.flow_id AND "
        "d.usage_revision=?) LIMIT 1",
        (revision,),
    ).fetchall()
    completed = 0
    for row in rows:
        if capture(work, db, row[0], at):
            completed += 1
        else:
            db.execute(
                "INSERT INTO calibration_legacy_deferred VALUES(?,?) ON CONFLICT(flow_id) "
                "DO UPDATE SET usage_revision=excluded.usage_revision",
                (row[0], revision),
            )
    return completed
