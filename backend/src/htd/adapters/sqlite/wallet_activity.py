"""Retain original spending and correction evidence independently of request compaction."""

import sqlite3


def append_activity(db: sqlite3.Connection, row: sqlite3.Row) -> None:
    kind = row["kind"]
    amount = int(row["amount_pico"])
    at = row["occurred_at"]
    note = row["note"]
    related = db.execute("SELECT seq FROM wallet_entry WHERE id=?", (row["related_entry_id"],)).fetchone()
    if kind == "consumption":
        at = at[:16] + ":00.000Z"
        old = db.execute(
            "SELECT seq,amount_pico,count FROM wallet_activity WHERE vendor_id=? AND "
            "occurred_at=? AND kind='consumption'",
            (row["vendor_id"], at),
        ).fetchone()
        if old:
            db.execute(
                "UPDATE wallet_activity SET amount_pico=?,count=count+1 WHERE seq=?",
                (str(int(old["amount_pico"]) + amount), old["seq"]),
            )
            return
        note = "original_spending"
    elif kind == "consumption_delta":
        kind, amount, note = "adjustment", -amount, "recalc"
    elif note == "calibration" and amount == 0:
        # The exact waterline remains in the immutable calibration audit.
        return
    db.execute(
        "INSERT INTO wallet_activity VALUES(?,?,?,?,?,?,?,?,?)",
        (
            row["seq"],
            row["vendor_id"],
            at,
            kind,
            str(amount),
            1,
            note,
            row["id"],
            related[0] if related else None,
        ),
    )


def initialize_activity(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM runtime_state WHERE key='wallet_activity_initialized'").fetchone():
        return
    # Existing compacted history cannot recover original/correction splits. Preserve
    # its exact balance effect and label it honestly instead of fabricating consumption.
    for row in db.execute("SELECT * FROM wallet_rollup ORDER BY first_seq"):
        db.execute(
            "INSERT INTO wallet_activity VALUES(?,?,?,?,?,?,?,?,?)",
            (
                row["first_seq"],
                row["vendor_id"],
                row["bucket"],
                "adjustment",
                str(int(row["adjustment"]) - int(row["consumption"])),
                row["count"],
                "legacy_compacted",
                None,
                None,
            ),
        )
    for row in db.execute("SELECT * FROM wallet_entry ORDER BY seq"):
        append_activity(db, row)
    db.execute("INSERT INTO runtime_state VALUES('wallet_activity_initialized','true')")
