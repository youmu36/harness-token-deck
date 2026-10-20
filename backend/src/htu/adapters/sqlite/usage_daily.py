"""Exact permanent day counters, active-minute unions and atomic source retirement."""

import hashlib
import json
import sqlite3
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from htu.adapters.sqlite.counter_codec import FIELDS, FLAGS, compress, decompress, unpack
from htu.adapters.sqlite.wallet_closing import bounds, timezone
from htu.application.ports import UnitOfWork
from htu.domain.common.time import Instant, format_instant, parse_instant


def cutoff(db: sqlite3.Connection, at: Instant) -> str:
    local = at.value.astimezone(ZoneInfo(timezone(db)))
    month = local.year * 12 + local.month - 1 - 2
    day = date(month // 12, month % 12 + 1, 1).isoformat()
    return format_instant(bounds(day, timezone(db))[0])


def add(db: sqlite3.Connection, row: dict[str, Any]) -> None:
    zone = timezone(db)
    instant = parse_instant(row["bucket"])
    day = instant.value.astimezone(ZoneInfo(zone)).date().isoformat()
    start, end = bounds(day, zone)
    key = row["harness"], day, row["vendor_id"], row["model_key"]
    old = db.execute(
        "SELECT * FROM usage_daily WHERE harness=? AND business_day=? AND vendor_id=? AND model_key=?", key
    ).fetchone()
    counter = dict(row)
    counter["bucket"] = format_instant(start)
    counter["latest_bucket"] = row.get("latest_bucket", row["bucket"])
    active = (1 << int((instant.value - start.value).total_seconds() // 60)) if row["output"] > 0 else 0
    missing = {"request_timing", "missing_field_counts"}
    if row["missing_usage"]:
        missing.add("complete_output_samples")
    if old:
        previous = decompress(old["payload"])
        for field in FIELDS[6:]:
            counter[field] = (
                str(int(previous[field]) + int(row[field]))
                if field == "pico"
                else int(bool(previous[field] or row[field]))
                if field in FLAGS
                else previous[field] + row[field]
            )
        if previous.get("latest_bucket", previous["bucket"]) > counter["latest_bucket"]:
            for field in ("model_id", "display_name", "latest_bucket"):
                counter[field] = previous.get(field, previous["bucket"])
        active |= int(old["active_minutes"])
        missing.update(json.loads(old["missing_capabilities"]))
    payload = compress(counter)
    if decompress(payload) != counter:
        raise ValueError("Daily authority verification failed")
    db.execute(
        "INSERT INTO usage_daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON "
        "CONFLICT(harness,business_day,vendor_id,model_key) "
        "DO UPDATE SET payload=excluded.payload,active_minutes=excluded.ac"
        "tive_minutes,missing_capabilities=excluded.missing_capabilities,r"
        "evision=usage_daily.revision+1,model_id=excluded.model_id,records"
        "=excluded.records",
        (
            *key,
            zone,
            format_instant(start),
            format_instant(end),
            payload,
            str(active),
            json.dumps(sorted(missing)),
            1,
            counter["model_id"],
            str(counter["records"]),
        ),
    )


def publish(
    db: sqlite3.Connection, work: UnitOfWork, *, at: Instant, limit: int = 500, before: str | None = None
) -> int:
    boundary = before or cutoff(db, at)
    rows = list(
        db.execute(
            "SELECT * FROM usage_rollup WHERE bucket<? ORDER BY bucket,harness,vendor_id,model_key LIMIT ?",
            (boundary, limit),
        )
    )
    for row in rows:
        add(db, dict(row))
    db.executemany(
        "DELETE FROM usage_rollup WHERE harness=? AND bucket=? AND vendor_id=? AND model_key=?",
        ((row["harness"], row["bucket"], row["vendor_id"], row["model_key"]) for row in rows),
    )
    count = sum(row["records"] for row in rows)
    if not rows:
        # The compressed source frame has a hard 500-row limit, so a transaction
        # always remains bounded even during years of offline catch-up.
        block = db.execute(
            "SELECT * FROM usage_archive WHERE start_at<? ORDER BY id LIMIT 1", (boundary,)
        ).fetchone()
        if block:
            if hashlib.sha256(block["payload"]).hexdigest() != block["checksum"]:
                raise ValueError("Usage archive checksum mismatch")
            counters = unpack(block["payload"])
            expired = [row for row in counters if row["bucket"] < boundary]
            retained = [row for row in counters if row["bucket"] >= boundary]
            for row in expired:
                add(db, row)
            db.execute("DELETE FROM usage_archive WHERE id=?", (block["id"],))
            if retained:
                from htu.adapters.sqlite.usage_archive import insert_counters

                insert_counters(db, retained)
            count = sum(row["records"] for row in expired)
    if count:
        work.touch("usage")
        work.touch("sources")
    return count


def rows(db: sqlite3.Connection, start: str, end: str, harness: str = "all", model_key: str | None = None):
    clause = "" if harness == "all" else " AND harness=?"
    args = () if harness == "all" else (harness,)
    if model_key is not None:
        clause += " AND model_key=?"
        args += (model_key,)
    for record in db.execute(
        "SELECT * FROM usage_daily WHERE end_at>? AND start_at<?"
        + clause
        + " ORDER BY harness,start_at,vendor_id,model_key",
        (start, end, *args),
    ):
        yield record, decompress(record["payload"])


def exact_summary(db: sqlite3.Connection, start: str, end: str, harness: str) -> dict[str, object]:
    totals = {
        name: 0
        for name in (
            "records",
            "input",
            "cache_read",
            "output",
            "pico",
            "rated_input",
            "rated_cache",
            "missing_usage",
            "unknown_model",
        )
    }
    minutes: dict[tuple[str, str], int] = {}
    missing: set[str] = set()
    days: set[str] = set()
    partial = False
    for record, counter in rows(db, start, end, harness):
        for field in totals:
            totals[field] += int(counter[field])
        key = record["business_day"], record["timezone"]
        minutes[key] = minutes.get(key, 0) | int(record["active_minutes"])
        days.add(record["business_day"])
        missing.update(json.loads(record["missing_capabilities"]))
        partial |= start > record["start_at"] or end < record["end_at"]
    return {
        "totals": {name: str(value) for name, value in totals.items()},
        "activeMinutes": str(sum(bits.bit_count() for bits in minutes.values())),
        "coveredDays": len(days),
        "businessTimezone": timezone(db),
        "includesWholeBoundaryDays": partial,
        "missingCapabilities": sorted(missing),
        "basis": "本地历史估算；账户消费另按账户区间累计",  # noqa: RUF001
    }
