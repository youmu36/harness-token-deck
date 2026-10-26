"""Additive durable session directory, verified aliases and request processing membership."""

import hashlib
import sqlite3
from dataclasses import replace

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.usage_codec import dumps, object_json
from htd.domain.common.time import format_instant, parse_instant
from htd.domain.usage.model import Harness, SourceKind, Tokens, UsageEvent, UsageStatus
from htd.domain.usage.session import UsageObservation, session_reference, source_session_key


def install(db: sqlite3.Connection) -> None:
    for statement in (
        "CREATE TABLE IF NOT EXISTS session_directory("
        "ref TEXT PRIMARY KEY,harness TEXT NOT NULL,native_key TEXT NOT NULL,native_identity TEXT NOT NULL,"
        "parent_ref TEXT REFERENCES session_directory(ref),started_at TEXT,last_received_at TEXT,"
        "identity_quality TEXT NOT NULL,UNIQUE(harness,native_key)) STRICT",
        "CREATE INDEX IF NOT EXISTS session_directory_activity "
        "ON session_directory(harness,last_received_at DESC,ref)",
        "CREATE TABLE IF NOT EXISTS session_alias(harness TEXT NOT NULL,alias TEXT NOT NULL,"
        "session_ref TEXT NOT NULL REFERENCES session_directory(ref),PRIMARY KEY(harness,alias,session_ref)) "
        "STRICT, WITHOUT ROWID",
        "CREATE TABLE IF NOT EXISTS session_identity_conflict("
        "harness TEXT NOT NULL,identity_key TEXT NOT NULL,"
        "reason TEXT NOT NULL,PRIMARY KEY(harness,identity_key,reason)) STRICT, WITHOUT ROWID",
        "CREATE TABLE IF NOT EXISTS session_request(harness TEXT NOT NULL,dedupe_key BLOB NOT NULL,"
        "session_ref TEXT REFERENCES session_directory(ref),"
        "occurred_at TEXT NOT NULL,received_at TEXT NOT NULL,"
        "origin TEXT NOT NULL,watermark TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,"
        "valued_revision INTEGER NOT NULL DEFAULT 0,projected_revision INTEGER NOT NULL DEFAULT 0,"
        "published_revision INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'accepted',"
        "PRIMARY KEY(harness,dedupe_key),"
        "FOREIGN KEY(harness,dedupe_key) REFERENCES identity(harness,dedupe_key)) "
        "STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS session_request_time "
        "ON session_request(session_ref,occurred_at,dedupe_key)",
        "CREATE INDEX IF NOT EXISTS session_request_pending ON session_request(state,received_at)",
        "CREATE INDEX IF NOT EXISTS session_request_harness_time "
        "ON session_request(harness,occurred_at,dedupe_key)",
    ):
        db.execute(statement)
    from htd.adapters.sqlite.session_activity import install as install_activity
    from htd.adapters.sqlite.session_projection import install as install_projection

    install_activity(db)
    install_projection(db)
    from htd.adapters.sqlite.session_monitor import install as install_monitor

    install_monitor(db)
    from htd.adapters.sqlite.reconciliation_batches import install as install_reconciliation

    install_reconciliation(db)
    from htd.adapters.sqlite.calibration_schema import install as install_calibration

    install_calibration(db)


def _conflict(db: sqlite3.Connection, harness: str, key: str, reason: str) -> None:
    db.execute("INSERT OR IGNORE INTO session_identity_conflict VALUES(?,?,?)", (harness, key, reason))


def directory(
    db: sqlite3.Connection,
    harness: Harness,
    native: str,
    *,
    occurred_at: str | None,
    received_at: str | None,
    quality: str = "native",
) -> str | None:
    ref, key = session_reference(harness, native), source_session_key(harness, native)
    old = db.execute(
        "SELECT ref,native_identity FROM session_directory WHERE (harness=? AND native_key=?) OR ref=?",
        (harness.value, key, ref),
    ).fetchone()
    if old is not None and (old[0] != ref or old[1] != native):
        _conflict(db, harness.value, key, "native_identity_collision")
        return None
    db.execute(
        "INSERT INTO session_directory(ref,harness,native_key,native_identity,started_at,last_received_at,"
        "identity_quality) VALUES(?,?,?,?,?,?,?) ON CONFLICT(ref) DO UPDATE SET "
        "started_at=CASE WHEN excluded.started_at IS NULL THEN started_at "
        "WHEN started_at IS NULL THEN excluded.started_at ELSE MIN(started_at,excluded.started_at) END,"
        "last_received_at=CASE WHEN excluded.last_received_at IS NULL THEN last_received_at "
        "WHEN last_received_at IS NULL THEN excluded.last_received_at "
        "ELSE MAX(last_received_at,excluded.last_received_at) END",
        (ref, harness.value, key, native, occurred_at, received_at, quality),
    )
    alias = hashlib.sha256(native.strip().encode("utf-8")).hexdigest()[:16]
    db.execute("INSERT OR IGNORE INTO session_alias VALUES(?,?,?)", (harness.value, alias, ref))
    if (
        db.execute(
            "SELECT COUNT(*) FROM session_alias WHERE harness=? AND alias=?", (harness.value, alias)
        ).fetchone()[0]
        > 1
    ):
        _conflict(db, harness.value, alias, "legacy_alias_collision")
    return ref


