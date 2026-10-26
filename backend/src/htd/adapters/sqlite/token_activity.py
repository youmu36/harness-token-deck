"""Shared ten-minute token activity evidence, with no extra timer or remote-poll input."""

import sqlite3

from htd.domain.common.time import Instant, format_instant
from htd.domain.usage.activity import QUIET as QUIET
from htd.domain.usage.model import Harness


def mark(db: sqlite3.Connection, harness: str, model: str, at: Instant) -> None:
    db.execute(
        "INSERT INTO token_activity VALUES(?,?,?) ON CONFLICT(harness,model_key) DO UPDATE SET "
        "last_received_at=MAX(token_activity.last_received_at,excluded.last_received_at)",
        (harness, model, format_instant(at)),
    )


def account_state(db: sqlite3.Connection, vendor: str) -> tuple[str | None, bool]:
    actual = db.execute(
        "WITH h(name) AS (VALUES('claude'),('codex'),('zcode'),('dsh')) SELECT name FROM h "
        "WHERE EXISTS(SELECT 1 FROM reconciliation_member m WHERE m.vendor_id=? AND m.harness=h.name)",
        (vendor,),
    ).fetchall()
    # Four indexed current selections, independent of the length of model/price history.
    sources = (
        tuple(row[0] for row in actual)
        if actual
        else tuple(
            row[0]
            for row in db.execute(
                "WITH h(name) AS (VALUES(?),(?),(?),(?)) SELECT name FROM h WHERE "
                "(SELECT vendor_id FROM vendor_link WHERE group_id=(SELECT group_id FROM harness_selection "
                "WHERE harness=h.name ORDER BY effective_at DESC,seq DESC LIMIT 1) "
                "ORDER BY effective_at DESC,seq DESC LIMIT 1)=?",
                (*[h.value for h in Harness], vendor),
            )
        )
    )
    if not sources:
        return None, False
    placeholders = ",".join("?" for _ in sources)
    last = (
        db.execute(
            "SELECT received_at FROM reconciliation_member WHERE vendor_id=? "
            "ORDER BY received_at DESC LIMIT 1",
            (vendor,),
        ).fetchone()[0]
        if actual
        else db.execute(
            "SELECT MAX(last_received_at) FROM token_activity WHERE harness IN (" + placeholders + ")",
            sources,
        ).fetchone()[0]
    )
    busy = (
        db.execute(
            "SELECT 1 FROM inbox b WHERE (SELECT vl.vendor_id FROM vendor_link vl WHERE vl.group_id="
            "(SELECT hs.group_id FROM harness_selection hs WHERE hs.harness=b.harness "
            "AND hs.effective_at<=b.occurred_at ORDER BY hs.effective_at DESC,hs.seq DESC LIMIT 1) "
            "AND vl.effective_at<=b.occurred_at ORDER BY vl.effective_at DESC,vl.seq DESC LIMIT 1)=? LIMIT 1",
            (vendor,),
        ).fetchone()
        if actual
        else db.execute(
            "SELECT 1 FROM inbox WHERE harness IN (" + placeholders + ") LIMIT 1", sources
        ).fetchone()
    )
    paused = db.execute("SELECT value FROM runtime_state WHERE key='collection_paused'").fetchone()
    disabled = db.execute(
        "SELECT 1 FROM runtime_state WHERE value='false' AND key IN ("
        + ",".join("?" for _ in sources)
        + ") LIMIT 1",
        tuple("scan_enabled." + source for source in sources),
    ).fetchone()
    incomplete = db.execute(
        "SELECT 1 FROM source_visibility WHERE harness IN (" + placeholders + ") AND state<>'ready' LIMIT 1",
        sources,
    ).fetchone()
    weak = db.execute(
        "SELECT 1 FROM session_weak_activity w JOIN session_directory s ON s.ref=w.session_ref "
        "WHERE s.harness IN (" + placeholders + ") AND w.weak=1 LIMIT 1",
        sources,
    ).fetchone()
    return last, busy is not None or bool(paused and paused[0] == "true") or any(
        value is not None for value in (disabled, incomplete, weak)
    )


def account_last(db: sqlite3.Connection, vendor: str) -> str | None:
    return account_state(db, vendor)[0]
