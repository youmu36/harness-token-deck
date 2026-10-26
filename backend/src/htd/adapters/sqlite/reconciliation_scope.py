"""Freeze local calibration coverage only for a completed remote transaction."""

import sqlite3
from datetime import timedelta

from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.activity import QUIET

RETENTION = timedelta(days=3)


def install(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS reconciliation_interval_scope("
        "interval_id TEXT PRIMARY KEY REFERENCES account_interval(id),"
        "start_at TEXT,end_at TEXT NOT NULL,completed_at TEXT NOT NULL) STRICT"
    )


def freeze(db: sqlite3.Connection, interval: str, completed: str) -> None:
    if db.execute("SELECT 1 FROM reconciliation_interval_scope WHERE interval_id=?", (interval,)).fetchone():
        return
    row = db.execute(
        "SELECT s.vendor_id,b.observed_at start_at,e.observed_at end_at,i.e_watermark "
        "FROM account_interval i JOIN account_segment s ON s.id=i.segment_id "
        "JOIN account_endpoint b ON b.id=i.start_endpoint "
        "JOIN account_endpoint e ON e.id=i.end_endpoint WHERE i.id=?",
        (interval,),
    ).fetchone()
    assert row is not None
    cutoff = format_instant(Instant(parse_instant(completed).value - RETENTION))
    lead = max(cutoff, format_instant(Instant(parse_instant(row["start_at"]).value - timedelta(minutes=5))))
    start = db.execute(
        "SELECT MIN(COALESCE(d.started_at,m.occurred_at)) FROM reconciliation_member m "
        "LEFT JOIN session_directory d ON d.ref=m.session_ref WHERE m.vendor_id=? "
        "AND m.occurred_at>=? AND m.occurred_at<=? AND m.received_at<=? AND m.voucher_seq<=?",
        (row["vendor_id"], lead, row["end_at"], completed, row["e_watermark"]),
    ).fetchone()[0]
    previous = db.execute(
        "SELECT MAX(c.end_at) FROM reconciliation_interval_scope c "
        "JOIN account_interval i ON i.id=c.interval_id JOIN account_segment s ON s.id=i.segment_id "
        "JOIN account_endpoint e ON e.id=i.end_endpoint WHERE s.vendor_id=? AND e.observed_at<=?",
        (row["vendor_id"], row["start_at"]),
    ).fetchone()[0]
    if start:
        start = max(start, previous or start)
        # An older session is not silently truncated into an apparently complete scope.
        if start < cutoff or start >= completed:
            start = None
    db.execute(
        "INSERT INTO reconciliation_interval_scope VALUES(?,?,?,?)", (interval, start, completed, completed)
    )
    db.execute("DELETE FROM reconciliation_interval_member WHERE interval_id=?", (interval,))
    if start:
        db.execute(
            "INSERT INTO reconciliation_interval_member SELECT ?,m.harness,m.dedupe_key,m.revision "
            "FROM reconciliation_member m WHERE m.vendor_id=? AND m.occurred_at>=? "
            "AND m.occurred_at<? AND m.received_at<=? AND m.voucher_seq<=?",
            (interval, row["vendor_id"], start, completed, completed, row["e_watermark"]),
        )


def backfill(db: sqlite3.Connection, at: Instant) -> set[str]:
    """Recent released flows only; no history scan and no write from a page read."""
    from htd.adapters.sqlite.usage_codec import object_json

    ready = db.execute("SELECT value FROM runtime_state WHERE key='reconciliation_backfill'").fetchone()
    if ready and not object_json(ready[0])["done"]:
        return set()
    cutoff = format_instant(Instant(at.value - RETENTION))
    rows = db.execute(
        "SELECT p.interval_id,MIN(f.posted_at) completed_at,f.vendor_id FROM wallet_flow f "
        "JOIN account_posting p ON p.vendor_id=f.vendor_id "
        "AND p.rowid BETWEEN f.source_first AND f.source_last "
        "WHERE f.scope='remote' AND f.kind='consumption' AND f.posted_at>=? AND p.kind='consumption' "
        "AND p.interval_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM reconciliation_interval_scope c "
        "WHERE c.interval_id=p.interval_id) GROUP BY p.interval_id LIMIT 50",
        (cutoff,),
    ).fetchall()
    for row in rows:
        last = db.execute(
            "SELECT MAX(m.received_at) FROM reconciliation_member m JOIN account_interval i "
            "ON i.id=? JOIN account_segment s ON s.id=i.segment_id WHERE m.vendor_id=s.vendor_id "
            "AND m.voucher_seq<=i.e_watermark AND m.received_at<=? AND m.received_at>=?",
            (row[0], row[1], cutoff),
        ).fetchone()[0]
        completed = (
            min(row[1], format_instant(Instant(parse_instant(last).value + QUIET))) if last else row[1]
        )
        freeze(db, row[0], completed)
    return {row[2] for row in rows}
