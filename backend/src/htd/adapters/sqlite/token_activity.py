"""Shared ten-minute token activity evidence, with no extra timer or remote-poll input."""

import sqlite3
from datetime import timedelta

from htd.domain.common.time import Instant, format_instant
from htd.domain.usage.model import Harness

QUIET = timedelta(minutes=10)


def mark(db: sqlite3.Connection, harness: str, model: str, at: Instant) -> None:
    db.execute(
        "INSERT INTO token_activity VALUES(?,?,?) ON CONFLICT(harness,model_key) DO UPDATE SET "
        "last_received_at=MAX(token_activity.last_received_at,excluded.last_received_at)",
        (harness, model, format_instant(at)),
    )


def account_state(db: sqlite3.Connection, vendor: str) -> tuple[str | None, bool]:
    # Four indexed current selections, independent of the length of model/price history.
    sources = tuple(
        row[0]
        for row in db.execute(
            "WITH h(name) AS (VALUES(?),(?),(?),(?)) SELECT name FROM h WHERE "
            "(SELECT vendor_id FROM vendor_link WHERE group_id=(SELECT group_id FROM harness_selection "
            "WHERE harness=h.name ORDER BY effective_at DESC,seq DESC LIMIT 1) "
            "ORDER BY effective_at DESC,seq DESC LIMIT 1)=?",
            (*[h.value for h in Harness], vendor),
        )
    )
    if not sources:
        return None, False
    placeholders = ",".join("?" for _ in sources)
    last = db.execute(
        "SELECT MAX(last_received_at) FROM token_activity WHERE harness IN (" + placeholders + ")", sources
    ).fetchone()[0]
    busy = db.execute(
        "SELECT 1 FROM inbox WHERE harness IN (" + placeholders + ") LIMIT 1", sources
    ).fetchone()
    return last, busy is not None


def account_last(db: sqlite3.Connection, vendor: str) -> str | None:
    return account_state(db, vendor)[0]
