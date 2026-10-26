"""Sparse accepted-usage contributions; valuation transfers attribution without adding Token twice."""

import sqlite3

from htd.adapters.sqlite.key_codec import key_bytes
from htd.domain.usage.model import UsageEvent
from htd.domain.usage.model_key import model_key

COUNTERS = (
    "records",
    "input",
    "cache_read",
    "cache_write",
    "output",
    "reasoning",
    "missing_input",
    "missing_cache",
    "missing_output",
    "missing_write",
    "missing_reasoning",
    "pending",
    "estimate",
    "effective",
)
MINUTE_KEYS = ("harness", "session_ref", "bucket", "model_key", "group_id", "version_id", "vendor_id")


def install(db: sqlite3.Connection) -> None:
    columns = ",".join('"' + name + '" TEXT NOT NULL' for name in COUNTERS)
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_total(harness TEXT NOT NULL,session_ref TEXT NOT NULL,"
        + columns
        + ",PRIMARY KEY(harness,session_ref)) STRICT, WITHOUT ROWID"
    )
    dimensions = ",".join(name + " TEXT NOT NULL" for name in MINUTE_KEYS)
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_minute("
        + dimensions
        + ","
        + columns
        + ",PRIMARY KEY("
        + ",".join(MINUTE_KEYS)
        + ")) STRICT, WITHOUT ROWID"
    )
    db.execute("CREATE INDEX IF NOT EXISTS session_minute_range ON session_minute(bucket,harness)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_estimate(harness TEXT NOT NULL,dedupe_key BLOB NOT NULL,"
        "group_id TEXT NOT NULL,version_id TEXT NOT NULL,vendor_id TEXT NOT NULL,"
        "pico TEXT NOT NULL,effective_pico TEXT NOT NULL,revision INTEGER NOT NULL,"
        "PRIMARY KEY(harness,dedupe_key)) STRICT, WITHOUT ROWID"
    )


def change_counters(
    db: sqlite3.Connection, table: str, keys: tuple[str, ...], identity: tuple[str, ...], delta: list[int]
) -> None:
    where = " AND ".join(name + "=?" for name in keys)
    old = db.execute(
        "SELECT " + ",".join('"' + name + '"' for name in COUNTERS) + " FROM " + table + " WHERE " + where,
        identity,
    ).fetchone()
    values = [value + (int(old[index]) if old else 0) for index, value in enumerate(delta)]
    if any(value < 0 for value in values[:-2]):
        raise RuntimeError("Session contribution counter would become negative")
    if not any(values):
        db.execute("DELETE FROM " + table + " WHERE " + where, identity)
        return
    columns = ",".join([*keys, *('"' + name + '"' for name in COUNTERS)])
    db.execute(
        "INSERT OR REPLACE INTO "
        + table
        + "("
        + columns
        + ") VALUES("
        + ",".join("?" for _ in range(len(keys) + len(COUNTERS)))
        + ")",
        (*identity, *(str(value) for value in values)),
    )


def vector(event: UsageEvent) -> list[int]:
    tokens = event.tokens
    return [
        1,
        tokens.input or 0,
        tokens.cache_read or 0,
        tokens.cache_write or 0,
        tokens.output or 0,
        tokens.reasoning or 0,
        int(tokens.input is None),
        int(tokens.cache_read is None),
        int(tokens.output is None),
        int(tokens.cache_write is None),
        int(tokens.reasoning is None),
        1,
        0,
        0,
    ]


def _member(db: sqlite3.Connection, event: UsageEvent) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM session_request WHERE harness=? AND dedupe_key=?",
        (event.harness.value, key_bytes(event.dedupe_key)),
    ).fetchone()
    if row is None:
        raise RuntimeError("Accepted session membership must exist before projecting usage")
    return row


def minute_key(event: UsageEvent, ref: str, attribution: tuple[str, str, str]) -> tuple[str, ...]:
    return (
        event.harness.value,
        ref,
        event.occurred_at[:16] + ":00.000Z",
        model_key(event.model_id) if event.model_id else "",
        *attribution,
    )