def observe(db: sqlite3.Connection, observation: UsageObservation, *, valued: bool = False) -> str | None:
    event = observation.event
    identity = (event.harness.value, key_bytes(event.dedupe_key))
    previous = db.execute(
        "SELECT session_ref FROM session_request WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    if previous is not None:
        return previous[0]
    native = event.native_session_id
    received = format_instant(observation.received_at)
    ref = (
        None
        if native is None
        else directory(
            db,
            event.harness,
            native,
            occurred_at=event.occurred_at,
            received_at=received,
            quality="legacy" if observation.origin == "legacy" else "native",
        )
    )
    if ref is not None and event.parent_session_id is not None and event.parent_session_id != native:
        parent = directory(db, event.harness, event.parent_session_id, occurred_at=None, received_at=None)
        if parent is not None:
            cyclic = db.execute(
                "WITH RECURSIVE ancestors(ref) AS (SELECT ? UNION "
                "SELECT s.parent_ref FROM session_directory s JOIN ancestors a ON s.ref=a.ref "
                "WHERE s.parent_ref IS NOT NULL) SELECT 1 FROM ancestors WHERE ref=?",
                (parent, ref),
            ).fetchone()
            old_parent = db.execute(
                "SELECT parent_ref FROM session_directory WHERE ref=?", (ref,)
            ).fetchone()[0]
            if cyclic or old_parent not in (None, parent):
                _conflict(db, event.harness.value, ref, "parent_evidence_conflict")
            else:
                db.execute("UPDATE session_directory SET parent_ref=? WHERE ref=?", (parent, ref))
    db.execute(
        "INSERT INTO session_request(harness,dedupe_key,session_ref,occurred_at,received_at,origin,watermark,"
        "valued_revision,state) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            *identity,
            ref,
            event.occurred_at,
            received,
            observation.origin,
            observation.watermark,
            int(valued),
            "billed" if valued else "accepted",
        ),
    )
    return ref


def ledger_event(row: sqlite3.Row) -> UsageEvent:
    from typing import cast

    return UsageEvent(
        Harness(row["harness"]),
        str(row["dedupe_key"]),
        row["session_id"],
        row["request_id"],
        row["model_id"],
        row["occurred_at"],
        Tokens(*(row[name] for name in ("input", "cache_read", "cache_write", "output", "reasoning"))),
        cast(UsageStatus, row["status"]),
        (),
        cast(SourceKind | None, row["source_kind"]),
    )


def remember_ledger(db: sqlite3.Connection, row: sqlite3.Row) -> None:
    event = ledger_event(row)
    annotation = db.execute(
        "SELECT cache_read FROM usage_annotation WHERE harness=? AND dedupe_key=?",
        (event.harness.value, key_bytes(event.dedupe_key)),
    ).fetchone()
    if event.tokens.cache_read is None and annotation is not None and annotation[0] is not None:
        tokens = event.tokens
        event = replace(
            event,
            tokens=Tokens(
                tokens.input + annotation[0] if tokens.input is not None else None,
                annotation[0],
                tokens.cache_write,
                tokens.output,
                tokens.reasoning,
            ),
        )
    observed = db.execute(
        "SELECT first_seen_at,origin FROM identity WHERE harness=? AND dedupe_key=?",
        (row["harness"], key_bytes(row["dedupe_key"])),
    ).fetchone()
    if observed is not None:
        observe(
            db, UsageObservation(event, parse_instant(observed[0]), "legacy-ledger", "legacy"), valued=True
        )
        from htd.adapters.sqlite.session_monitor import record

        record(db, event)
        from htd.adapters.sqlite.session_projection import valued
        from htd.adapters.sqlite.usage_attribution import vendor_for_request

        vendor = vendor_for_request(
            db, event.harness.value, key_bytes(event.dedupe_key), row["price_group_id"], event.occurred_at
        )
        valued(
            db, event, int(row["pico"]), (row["price_group_id"] or "", row["price_version_id"] or "", vendor)
        )


def backfill(db: sqlite3.Connection, *, limit: int = 500) -> int:
    saved = db.execute("SELECT value FROM runtime_state WHERE key='session_directory_backfill'").fetchone()
    state = object_json(saved[0]) if saved else {"harness": "", "key": "", "done": False}
    if state["done"]:
        return 0
    rows = db.execute(
        "SELECT * FROM ledger WHERE (harness,dedupe_key)>(?,?) ORDER BY harness,dedupe_key LIMIT ?",
        (state["harness"], bytes.fromhex(str(state["key"])), limit),
    ).fetchall()
    for row in rows:
        remember_ledger(db, row)
    if rows:
        state.update({"harness": rows[-1]["harness"], "key": str(rows[-1]["dedupe_key"])})
    state["done"] = len(rows) < limit
    db.execute(
        "INSERT INTO runtime_state VALUES('session_directory_backfill',?) ON CONFLICT(key) "
        "DO UPDATE SET value=excluded.value",
        (dumps(state),),
    )
    return len(rows)
