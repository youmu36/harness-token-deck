"""Recover valuation-only changes made by older writers after they retire request detail."""

import sqlite3

from htd.adapters.sqlite.calibration_projection import refresh
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.recalc_codec import audit
from htd.adapters.sqlite.session_projection import COUNTERS, change_counters


def recover(db: sqlite3.Connection, *, limit: int = 500) -> int:
    pending = db.execute("SELECT value FROM runtime_state WHERE key='session_recalc_until'").fetchone()
    if pending is None:
        return 0
    marker = db.execute("SELECT value FROM runtime_state WHERE key='session_recalc_cursor'").fetchone()
    cursor = int(marker[0]) if marker else 0
    rows = db.execute(
        "SELECT i.rowid cursor,i.*,b.committed_at at FROM recalc_item i "
        "JOIN recalc_batch b ON b.id=i.batch_id "
        "WHERE i.rowid>? AND i.rowid<=? ORDER BY i.rowid LIMIT ?",
        (cursor, int(pending[0]), limit),
    ).fetchall()
    for row in rows:
        key = row["harness"], key_bytes(row["dedupe_key"])
        member = db.execute(
            "SELECT * FROM reconciliation_member WHERE harness=? AND dedupe_key=?", key
        ).fetchone()
        if (
            member is None
            or db.execute("SELECT 1 FROM ledger WHERE harness=? AND dedupe_key=?", key).fetchone()
        ):
            continue  # The normal bounded ledger backfill handles retained details.
        _, updated = audit(row)
        if str(updated["pico"]) == member["estimate_pico"] and (
            updated["price_group_id"] or "",
            updated["price_version_id"] or "",
        ) == (member["group_id"], member["version_id"]):
            continue
        db.execute(
            "UPDATE reconciliation_member SET estimate_pico=?,effective_pico=?,group_id=?,version_id=?,"
            "incomplete=1,unpriced=?,revision=revision+1 WHERE harness=? AND dedupe_key=?",
            (
                str(updated["pico"]),
                str(updated["pico"]),
                updated["price_group_id"] or "",
                updated["price_version_id"] or "",
                int(updated["unpriced"]),
                *key,
            ),
        )
        refresh(db, *key, row["at"], member)
        estimate = db.execute(
            "SELECT * FROM session_estimate WHERE harness=? AND dedupe_key=?", key
        ).fetchone()
        if estimate is not None:
            delta = [0] * len(COUNTERS)
            delta[-2:] = [int(updated["pico"]) - int(estimate["pico"])] * 2
            change_counters(
                db,
                "session_total",
                ("harness", "session_ref"),
                (row["harness"], member["session_ref"] or ""),
                delta,
            )
            db.execute(
                "UPDATE session_estimate SET pico=?,effective_pico=?,group_id=?,version_id=?,"
                "revision=revision+1 WHERE harness=? AND dedupe_key=?",
                (
                    str(updated["pico"]),
                    str(updated["pico"]),
                    updated["price_group_id"] or "",
                    updated["price_version_id"] or "",
                    *key,
                ),
            )
    if rows:
        db.execute(
            "INSERT INTO runtime_state VALUES('session_recalc_cursor',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(rows[-1]["cursor"]),),
        )
    if len(rows) < limit or rows[-1]["cursor"] >= int(pending[0]):
        db.execute("DELETE FROM runtime_state WHERE key='session_recalc_until'")
        db.execute(
            "UPDATE runtime_state SET value=(SELECT CAST(COALESCE(MAX(rowid),0) AS TEXT) FROM recalc_item) "
            "WHERE key='session_recalc_cursor'"
        )
    return len(rows)
