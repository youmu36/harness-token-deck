"""Retired numeric minutes preserve session and price dimensions for sixty-two days."""

import sqlite3
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.counter_codec import compress, decompress
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.wallet_closing import bounds, timezone
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.explorer import ExploreTotals

KEYS = ("bucket", "harness", "session_ref", "activity_id", "vendor_id", "group_id", "version_id", "model_key")


def install(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS usage_recent("
        + ",".join(name + " TEXT NOT NULL" for name in KEYS)
        + ",model_id TEXT NOT NULL,payload BLOB NOT NULL,PRIMARY KEY("
        + ",".join(KEYS)
        + ")) STRICT, WITHOUT ROWID"
    )
    db.execute("CREATE INDEX IF NOT EXISTS usage_recent_session ON usage_recent(session_ref,bucket,harness)")
    db.execute("CREATE INDEX IF NOT EXISTS usage_recent_model ON usage_recent(model_key,bucket,harness)")
    db.execute("CREATE INDEX IF NOT EXISTS usage_recent_activity ON usage_recent(activity_id,bucket)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS usage_recent_member(harness TEXT NOT NULL,dedupe_key BLOB NOT NULL,"
        "bucket TEXT NOT NULL,PRIMARY KEY(harness,dedupe_key)) STRICT, WITHOUT ROWID"
    )
    db.execute("CREATE INDEX IF NOT EXISTS usage_recent_member_time ON usage_recent_member(bucket)")


def retire(db: sqlite3.Connection, row: dict[str, Any], values: dict[str, int]) -> None:
    identity = row["harness"], key_bytes(row["dedupe_key"])
    member = db.execute(
        "SELECT session_ref FROM session_request WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    activity = db.execute(
        "SELECT batch_id FROM session_request_batch WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    bucket = row["occurred_at"][:16] + ":00.000Z"
    key = (
        bucket,
        row["harness"],
        member[0] or "" if member else "",
        activity[0] if activity else "",
        row["vendor_id"],
        row["price_group_id"] or "",
        row["price_version_id"] or "",
        row["model_key"],
    )
    where = " AND ".join(name + "=?" for name in KEYS)
    old = db.execute("SELECT payload FROM usage_recent WHERE " + where, key).fetchone()
    total = ExploreTotals()
    if old:
        total.add(decompress(old[0]))
    total.add(values)
    db.execute(
        "INSERT OR REPLACE INTO usage_recent VALUES(" + ",".join("?" for _ in range(10)) + ")",
        (*key, row["model_id"] or "未知模型", compress(total.values)),
    )
    db.execute("INSERT INTO usage_recent_member VALUES(?,?,?)", (*identity, bucket))


def fold(db: sqlite3.Connection, at: Instant, limit: int = 500) -> int:
    from htd.adapters.sqlite.explorer_storage import add

    zone = timezone(db)
    boundary_day = (at.value - timedelta(days=62)).astimezone(ZoneInfo(zone)).date().isoformat()
    # Fold only complete business days, keeping the whole boundary day detailed.
    cutoff = format_instant(bounds(boundary_day, zone)[0])
    rows = db.execute(
        "SELECT * FROM usage_recent WHERE bucket<? ORDER BY bucket LIMIT ?", (cutoff, limit)
    ).fetchall()
    for row in rows:
        day = parse_instant(row["bucket"]).value.astimezone(ZoneInfo(zone)).date().isoformat()
        start, end = map(format_instant, bounds(day, zone))
        key = (
            start,
            "day",
            row["harness"],
            row["vendor_id"],
            row["group_id"],
            row["version_id"],
            row["model_key"],
        )
        add(db, key, end, row["model_id"], decompress(row["payload"]))
        db.execute(
            "INSERT INTO usage_day_revision VALUES(?,?,1) "
            "ON CONFLICT(harness,day) DO UPDATE SET value=value+1",
            (row["harness"], row["bucket"][:10]),
        )
        db.execute(
            "DELETE FROM usage_recent WHERE " + " AND ".join(name + "=?" for name in KEYS),
            tuple(row[name] for name in KEYS),
        )
    db.execute(
        "DELETE FROM usage_recent_member WHERE (harness,dedupe_key) IN (SELECT harness,dedupe_key "
        "FROM usage_recent_member m WHERE bucket<? AND NOT EXISTS(SELECT 1 FROM usage_recent r "
        "WHERE r.bucket=m.bucket AND r.harness=m.harness) LIMIT 500)",
        (cutoff,),
    )
    return len(rows)
