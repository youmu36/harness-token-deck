"""Detect released writers through shared revisions and resume bounded evidence backfills."""

import sqlite3

from htd.adapters.sqlite.usage_codec import dumps


def install(db: sqlite3.Connection) -> None:
    revision = db.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0]
    marker = db.execute("SELECT value FROM runtime_state WHERE key='session_writer_revision'").fetchone()
    maximum = db.execute("SELECT COALESCE(MAX(rowid),0) FROM recalc_item").fetchone()[0]
    cursor = db.execute("SELECT value FROM runtime_state WHERE key='session_recalc_cursor'").fetchone()
    if cursor is None:
        db.execute("INSERT INTO runtime_state VALUES('session_recalc_cursor',?)", (str(maximum),))
    elif marker is not None and int(marker[0]) != revision and int(cursor[0]) < maximum:
        db.execute(
            "INSERT INTO runtime_state VALUES('session_recalc_until',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(maximum),),
        )
    if marker is not None and int(marker[0]) == revision:
        return
    # Never infer missing retired identities. Revisit only retained ledger evidence.
    state = dumps(
        {"harness": "", "key": "", "done": db.execute("SELECT 1 FROM ledger LIMIT 1").fetchone() is None}
    )
    db.executemany(
        "INSERT INTO runtime_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            (name, state)
            for name in (
                "session_directory_backfill",
                "session_cache_backfill",
                "reconciliation_backfill",
            )
        ),
    )
    db.execute(
        "INSERT INTO runtime_state VALUES('session_writer_revision',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(revision),),
    )
