"""Use committed shared revisions; avoid per-request writes for a small read cache."""

import sqlite3
from typing import Any

from htd.domain.usage.model import Harness


def install(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_monitor_revision("
        "harness TEXT PRIMARY KEY,value INTEGER NOT NULL) STRICT, WITHOUT ROWID"
    )
    db.executemany(
        "INSERT OR IGNORE INTO session_monitor_revision VALUES(?,0)",
        ((harness.value,) for harness in Harness),
    )
    # Keep the four-row table readable by previous 0.5.0 test builds. It no longer
    # participates in cache keys. Remove exactly our triggers on every activation,
    # including after an older binary recreated them during a downgrade.
    for table in (
        "session_directory",
        "session_alias",
        "session_minute",
        "session_cache_total",
        "session_activity_batch",
        "session_weak_activity",
        "output_session",
    ):
        for event in ("INSERT", "UPDATE", "DELETE"):
            db.execute(f"DROP TRIGGER IF EXISTS monitor_{table}_{event.lower()}")


def signature(db: sqlite3.Connection, harness: str) -> tuple[Any, ...]:
    return tuple(
        db.execute(
            "SELECT (SELECT value FROM revision WHERE scope='usage'),"
            "(SELECT value FROM revision WHERE scope='output'),"
            "(SELECT value FROM revision WHERE scope='sources'),"
            "(SELECT state FROM source_visibility WHERE harness=?),"
            "(SELECT value FROM runtime_state WHERE key='collection_paused'),"
            "(SELECT value FROM runtime_state WHERE key=?)",
            (harness, "scan_enabled." + harness),
        ).fetchone()
    )
