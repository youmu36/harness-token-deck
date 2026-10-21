"""Retain financial counters and quality while removing expired request voucher indexes."""

import sqlite3

from htd.adapters.sqlite.key_codec import key_bytes


def compact_consumption(db: sqlite3.Connection, harness: str, key: str) -> None:
    original = db.execute(
        "SELECT id FROM wallet_entry WHERE usage_harness=? AND usage_key=? AND kind='consumption'",
        (harness, key_bytes(key)),
    ).fetchone()
    if original is None:
        return
    if db.execute(
        "SELECT 1 FROM wallet_validation_job WHERE vendor_id=(SELECT vendor_id FROM wallet_entry WHERE id=?)",
        (original[0],),
    ).fetchone():
        return
    rows = db.execute(
        "SELECT * FROM wallet_entry WHERE id=? OR related_entry_id=? OR related_entry_id IN "
        "(SELECT id FROM wallet_entry WHERE related_entry_id=?) ORDER BY seq",
        (original[0], original[0], original[0]),
    ).fetchall()
    latest = next(row for row in reversed(rows) if row["kind"] in ("consumption", "consumption_delta"))
    db.execute("INSERT INTO temp.allow_compaction VALUES(1)")
    try:
        for row in reversed(rows):
            bucket = row["occurred_at"][:16] + ":00.000Z"
            old = db.execute(
                "SELECT * FROM wallet_rollup WHERE vendor_id=? AND bucket=?", (row["vendor_id"], bucket)
            ).fetchone()
            consumption = (
                int(row["amount_pico"]) if row["kind"] in ("consumption", "consumption_delta") else 0
            )
            adjustment = int(row["amount_pico"]) if row["kind"] == "adjustment" else 0
            count = int(row["kind"] in ("consumption", "consumption_delta"))
            unpriced = row["unpriced"] if row["id"] == latest["id"] else 0
            incomplete = row["incomplete"] if row["id"] == latest["id"] else 0
            db.execute(
                "INSERT OR REPLACE INTO wallet_rollup VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    row["vendor_id"],
                    bucket,
                    str(consumption + (int(old["consumption"]) if old else 0)),
                    str(adjustment + (int(old["adjustment"]) if old else 0)),
                    count + (old["count"] if old else 0),
                    min(row["seq"], old["first_seq"]) if old else row["seq"],
                    max(row["seq"], old["last_seq"]) if old else row["seq"],
                    unpriced + (old["unpriced_count"] if old else 0),
                    incomplete + (old["incomplete_count"] if old else 0),
                ),
            )
            db.execute("DELETE FROM wallet_entry WHERE id=?", (row["id"],))
    finally:
        db.execute("DELETE FROM temp.allow_compaction")