def received(db: sqlite3.Connection, event: UsageEvent) -> None:
    from htd.adapters.sqlite.session_monitor import record

    record(db, event)
    row = _member(db, event)
    if row["projected_revision"]:
        return
    db.execute(
        "INSERT INTO usage_day_revision VALUES(?,?,1) ON CONFLICT(harness,day) DO UPDATE SET value=value+1",
        (event.harness.value, event.occurred_at[:10]),
    )
    values = vector(event)
    ref = row["session_ref"] or ""
    change_counters(db, "session_total", ("harness", "session_ref"), (event.harness.value, ref), values)
    change_counters(db, "session_minute", MINUTE_KEYS, minute_key(event, ref, ("", "", "")), values)
    db.execute(
        "UPDATE session_request SET projected_revision=revision WHERE harness=? AND dedupe_key=?",
        (event.harness.value, key_bytes(event.dedupe_key)),
    )


def valued(db: sqlite3.Connection, event: UsageEvent, pico: int, attribution: tuple[str, str, str]) -> None:
    from htd.adapters.sqlite.reconciliation_batches import valued as reconciliation_valued

    reconciliation_valued(db, event, pico, attribution)
    member = _member(db, event)
    identity = (event.harness.value, key_bytes(event.dedupe_key))
    prior = db.execute("SELECT * FROM session_estimate WHERE harness=? AND dedupe_key=?", identity).fetchone()
    if (
        prior is not None
        and int(prior["pico"]) == pico
        and attribution == (prior["group_id"], prior["version_id"], prior["vendor_id"])
    ):
        return
    ref = member["session_ref"] or ""
    if not member["projected_revision"]:
        received(db, event)
    if prior is None:
        original = vector(event)
        change_counters(
            db,
            "session_minute",
            MINUTE_KEYS,
            minute_key(event, ref, ("", "", "")),
            [-value for value in original],
        )
        revised = original[:]
        revised[-3:] = [0, pico, pico]
        change_counters(db, "session_minute", MINUTE_KEYS, minute_key(event, ref, attribution), revised)
        delta = [0] * len(COUNTERS)
        delta[-3:] = [-1, pico, pico]
    else:
        old_attribution = (prior["group_id"], prior["version_id"], prior["vendor_id"])
        if attribution != old_attribution:
            original = vector(event)
            original[-3:] = [0, int(prior["pico"]), int(prior["effective_pico"])]
            change_counters(
                db,
                "session_minute",
                MINUTE_KEYS,
                minute_key(event, ref, old_attribution),
                [-value for value in original],
            )
            change_counters(db, "session_minute", MINUTE_KEYS, minute_key(event, ref, attribution), original)
        delta = [0] * len(COUNTERS)
        delta[-2:] = [pico - int(prior["pico"]), pico - int(prior["pico"])]
        change_counters(db, "session_minute", MINUTE_KEYS, minute_key(event, ref, attribution), delta)
    change_counters(db, "session_total", ("harness", "session_ref"), (event.harness.value, ref), delta)
    db.execute(
        "INSERT INTO session_estimate VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(harness,dedupe_key) "
        "DO UPDATE SET pico=excluded.pico,effective_pico=excluded.effective_pico,revision=revision+1,"
        "group_id=excluded.group_id,version_id=excluded.version_id,vendor_id=excluded.vendor_id",
        (*identity, *attribution, str(pico), str(pico), 1),
    )


def cache_supplement(db: sqlite3.Connection, event: UsageEvent, cache: int) -> None:
    from htd.adapters.sqlite.session_monitor import cache_supplement as monitor_supplement

    monitor_supplement(db, event, cache)
    identity = (event.harness.value, key_bytes(event.dedupe_key))
    member = db.execute("SELECT * FROM session_request WHERE harness=? AND dedupe_key=?", identity).fetchone()
    if member is None or not member["projected_revision"]:
        return
    estimate = db.execute(
        "SELECT * FROM session_estimate WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    attribution = (
        (estimate["group_id"], estimate["version_id"], estimate["vendor_id"]) if estimate else ("", "", "")
    )
    ref = member["session_ref"] or ""
    delta = [0] * len(COUNTERS)
    delta[1:3] = [cache, cache]
    delta[7] = -1
    change_counters(db, "session_total", ("harness", "session_ref"), (event.harness.value, ref), delta)
    change_counters(db, "session_minute", MINUTE_KEYS, minute_key(event, ref, attribution), delta)
    db.execute(
        "UPDATE session_request SET revision=revision+1,projected_revision=revision+1 "
        "WHERE harness=? AND dedupe_key=?",
        identity,
    )
