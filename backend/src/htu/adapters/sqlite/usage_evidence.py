"""Idempotent late source annotations, separate from immutable billing facts."""

import sqlite3

from htu.adapters.sqlite.key_codec import key_bytes
from htu.application.ports import UnitOfWork, UsageChanged
from htu.domain.usage.model import Harness, UsageEvent


def mark_compacted(db: sqlite3.Connection, work: UnitOfWork, key: str) -> None:
    identity = db.execute(
        "SELECT occurred_at FROM identity WHERE harness='codex' AND dedupe_key=?", (key_bytes(key),)
    ).fetchone()
    if identity is None:
        return
    changed = db.execute(
        "INSERT INTO usage_annotation VALUES('codex',?,1,NULL) "
        "ON CONFLICT(harness,dedupe_key) DO UPDATE SET compacted=1 WHERE compacted=0",
        (key_bytes(key),),
    ).rowcount
    if changed:
        db.execute(
            "INSERT INTO usage_day_revision VALUES('codex',?,1) ON CONFLICT(harness,day) "
            "DO UPDATE SET value=value+1",
            (identity["occurred_at"][:10],),
        )
        work.touch("usage")
        work.emit(UsageChanged((Harness.CODEX,), work.revision("usage")))


def supplement(db: sqlite3.Connection, work: UnitOfWork, event: UsageEvent, *, matching: bool) -> bool:
    identity = event.harness.value, key_bytes(event.dedupe_key)
    cache: int | None = None
    compatible = matching
    if not matching and event.harness == Harness.DSH and event.tokens.cache_read is not None:
        old = db.execute("SELECT * FROM ledger WHERE harness=? AND dedupe_key=?", identity).fetchone()
        tokens = event.tokens
        cache = tokens.cache_read
        assert cache is not None
        compatible = old is not None and bool(
            old["cache_read"] is None
            and tokens.input is not None
            and old["input"] == tokens.input - cache
            and old["output"] == tokens.output
            and old["cache_write"] == tokens.cache_write
            and old["reasoning"] == tokens.reasoning
            and old["model_id"] == event.model_id
            and old["session_id"] == event.session_id
            and old["request_id"] == event.request_id
            and old["occurred_at"] == event.occurred_at
        )
        if not compatible:
            cache = None
    compacted = int(compatible and "context_compacted" in event.issues)
    if not compacted and cache is None:
        return compatible
    changed = db.execute(
        "INSERT INTO usage_annotation VALUES(?,?,?,?) ON CONFLICT(harness,dedupe_key) DO UPDATE SET "
        "compacted=MAX(usage_annotation.compacted,excluded.compacted),"
        "cache_read=COALESCE(usage_annotation.cache_read,excluded.cache_read) "
        "WHERE excluded.compacted>usage_annotation.compacted OR "
        "(usage_annotation.cache_read IS NULL AND excluded.cache_read IS NOT NULL)",
        (*identity, compacted, cache),
    ).rowcount
    if changed:
        db.execute(
            "INSERT INTO usage_day_revision VALUES(?,?,1) ON CONFLICT(harness,day) "
            "DO UPDATE SET value=value+1",
            (event.harness.value, event.occurred_at[:10]),
        )
        work.touch("usage")
        work.emit(UsageChanged((event.harness,), work.revision("usage")))
    return compatible
