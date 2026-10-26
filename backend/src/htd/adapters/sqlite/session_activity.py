"""Persistent activity batches; source visibility is evidence, never a resettable UI timer."""

import sqlite3
from uuid import UUID, uuid5

from htd.adapters.sqlite.key_codec import key_bytes
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.activity import QUIET, activity_state
from htd.domain.usage.session import SourceObservation, UsageObservation


def install(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS source_visibility(harness TEXT PRIMARY KEY,state TEXT NOT NULL,"
        "observed_at TEXT NOT NULL,watermark TEXT) STRICT"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_activity_batch(id TEXT PRIMARY KEY,session_ref TEXT NOT NULL "
        "REFERENCES session_directory(ref),started_at TEXT NOT NULL,last_occurred_at TEXT NOT NULL,"
        "last_received_at TEXT NOT NULL,state TEXT NOT NULL,sealed_at TEXT,revision INTEGER NOT NULL) STRICT"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS session_activity_open "
        "ON session_activity_batch(session_ref) WHERE state='open'"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS session_activity_due ON session_activity_batch(state,last_received_at)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_request_batch(harness TEXT NOT NULL,dedupe_key BLOB NOT NULL,"
        "batch_id TEXT NOT NULL REFERENCES session_activity_batch(id),"
        "PRIMARY KEY(harness,dedupe_key)) STRICT, WITHOUT ROWID"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_weak_activity(session_ref TEXT NOT NULL,source_key TEXT NOT NULL,"
        "weak INTEGER NOT NULL,checked_at TEXT NOT NULL,"
        "PRIMARY KEY(session_ref,source_key)) STRICT, WITHOUT ROWID"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS session_request_batch_members "
        "ON session_request_batch(batch_id,harness,dedupe_key)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_activity_boundary(batch_id TEXT PRIMARY KEY "
        "REFERENCES session_activity_batch(id),source_complete INTEGER NOT NULL,"
        "recorded_at TEXT NOT NULL) STRICT"
    )


def source_observed(db: sqlite3.Connection, observation: SourceObservation) -> None:
    db.execute(
        "INSERT INTO source_visibility VALUES(?,?,?,?) ON CONFLICT(harness) DO UPDATE SET "
        "state=excluded.state,observed_at=excluded.observed_at,watermark=excluded.watermark",
        (
            observation.harness.value,
            observation.state,
            format_instant(observation.observed_at),
            observation.watermark,
        ),
    )


def received(db: sqlite3.Connection, observation: UsageObservation, ref: str | None) -> None:
    if ref is None:
        return
    event, at = observation.event, observation.received_at
    identity = (event.harness.value, key_bytes(event.dedupe_key))
    if db.execute(
        "SELECT 1 FROM session_request_batch WHERE harness=? AND dedupe_key=?", identity
    ).fetchone():
        return
    current = db.execute(
        "SELECT * FROM session_activity_batch WHERE session_ref=? AND state='open'", (ref,)
    ).fetchone()
    live = observation.origin == "live" and parse_instant(event.occurred_at).value >= at.value - QUIET
    if current is not None and event.occurred_at < current["last_occurred_at"]:
        live = False
    if not live:
        old = db.execute(
            "SELECT id FROM session_activity_batch WHERE session_ref=? AND started_at<=? "
            "ORDER BY started_at DESC LIMIT 1",
            (ref, event.occurred_at),
        ).fetchone()
        if old is not None:
            db.execute("INSERT INTO session_request_batch VALUES(?,?,?)", (*identity, old[0]))
            db.execute("UPDATE session_activity_batch SET revision=revision+1 WHERE id=?", (old[0],))
        return
    stamp = format_instant(at)
    if current is None:
        batch_id = str(uuid5(UUID(ref), event.dedupe_key))
        db.execute(
            "INSERT INTO session_activity_batch VALUES(?,?,?,?,?,'open',NULL,1)",
            (batch_id, ref, event.occurred_at, event.occurred_at, stamp),
        )
    else:
        batch_id = current["id"]
        db.execute(
            "UPDATE session_activity_batch SET last_occurred_at=MAX(last_occurred_at,?),"
            "last_received_at=MAX(last_received_at,?),revision=revision+1 WHERE id=?",
            (event.occurred_at, stamp, batch_id),
        )
    db.execute("INSERT INTO session_request_batch VALUES(?,?,?)", (*identity, batch_id))


def complete_evidence(db: sqlite3.Connection, harness: str, ref: str) -> bool:
    paused = db.execute("SELECT value FROM runtime_state WHERE key='collection_paused'").fetchone()
    enabled = db.execute(
        "SELECT value FROM runtime_state WHERE key=?", ("scan_enabled." + harness,)
    ).fetchone()
    if (paused and paused[0] == "true") or (enabled and enabled[0] == "false"):
        return False
    source = db.execute("SELECT state FROM source_visibility WHERE harness=?", (harness,)).fetchone()
    weak = db.execute(
        "SELECT 1 FROM session_weak_activity WHERE session_ref=? AND weak=1 LIMIT 1", (ref,)
    ).fetchone()
    return source is not None and source[0] == "ready" and weak is None


def begin_run(db: sqlite3.Connection, at: Instant) -> None:
    while True:
        rows = db.execute(
            "SELECT b.id,b.session_ref,d.harness FROM session_activity_batch b "
            "JOIN session_directory d ON d.ref=b.session_ref WHERE b.state='open' LIMIT 500"
        ).fetchall()
        if not rows:
            break
        for row in rows:
            db.execute(
                "INSERT OR IGNORE INTO session_activity_boundary VALUES(?,?,?)",
                (row[0], int(complete_evidence(db, row[2], row[1])), format_instant(at)),
            )
            db.execute(
                "UPDATE session_activity_batch SET state='recovering',revision=revision+1 WHERE id=?",
                (row[0],),
            )
    db.execute("UPDATE source_visibility SET state='pending' WHERE state IN ('ready','reading')")


def seal(db: sqlite3.Connection, at: Instant, *, limit: int = 500) -> int:
    rows = db.execute(
        "SELECT b.id,b.session_ref,b.last_received_at,d.harness,COALESCE(r.source_complete,0) complete "
        "FROM session_activity_batch b JOIN session_directory d ON d.ref=b.session_ref "
        "LEFT JOIN session_activity_boundary r ON r.batch_id=b.id WHERE b.state IN ('open','recovering') "
        "AND b.last_received_at<=? ORDER BY b.last_received_at LIMIT ?",
        (format_instant(Instant(at.value - QUIET)), limit),
    ).fetchall()
    count = 0
    for row in rows:
        pending = db.execute(
            "SELECT 1 FROM session_request r JOIN session_request_batch b USING(harness,dedupe_key) "
            "WHERE b.batch_id=? AND r.state='accepted' LIMIT 1",
            (row[0],),
        ).fetchone()
        if (
            activity_state(
                parse_instant(row[2]),
                at,
                complete=bool(row[4]) or complete_evidence(db, row[3], row[1]),
                pending=pending is not None,
            )
            == "quiet"
        ):
            db.execute(
                "UPDATE session_activity_batch SET state='sealed',sealed_at=?,revision=revision+1 WHERE id=?",
                (format_instant(at), row[0]),
            )
            count += 1
    return count
