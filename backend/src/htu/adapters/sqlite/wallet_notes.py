"""Structured daily notes keep exact calibration amounts separate from display language."""

import sqlite3


def daily_notes(
    db: sqlite3.Connection, vendor_id: str, days: set[str], offset_minutes: int, first_seq: int | None
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {
        day: {"calibrationCount": 0, "calibrationPico": "0"} for day in days
    }
    if not days:
        return result
    names = ",".join("?" for _ in days)
    for row in db.execute(
        "SELECT date(occurred_at,?) day,amount_pico FROM wallet_entry WHERE vendor_id=? "
        "AND note='calibration' AND seq<>? AND amount_pico<>'0' "
        f"AND date(occurred_at,?) IN ({names})",
        (f"{offset_minutes:+d} minutes", vendor_id, first_seq or 0, f"{offset_minutes:+d} minutes", *days),
    ):
        note = result[row["day"]]
        note["calibrationCount"] = int(str(note["calibrationCount"])) + 1
        note["calibrationPico"] = str(int(str(note["calibrationPico"])) + int(row["amount_pico"]))
    return result
