"""Preserve price dimensions when details retire; merge old days into months."""

import json
import sqlite3
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.counter_codec import compress, decompress
from htd.adapters.sqlite.wallet_closing import bounds, timezone
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.explorer import ExploreTotals


def install(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS quality_notice_ack(id TEXT PRIMARY KEY,fingerprint TEXT NOT "
        "NULL,acknowledged_at TEXT NOT NULL) STRICT"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS usage_dimensions("
        "bucket TEXT NOT NULL,end_at TEXT NOT NULL,grain TEXT NOT NULL,harness TEXT NOT NULL,"
        "vendor_id TEXT NOT NULL,group_id TEXT NOT NULL,version_id TEXT NOT NULL,model_key TEXT NOT NULL,"
        "model_id TEXT NOT NULL,payload BLOB NOT NULL,"
        "PRIMARY KEY(bucket,grain,harness,vendor_id,group_id,version_id,model_key)) STRICT, WITHOUT ROWID"
    )
    db.execute("CREATE INDEX IF NOT EXISTS usage_dimensions_range ON usage_dimensions(end_at,bucket)")
    db.execute("CREATE INDEX IF NOT EXISTS usage_dimensions_version ON usage_dimensions(version_id,bucket)")


def detail(db: sqlite3.Connection, row: dict[str, Any]) -> dict[str, int]:
    i, c, o, w = (row.get(key) for key in ("input", "cache_read", "output", "cache_write"))
    if c is None and row.get("detected_cache") is not None:
        c = row["detected_cache"]
        i = i + c if i is not None else None
    unprobed = row["harness"] == "zcode" and "grok" in row["model_key"] and c == 0
    rated = i is not None and c is not None and not unprobed
    complete = not row["incomplete"] and not row["unpriced"] and rated and o is not None
    paid_write = False
    if w and complete:
        rate = db.execute(
            "SELECT cache_write,rule_json FROM price_revision_model WHERE revision_id=? AND model_key=?",
            (row["price_revision_id"], row["model_key"]),
        ).fetchone()
        if rate:
            value = rate["cache_write"]
            if row["rule_applied"] in ("tier", "peak") and rate["rule_json"]:
                value = json.loads(rate["rule_json"])["rates"].get("cache_write")
            from decimal import Decimal

            paid_write = value is not None and Decimal(value) > 0
        else:
            complete = False  # A priced alias without retained component evidence is not guessed.
    return {
        "records": 1,
        "input": i or 0,
        "cache": c or 0,
        "output": o or 0,
        "write": w or 0,
        "pico": int(row["pico"]),
        "rated_input": (i or 0) if rated else 0,
        "rated_cache": (c or 0) if rated else 0,
        "unknown": int(not rated),
        "incomplete": int(row["incomplete"]),
        "unpriced": int(row["unpriced"]),
        "eligible_input": ((i or 0) - (c or 0) - ((w or 0) if paid_write else 0)) if complete else 0,
        "eligible_cache": (c or 0) if complete else 0,
        "eligible_output": (o or 0) if complete else 0,
        "eligible_write": (w or 0) if complete and paid_write else 0,
        "eligible_pico": int(row["pico"]) if complete else 0,
    }


def add(db: sqlite3.Connection, key: tuple[str, ...], end: str, name: str, values: dict[str, int]) -> None:
    old = db.execute(
        "SELECT payload FROM usage_dimensions WHERE bucket=? AND grain=? AND harness=? AND vendor_id=? "
        "AND group_id=? AND version_id=? AND model_key=?",
        key,
    ).fetchone()
    totals = ExploreTotals()
    if old:
        totals.add(decompress(old[0]))
    totals.add(values)
    db.execute(
        "INSERT INTO usage_dimensions VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
        "payload=excluded.payload,model_id=excluded.model_id,end_at=excluded.end_at",
        (key[0], end, *key[1:], name, compress(totals.values)),
    )


def retire(db: sqlite3.Connection, row: dict[str, Any]) -> None:
    day = parse_instant(row["occurred_at"]).value.astimezone(ZoneInfo(timezone(db))).date().isoformat()
    start, end = bounds(day, timezone(db))
    key = (
        format_instant(start),
        "day",
        row["harness"],
        row["vendor_id"],
        row["price_group_id"] or "",
        row["price_version_id"] or "",
        row["model_key"],
    )
    add(db, key, format_instant(end), row["model_id"] or "模型未知", detail(db, row))


def compact(db: sqlite3.Connection, at: Instant) -> int:
    zone = ZoneInfo(timezone(db))
    cutoff = at.value.astimezone(zone).replace(year=at.value.astimezone(zone).year - 2, month=1, day=1)
    rows = db.execute(
        "SELECT * FROM usage_dimensions WHERE grain='day' AND end_at<=? ORDER BY bucket LIMIT 200",
        (format_instant(Instant(cutoff)),),
    ).fetchall()
    for row in rows:
        local = parse_instant(row["bucket"]).value.astimezone(zone).replace(day=1)
        end = (local + timedelta(days=32)).replace(day=1)
        key = (
            format_instant(Instant(local)),
            "month",
            row["harness"],
            row["vendor_id"],
            row["group_id"],
            row["version_id"],
            row["model_key"],
        )
        add(db, key, format_instant(Instant(end)), row["model_id"], decompress(row["payload"]))
        db.execute(
            "DELETE FROM usage_dimensions WHERE bucket=? AND grain=? AND harness=? AND vendor_id=? "
            "AND group_id=? AND version_id=? AND model_key=?",
            (
                row["bucket"],
                row["grain"],
                row["harness"],
                row["vendor_id"],
                row["group_id"],
                row["version_id"],
                row["model_key"],
            ),
        )
    return len(rows)
