"""Stable business days, bounded offline catch-up and immutable local estimate revisions."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from htu.adapters.sqlite.wallet_archive import unpack
from htu.application.ports import UnitOfWork
from htu.domain.common.time import Instant, format_instant, parse_instant


def timezone(db: sqlite3.Connection) -> str:
    row = db.execute("SELECT value_json FROM preference WHERE key='accountingTimezone'").fetchone()
    return json.loads(row[0]) if row else "Asia/Shanghai"


def bounds(day: str, zone: str) -> tuple[Instant, Instant]:
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(zone))
    end = (start + timedelta(days=1)).astimezone(UTC)
    return Instant(start.astimezone(UTC)), Instant(end)


def mark(db: sqlite3.Connection, vendor: str, at: str) -> None:
    zone = timezone(db)
    day = parse_instant(at).value.astimezone(ZoneInfo(zone)).date().isoformat()
    db.execute("INSERT OR IGNORE INTO wallet_daily_dirty VALUES(?,?,?)", (vendor, day, zone))


def backfill(db: sqlite3.Connection, limit: int = 500) -> bool:
    state = db.execute("SELECT * FROM wallet_daily_backfill WHERE id=1").fetchone()
    if state["ready"]:
        return True
    if not state["hot_done"]:
        rows = list(
            db.execute(
                "SELECT vendor_id,occurred_at FROM wallet_activity WHERE (vendor_id,occurred_at)>(?,?) "
                "GROUP BY vendor_id,occurred_at ORDER BY vendor_id,occurred_at LIMIT ?",
                (state["last_vendor"], state["last_at"], limit),
            )
        )
        for row in rows:
            mark(db, row["vendor_id"], row["occurred_at"])
        if rows:
            db.execute("UPDATE wallet_daily_backfill SET last_vendor=?,last_at=? WHERE id=1", tuple(rows[-1]))
        if len(rows) < limit:
            db.execute("UPDATE wallet_daily_backfill SET hot_done=1 WHERE id=1")
        return False
    block = db.execute(
        "SELECT * FROM wallet_archive WHERE id>? ORDER BY id LIMIT 1", (state["cold_id"],)
    ).fetchone()
    if block:
        frame = unpack(block)
        for row in frame["rollup"]:
            mark(db, row["vendor_id"], row["bucket"])
        db.execute("UPDATE wallet_daily_backfill SET cold_id=? WHERE id=1", (block["id"],))
        return False
    db.execute("UPDATE wallet_daily_backfill SET ready=1 WHERE id=1")
    return True


def close_days(work: UnitOfWork, db: sqlite3.Connection, *, at: Instant, limit: int = 32) -> int:
    backfill(db)
    zone = timezone(db)
    today = at.value.astimezone(ZoneInfo(zone)).date().isoformat()
    dirty = list(
        db.execute(
            "SELECT * FROM wallet_daily_dirty WHERE day<? ORDER BY day,vendor_id LIMIT ?", (today, limit)
        )
    )
    for item in dirty:
        start, end = (format_instant(value) for value in bounds(item["day"], item["timezone"]))
        hot = db.execute(
            "SELECT amount_pico,kind,seq FROM wallet_entry WHERE vendor_id=? AND occurred_at>=? "
            "AND occurred_at<? AND kind IN ('consumption','consumption_delta')",
            (item["vendor_id"], start, end),
        )
        amount = count = 0
        first_seq = None
        for row in hot:
            amount += int(row["amount_pico"])
            count += row["kind"] == "consumption"
            first_seq = min(first_seq or row["seq"], row["seq"])
        for row in db.execute(
            "SELECT consumption,count,first_seq FROM wallet_rollup WHERE "
            "vendor_id=? AND bucket>=? AND bucket<?",
            (item["vendor_id"], start, end),
        ):
            amount += int(row["consumption"])
            count += row["count"]
            first_seq = min(first_seq or row["first_seq"], row["first_seq"])
        for block in db.execute(
            "SELECT * FROM wallet_archive WHERE vendor_id=? AND end_at>=? AND start_at<? AND rollup_count>0",
            (item["vendor_id"], start, end),
        ):
            for row in unpack(block)["rollup"]:
                if start <= row["bucket"] < end:
                    amount += int(row["consumption"])
                    count += row["count"]
                    first_seq = min(first_seq or row["first_seq"], row["first_seq"])
        key = item["vendor_id"], item["day"], item["timezone"]
        old = db.execute(
            "SELECT * FROM wallet_daily_close WHERE vendor_id=? AND day=? AND timezone=?", key
        ).fetchone()
        if old is None and count == 0:
            db.execute("DELETE FROM wallet_daily_dirty WHERE vendor_id=? AND day=? AND timezone=?", key)
            continue
        if old is None or int(old["current"]) != amount:
            seq = (
                first_seq
                if old is None
                else db.execute(
                    "UPDATE wallet_sequence SET value=value+1 WHERE id=1 RETURNING value"
                ).fetchone()[0]
            )
            if old is None:
                db.execute(
                    "INSERT INTO wallet_daily_close(vendor_id,day,timezone,original,cu"
                    "rrent,seq,count) VALUES(?,?,?,?,?,?,?)",
                    (*key, str(amount), str(amount), seq, count),
                )
            else:
                revision = old["revision"] + 1
                db.execute(
                    "INSERT INTO wallet_daily_correction(vendor_id,day,timezone,before"
                    "_pico,after_pico,posted_at,revision,seq) VALUES(?,?,?,?,?,?,?,?)",
                    (*key, old["current"], str(amount), format_instant(at), revision, seq),
                )
                db.execute(
                    "UPDATE wallet_daily_close SET current=?,revision=? WHERE "
                    "vendor_id=? AND day=? AND timezone=?",
                    (str(amount), revision, *key),
                )
            work.touch("display")
        db.execute("DELETE FROM wallet_daily_dirty WHERE vendor_id=? AND day=? AND timezone=?", key)
    return len(dirty)
