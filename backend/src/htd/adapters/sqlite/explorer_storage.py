"""Preserve price dimensions when details retire; merge old days into months."""

import hashlib
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
    from htd.adapters.sqlite.recent_usage import install as install_recent

    install_recent(db)
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
    db.execute(
        "CREATE INDEX IF NOT EXISTS usage_dimensions_scope ON usage_dimensions(harness,model_key,bucket)"
    )
    db.execute("CREATE TABLE IF NOT EXISTS usage_quality_day AS SELECT * FROM usage_dimensions WHERE 0")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS usage_quality_day_identity ON usage_quality_day"
        "(bucket,grain,harness,vendor_id,group_id,version_id,model_key)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS usage_quality_day_range ON usage_quality_day(harness,model_key,bucket)"
    )
    db.execute("CREATE TABLE IF NOT EXISTS usage_quality_legacy AS SELECT * FROM usage_dimensions WHERE 0")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS usage_quality_legacy_identity ON usage_quality_legacy"
        "(bucket,grain,harness,vendor_id,group_id,version_id,model_key)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS usage_quality_legacy_range ON usage_quality_legacy(end_at,bucket)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS quality_ack_evidence(id TEXT NOT NULL,fingerprint TEXT NOT NULL,"
        "acknowledged_at TEXT NOT NULL,PRIMARY KEY(id,fingerprint)) STRICT, WITHOUT ROWID"
    )


def detail(
    db: sqlite3.Connection, row: dict[str, Any], price_cache: dict[tuple[str, str], Any] | None = None
) -> dict[str, int]:
    i, c, o, w = (row.get(key) for key in ("input", "cache_read", "output", "cache_write"))
    if c is None and row.get("detected_cache") is not None:
        c = row["detected_cache"]
        i = i + c if i is not None else None
    unprobed = row["harness"] == "zcode" and "grok" in row["model_key"] and c == 0
    rated = i is not None and c is not None and not unprobed
    complete = not row["incomplete"] and not row["unpriced"] and rated and o is not None
    paid_write = False
    if w and complete:
        cache = price_cache if price_cache is not None else {}
        price_key = row["price_revision_id"], row["model_key"]
        if price_key not in cache:
            rate = db.execute(
                "SELECT cache_write,rule_json FROM price_revision_model WHERE revision_id=? AND model_key=?",
                price_key,
            ).fetchone()
            from decimal import Decimal

            if rate:
                base = rate["cache_write"]
                alternate = (
                    json.loads(rate["rule_json"])["rates"].get("cache_write") if rate["rule_json"] else base
                )
                cache[price_key] = (
                    base is not None and Decimal(base) > 0,
                    alternate is not None and Decimal(alternate) > 0,
                )
            else:
                cache[price_key] = None
        evidence = cache[price_key]
        if evidence is not None:
            paid_write = evidence[int(row["rule_applied"] in ("tier", "peak"))]
        else:
            complete = False  # A priced alias without retained component evidence is not guessed.
    missing = {
        "version": int(not row["price_version_id"]),
        "cache": int(not rated),
        "incomplete": int(row["incomplete"]),
        "unpriced": int(row["unpriced"]),
    }
    from htd.adapters.sqlite.key_codec import key_bytes

    signature = (
        int.from_bytes(
            hashlib.sha256(
                row["harness"].encode()
                + key_bytes(row["dedupe_key"])
                + json.dumps(missing, sort_keys=True).encode()
            ).digest()
        )
        if any(missing.values())
        else 0
    )
    return {
        "records": 1,
        "eligible_records": int(complete),
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
        "quality_known": 1,
        "quality_count": int(any(missing.values())),
        "quality_signature": signature,
        **{"quality_" + key: value for key, value in missing.items()},
    }


def add(
    db: sqlite3.Connection,
    key: tuple[str, ...],
    end: str,
    name: str,
    values: dict[str, int],
    *,
    table: str = "usage_dimensions",
) -> None:
    if table not in ("usage_dimensions", "usage_quality_day"):
        raise ValueError("Invalid dimension projection")
    old = db.execute(
        f"SELECT payload FROM {table} WHERE bucket=? AND grain=? AND harness=? AND vendor_id=? "
        "AND group_id=? AND version_id=? AND model_key=?",
        key,
    ).fetchone()
    totals = ExploreTotals()
    if old:
        previous = decompress(old[0])
        if table == "usage_dimensions":
            preserve_legacy(db, key, end, name, previous)
        totals.add(previous)
    totals.add(values)
    db.execute(
        f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
        "payload=excluded.payload,model_id=excluded.model_id,end_at=excluded.end_at",
        (key[0], end, *key[1:], name, compress(totals.values)),
    )


def legacy_values(values: dict[str, int]) -> dict[str, int]:
    result = {key: value for key, value in values.items() if not key.startswith("quality_")}
    result["records"] -= values.get("quality_known", 0) + values.get("quality_legacy", 0)
    for field, reason in (("unknown", "cache"), ("incomplete", "incomplete"), ("unpriced", "unpriced")):
        result[field] = max(0, result.get(field, 0) - values.get("quality_" + reason, 0))
    return result


def preserve_legacy(
    db: sqlite3.Connection, key: tuple[str, ...], end: str, name: str, values: dict[str, int]
) -> None:
    old = legacy_values(values)
    if old["records"] <= 0:
        return
    # Freeze the old day/month before merging it. Its lower-bound evidence and
    # acknowledgement identity then survive later monthly folding and new data.
    db.execute(
        "INSERT OR IGNORE INTO usage_quality_legacy VALUES(?,?,?,?,?,?,?,?,?,?)",
        (key[0], end, *key[1:], name, compress(old)),
    )
    values["quality_legacy"] = values.get("quality_legacy", 0) + old["records"]


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
    values = detail(db, row)
    from htd.adapters.sqlite.recent_usage import retire as retire_recent

    retire_recent(db, row, values)
    add(
        db,
        key,
        format_instant(end),
        row["model_id"] or "未知模型",
        {name: value for name, value in values.items() if name.startswith("quality_") or name == "records"},
        table="usage_quality_day",
    )


def compact(db: sqlite3.Connection, at: Instant) -> int:
    from htd.adapters.sqlite.recent_usage import fold

    folded = fold(db, at)
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
        values = decompress(row["payload"])
        preserve_legacy(
            db,
            tuple(
                row[field]
                for field in (
                    "bucket",
                    "grain",
                    "harness",
                    "vendor_id",
                    "group_id",
                    "version_id",
                    "model_key",
                )
            ),
            row["end_at"],
            row["model_id"],
            values,
        )
        add(db, key, format_instant(Instant(end)), row["model_id"], values)
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
    return len(rows) + folded
