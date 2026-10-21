"""Persistent natural-day directories with bounded, resumable construction per offset."""

import sqlite3
from datetime import timedelta
from typing import Any

from htd.adapters.sqlite.wallet_archive import unpack
from htd.domain.common.time import parse_instant


def _add(db: sqlite3.Connection, row: dict[str, Any], offset: int) -> None:
    day = (parse_instant(row["occurred_at"]).value + timedelta(minutes=offset)).date().isoformat()
    db.execute(
        "INSERT INTO wallet_day VALUES(?,?,?,?,?,?) ON CONFLICT(vendor_id,"
        "offset_minutes,day) DO UPDATE SET first_seq=MIN(first_seq,"
        "excluded.first_seq),amount_pico=htd_pico_add(amount_pico,"
        "excluded.amount_pico),count=count+excluded.count",
        (row["vendor_id"], offset, day, row["seq"], row["amount_pico"], row["count"]),
    )


def build(db: sqlite3.Connection, offset: int, *, limit: int = 500) -> bool:
    state = db.execute("SELECT * FROM wallet_day_offset WHERE offset_minutes=?", (offset,)).fetchone()
    if state is None:
        target = db.execute("SELECT value FROM wallet_sequence WHERE id=1").fetchone()[0]
        db.execute("INSERT INTO wallet_day_offset(offset_minutes,target) VALUES(?,?)", (offset, target))
        state = db.execute("SELECT * FROM wallet_day_offset WHERE offset_minutes=?", (offset,)).fetchone()
    if state["ready"]:
        return True
    if not state["hot_done"]:
        rows = db.execute(
            "SELECT * FROM wallet_activity WHERE kind='consumption' AND seq>? AND "
            "seq<=? ORDER BY seq LIMIT ?",
            (state["last_seq"], state["target"], limit),
        ).fetchall()
        for row in rows:
            _add(db, dict(row), offset)
        if rows:
            db.execute(
                "UPDATE wallet_day_offset SET last_seq=? WHERE offset_minutes=?", (rows[-1]["seq"], offset)
            )
        if len(rows) < limit:
            db.execute("UPDATE wallet_day_offset SET hot_done=1 WHERE offset_minutes=?", (offset,))
        return False
    block = db.execute(
        "SELECT * FROM wallet_archive WHERE id>? ORDER BY id LIMIT 1", (state["cold_id"],)
    ).fetchone()
    if block is not None:
        for row in unpack(block)["activity"]:
            if row["kind"] == "consumption":
                _add(db, row, offset)
        db.execute("UPDATE wallet_day_offset SET cold_id=? WHERE offset_minutes=?", (block["id"], offset))
        return False
    db.execute("UPDATE wallet_day_offset SET ready=1 WHERE offset_minutes=?", (offset,))
    return True
