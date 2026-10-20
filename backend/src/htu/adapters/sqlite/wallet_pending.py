"""Read pending amounts without rescanning requests on each wallet page refresh."""

import sqlite3
from collections.abc import Sequence
from typing import Any, cast

from htu.application.ports import UnitOfWork
from htu.domain.remote.accounting import CENT, settle


def install(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS remote_wallet_pending_estimate("
        "segment_id TEXT PRIMARY KEY REFERENCES account_segment(id) ON DELETE CASCADE,"
        "start_endpoint TEXT NOT NULL,end_endpoint TEXT NOT NULL,estimate TEXT NOT NULL) STRICT"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS account_pending_manual ON account_posting(segment_id,occurred_at) "
        "WHERE kind='manual_top_up' AND interval_id IS NULL"
    )
    # One-time catch-up for accounts already waiting when this feature is installed.
    for row in db.execute(
        "SELECT p.segment_id FROM account_pending p JOIN account_segment s ON s.id=p.segment_id "
        "LEFT JOIN remote_wallet_pending_estimate v ON v.segment_id=p.segment_id "
        "WHERE s.ended_at IS NULL AND p.start_endpoint<>p.end_endpoint AND "
        "(v.segment_id IS NULL OR v.start_endpoint<>p.start_endpoint OR v.end_endpoint<>p.end_endpoint)"
    ).fetchall():
        sync(db, row[0])


def sync(db: sqlite3.Connection, segment: str, work: UnitOfWork | None = None) -> None:
    """Advance only the new observed interval; late billed inputs update at commit."""
    from htu.adapters.sqlite.account_book import AccountBook
    from htu.adapters.sqlite.writer import UnitOfWork as SqlWork

    if work is not None:
        transaction = cast(SqlWork, work)
        if transaction.wallet_estimates:
            commit_estimates(db, transaction.wallet_estimates)
            transaction.wallet_estimates.clear()

    row = db.execute(
        "SELECT p.*,s.vendor_id,b.observed_at start_at,e.observed_at end_at,b.u start_u,e.u end_u "
        "FROM account_pending p JOIN account_segment s ON s.id=p.segment_id "
        "JOIN account_endpoint b ON b.id=p.start_endpoint JOIN account_endpoint e ON e.id=p.end_endpoint "
        "WHERE p.segment_id=?",
        (segment,),
    ).fetchone()
    if row is None:
        return
    old = db.execute(
        "SELECT p.*,e.observed_at end_at FROM remote_wallet_pending_estimate p "
        "JOIN account_endpoint e ON e.id=p.end_endpoint WHERE p.segment_id=?",
        (segment,),
    ).fetchone()
    estimate = 0
    if row["start_endpoint"] != row["end_endpoint"] and (row["start_u"] is None or row["end_u"] is None):
        start = row["start_at"]
        if old and old["start_endpoint"] == row["start_endpoint"] and old["end_at"] <= row["end_at"]:
            estimate, start = int(old["estimate"]), old["end_at"]
        if start < row["end_at"]:
            estimate += AccountBook(db).estimate(row["vendor_id"], start, row["end_at"])
    db.execute(
        "INSERT INTO remote_wallet_pending_estimate VALUES(?,?,?,?) ON CONFLICT(segment_id) DO UPDATE SET "
        "start_endpoint=excluded.start_endpoint,end_endpoint=excluded.end_endpoint,estimate=excluded.estimate",
        (segment, row["start_endpoint"], row["end_endpoint"], str(estimate)),
    )


def commit_estimates(db: sqlite3.Connection, samples: dict[tuple[str, str], int]) -> None:
    by_vendor: dict[str, list[tuple[str, int]]] = {}
    for (vendor, occurred), amount in samples.items():
        by_vendor.setdefault(vendor, []).append((occurred, amount))
    for vendor, changes in by_vendor.items():
        for row in db.execute(
            "SELECT p.segment_id,p.estimate,b.observed_at start_at,e.observed_at end_at "
            "FROM remote_wallet_pending_estimate p JOIN account_segment s ON s.id=p.segment_id "
            "JOIN account_endpoint b ON b.id=p.start_endpoint JOIN account_endpoint e ON e.id=p.end_endpoint "
            "WHERE s.vendor_id=? AND s.ended_at IS NULL AND (b.u IS NULL OR e.u IS NULL)",
            (vendor,),
        ).fetchall():
            delta = sum(amount for occurred, amount in changes if row["start_at"] < occurred <= row["end_at"])
            if delta:
                db.execute(
                    "UPDATE remote_wallet_pending_estimate SET estimate=? WHERE segment_id=?",
                    (str(int(row["estimate"]) + delta), row["segment_id"]),
                )


def _remote(db: sqlite3.Connection, vendor: str) -> tuple[int, int]:
    rows = db.execute(
        "SELECT p.segment_id,p.start_endpoint,p.end_endpoint,COALESCE(v.estimate,'0') estimate "
        "FROM account_pending p JOIN account_segment s ON s.id=p.segment_id "
        "LEFT JOIN remote_wallet_pending_estimate v ON v.segment_id=p.segment_id "
        "AND v.start_endpoint=p.start_endpoint AND v.end_endpoint=p.end_endpoint "
        "WHERE s.vendor_id=? AND s.ended_at IS NULL AND p.start_endpoint<>p.end_endpoint "
        "UNION ALL SELECT b.segment_id,b.start_endpoint,b.end_endpoint,b.estimate "
        "FROM account_pending_batch b JOIN account_segment s ON s.id=b.segment_id WHERE s.vendor_id=?",
        (vendor, vendor),
    ).fetchall()
    recharge = consumption = 0
    for row in rows:
        start, end = (
            db.execute("SELECT * FROM account_endpoint WHERE id=?", (row[key],)).fetchone()
            for key in ("start_endpoint", "end_endpoint")
        )
        used = int(end["u"]) - int(start["u"]) if start["u"] is not None and end["u"] is not None else None
        manual = sum(
            int(p[0])
            for p in db.execute(
                "SELECT r FROM account_posting WHERE segment_id=? AND interval_id IS NULL "
                "AND kind='manual_top_up' AND occurred_at>? AND occurred_at<=?",
                (row["segment_id"], start["observed_at"], end["observed_at"]),
            )
        )
        if abs(int(end["b"]) - int(start["b"])) < CENT and abs(used or 0) < CENT and not manual:
            continue
        result = settle(int(start["b"]), int(end["b"]), int(row["estimate"]), used=used, manual_r=manual)
        recharge += result.recharge
        consumption += result.consumption
    return recharge, consumption


def display(
    db: sqlite3.Connection,
    vendor: str,
    scope: str,
    totals: dict[str, Any] | None,
    staged: Sequence[tuple[int, int]] = (),
) -> dict[str, Any] | None:
    if totals is None:
        return None
    amounts = [
        (int(r[0]), int(r[1]))
        for r in db.execute(
            "SELECT r,c FROM wallet_flow_pending WHERE vendor_id=? AND scope=?",
            (vendor, scope),
        )
    ] + list(staged)
    pr, pc = sum(r for r, _ in amounts), sum(c for _, c in amounts)
    rr, rc = _remote(db, vendor) if scope == "remote" else (0, 0)
    return {
        **totals,
        "postedR": str(int(totals["R"]) - pr),
        "postedC": str(int(totals["C"]) - pc),
        "pendingTopUpPico": str(pr + rr),
        "pendingConsumptionPico": str(pc + rc),
    }
