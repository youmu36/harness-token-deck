"""Sparse request warnings and per-session cache projections, read in one WAL snapshot."""

import sqlite3
from datetime import timedelta
from typing import Any

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.session_state import frozen, session_key
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.cache_warnings import cache_warnings
from htd.domain.statistics.window import align, bucket_key, bucket_label, monitor_window, next_minute
from htd.domain.usage.model import Harness, UsageEvent
from htd.domain.usage.model_key import model_key
from htd.domain.usage.session import RECENT_SESSION_IDLE

COUNTERS = (
    "records",
    "input",
    "output",
    "cache",
    "rated_input",
    "rated_cache",
    "missing",
    "bust",
    "loss",
    "compacted",
)


def install(db: sqlite3.Connection) -> None:
    columns = ",".join('"' + name + '" TEXT NOT NULL' for name in COUNTERS)
    for table, dimensions in (
        ("session_cache_total", ("session_ref",)),
        ("session_cache_minute", ("session_ref", "bucket")),
        ("session_cache_batch", ("session_ref", "batch_id")),
    ):
        db.execute(
            "CREATE TABLE IF NOT EXISTS "
            + table
            + "("
            + ",".join(name + " TEXT NOT NULL" for name in dimensions)
            + ","
            + columns
            + ",PRIMARY KEY("
            + ",".join(dimensions)
            + ")) STRICT, WITHOUT ROWID"
        )
    db.execute(
        "CREATE TABLE IF NOT EXISTS session_cache_warning(harness TEXT NOT NULL,dedupe_key BLOB NOT "
        "NULL,session_ref TEXT NOT NULL,bucket TEXT NOT NULL,batch_id TEXT,bust INTEGER NOT "
        "NULL,loss INTEGER NOT NULL,compacted INTEGER NOT NULL,PRIMARY KEY(harness,dedupe_key)) "
        "STRICT, WITHOUT ROWID"
    )
    db.execute("CREATE INDEX IF NOT EXISTS session_cache_warning_time ON session_cache_warning(bucket)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS session_activity_session ON "
        "session_activity_batch(session_ref,last_received_at DESC)"
    )


def _change(db: sqlite3.Connection, ref: str, bucket: str, batch: str | None, delta: list[int]) -> None:
    for table, keys, values in (
        ("session_cache_total", ("session_ref",), (ref,)),
        ("session_cache_minute", ("session_ref", "bucket"), (ref, bucket)),
    ):
        where = " AND ".join(name + "=?" for name in keys)
        old = db.execute(
            "SELECT "
            + ",".join('"' + name + '"' for name in COUNTERS)
            + " FROM "
            + table
            + " WHERE "
            + where,
            values,
        ).fetchone()
        totals = [number + (int(old[index]) if old else 0) for index, number in enumerate(delta)]
        if min(totals) < 0:
            raise RuntimeError("Cache projection would become negative")
        db.execute(
            "INSERT OR REPLACE INTO "
            + table
            + " VALUES("
            + ",".join("?" for _ in range(len(keys) + len(COUNTERS)))
            + ")",
            (*values, *(str(value) for value in totals)),
        )


def _warnings(event: UsageEvent, compacted: bool) -> tuple[int, int, int]:
    unprobed = (
        event.harness == Harness.ZCODE
        and "grok" in model_key(event.model_id or "unknown")
        and event.tokens.cache_read == 0
    )
    bust, loss = cache_warnings(
        event.tokens.input, event.tokens.cache_read, session=True, unprobed=unprobed, compacted=compacted
    )
    return int(bust), int(loss), int(compacted)


def record(db: sqlite3.Connection, event: UsageEvent) -> None:
    identity = event.harness.value, key_bytes(event.dedupe_key)
    if db.execute(
        "SELECT 1 FROM session_cache_warning WHERE harness=? AND dedupe_key=?", identity
    ).fetchone():
        return
    member = db.execute(
        "SELECT session_ref FROM session_request WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    if member is None or member[0] is None:
        return
    ref, bucket = member[0], event.occurred_at[:16] + ":00.000Z"
    batch = db.execute(
        "SELECT batch_id FROM session_request_batch WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    batch_id = batch[0] if batch else None
    annotation = db.execute(
        "SELECT compacted FROM usage_annotation WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    flags = _warnings(event, "context_compacted" in event.issues or bool(annotation and annotation[0]))
    tokens = event.tokens
    eligible = (
        tokens.input is not None
        and tokens.cache_read is not None
        and not (
            event.harness == Harness.ZCODE
            and "grok" in model_key(event.model_id or "unknown")
            and tokens.cache_read == 0
        )
    )
    delta = [
        1,
        tokens.input or 0,
        tokens.output or 0,
        tokens.cache_read or 0,
        (tokens.input or 0) if eligible else 0,
        (tokens.cache_read or 0) if eligible else 0,
        int(tokens.input is None or tokens.cache_read is None),
        *flags,
    ]
    _change(db, ref, bucket, batch_id, delta)
    db.execute(
        "INSERT INTO session_cache_warning VALUES(?,?,?,?,?,?,?,?)",
        (*identity, ref, bucket, batch_id, *flags),
    )


def compacted(db: sqlite3.Connection, key: str) -> None:
    row = db.execute(
        "SELECT * FROM session_cache_warning WHERE harness='codex' AND dedupe_key=?", (key_bytes(key),)
    ).fetchone()
    if row is None or row["compacted"]:
        return
    delta = [0] * len(COUNTERS)
    delta[-3:] = [-row["bust"], -row["loss"], 1]
    _change(db, row["session_ref"], row["bucket"], row["batch_id"], delta)
    db.execute(
        "UPDATE session_cache_warning SET bust=0,loss=0,compacted=1 WHERE harness='codex' AND dedupe_key=?",
        (key_bytes(key),),
    )


def cache_supplement(db: sqlite3.Connection, event: UsageEvent, cache: int) -> None:
    identity = event.harness.value, key_bytes(event.dedupe_key)
    row = db.execute(
        "SELECT * FROM session_cache_warning WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    if row is None:
        return
    flags = _warnings(event, bool(row["compacted"]))
    delta = [
        0,
        cache,
        0,
        cache,
        event.tokens.input or 0,
        cache,
        -1,
        *(flags[index] - row[name] for index, name in enumerate(("bust", "loss", "compacted"))),
    ]
    _change(db, row["session_ref"], row["bucket"], row["batch_id"], delta)
    db.execute(
        "UPDATE session_cache_warning SET bust=?,loss=?,compacted=? WHERE harness=? AND dedupe_key=?",
        (*flags, *identity),
    )


def backfill(db: sqlite3.Connection, *, limit: int = 500) -> int:
    from htd.adapters.sqlite.session_core import remember_ledger
    from htd.adapters.sqlite.usage_codec import dumps, object_json

    saved = db.execute("SELECT value FROM runtime_state WHERE key='session_cache_backfill'").fetchone()
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
        state.update(harness=rows[-1]["harness"], key=str(rows[-1]["dedupe_key"]))
    state["done"] = len(rows) < limit
    db.execute(
        "INSERT INTO runtime_state VALUES('session_cache_backfill',?) ON CONFLICT(key) "
        "DO UPDATE SET value=excluded.value",
        (dumps(state),),
    )
    return len(rows)


def _totals(row: sqlite3.Row | list[int] | None) -> dict[str, Any]:
    values = dict(zip(COUNTERS, (int(value) for value in row) if row else [0] * len(COUNTERS), strict=True))
    return {
        "recordCount": str(values["records"]),
        "inputTokens": str(values["input"]),
        "outputTokens": str(values["output"]),
        "cacheReadTokens": str(values["cache"]),
        "cacheHitRate": values["rated_cache"] / values["rated_input"] if values["rated_input"] else None,
        "cacheCoverageIncomplete": bool(values["missing"]),
    }


def read(
    db: sqlite3.Connection,
    harness: str,
    now: Instant,
    session_ref: str | None = None,
    offset_minutes: int = 0,
    cursor: str | None = None,
    chart_session_ref: str | None = None,
) -> dict[str, Any]:
    window = monitor_window(now, "minute", offset_minutes)
    after: tuple[str, ...] = ()
    clause = ""
    if cursor:
        stamp, separator, ref = cursor.partition("|")
        if not separator:
            raise DomainError("INVALID_CURSOR", "会话分页游标无效")
        parse_instant(stamp)
        clause = " AND (last_received_at<? OR (last_received_at=? AND ref>?))"
        after = (stamp, stamp, ref)
    page = db.execute(
        "SELECT *,COALESCE((SELECT a.last_received_at FROM session_activity_batch a "
        "WHERE a.session_ref=ref ORDER BY a.last_received_at DESC LIMIT 1),started_at) activity_at "
        "FROM session_directory WHERE harness=? AND started_at IS NOT NULL AND last_received_at>? "
        "AND COALESCE((SELECT a.last_received_at FROM session_activity_batch a "
        "WHERE a.session_ref=ref ORDER BY a.last_received_at DESC LIMIT 1),started_at)>?"
        + clause
        + " ORDER BY last_received_at DESC,ref LIMIT 51",
        (harness, *(format_instant(Instant(now.value - RECENT_SESSION_IDLE)),) * 2, *after),
    ).fetchall()
    next_cursor = page[49]["last_received_at"] + "|" + page[49]["ref"] if len(page) > 50 else None
    page = page[:50]
    for requested in dict.fromkeys(value for value in (session_ref, chart_session_ref) if value):
        if any(row["ref"] == requested for row in page):
            continue
        selected = db.execute(
            "SELECT *,COALESCE((SELECT a.last_received_at FROM session_activity_batch a "
            "WHERE a.session_ref=ref ORDER BY a.last_received_at DESC LIMIT 1),started_at) activity_at "
            "FROM session_directory WHERE harness=? AND ref=? AND started_at IS NOT NULL "
            "AND last_received_at>? AND COALESCE((SELECT a.last_received_at FROM session_activity_batch a "
            "WHERE a.session_ref=ref ORDER BY a.last_received_at DESC LIMIT 1),started_at)>?",
            (harness, requested, *(format_instant(Instant(now.value - RECENT_SESSION_IDLE)),) * 2),
        ).fetchone()
        if selected is not None:
            page.append(selected)
    sessions: list[dict[str, Any]] = []
    from htd.adapters.sqlite.output_speed import rate_ms

    # One bounded projection for all directory entries, without decoding their TPS windows.
    aliases = [(row["ref"], session_key(row["native_identity"])) for row in page]
    details = (
        {
            row["ref"]: row
            for row in db.execute(
                "WITH requested(ref,alias) AS (VALUES " + ",".join("(?,?)" for _ in aliases) + ") "
                "SELECT r.ref,c.*,(SELECT model_key FROM session_minute m WHERE m.harness=? "
                "AND m.session_ref=r.ref ORDER BY bucket DESC LIMIT 1) latest_model,"
                "s.model_key speed_model,s.display_name speed_name,s.output_tokens speed_output,"
                "s.duration_ms speed_duration FROM requested r "
                "LEFT JOIN session_cache_total c ON c.session_ref=r.ref "
                "LEFT JOIN output_session s ON s.harness=? AND s.session_key=r.alias "
                "AND s.model_key=(SELECT model_key FROM output_session o WHERE o.harness=s.harness "
                "AND o.session_key=r.alias ORDER BY last_received_at DESC,model_key LIMIT 1)",
                (*[value for pair in aliases for value in pair], harness, harness),
            )
        }
        if aliases
        else {}
    )
    for row in page:
        ref = row["ref"]
        last = row["activity_at"]
        values = details[ref]
        total = _totals([values[name] or 0 for name in COUNTERS])
        alias = session_key(row["native_identity"])
        latest_model = (
            values["latest_model"] if values["latest_model"] is not None else values["speed_model"] or ""
        )
        has_speed = values["speed_model"] is not None and values["speed_model"] == latest_model
        sessions.append(
            {
                "sessionRef": ref,
                "sessionKey": alias,
                "displayName": "会话",
                "startedAt": row["started_at"],
                "lastActivityAt": last,
                "cacheHitRate": total["cacheHitRate"],
                "cacheCoverageIncomplete": total["cacheCoverageIncomplete"],
                "frozen": frozen(last, now),
                "modelKey": latest_model,
                "modelName": values["speed_name"] if has_speed else latest_model or "未知模型",
                "tokensPerSecond": None,
                "sessionAverageTps": rate_ms(int(values["speed_output"]), int(values["speed_duration"]))
                if has_speed
                else None,
            }
        )
    selected = next((row for row in sessions if row["sessionRef"] == session_ref), None) or max(
        sessions, key=lambda row: (not row["frozen"], row["lastActivityAt"], row["sessionRef"]), default=None
    )
    ref = selected["sessionRef"] if selected else None
    chart = next((row for row in sessions if row["sessionRef"] == chart_session_ref), None) or min(
        sessions, key=lambda row: (row["startedAt"], row["sessionRef"]), default=None
    )
    chart_ref = chart["sessionRef"] if chart else None
    if chart_session_ref is None and session_ref is not None:
        chart_ref = ref
    buckets = (
        {
            row["bucket"]: [int(row[name]) for name in COUNTERS]
            for row in db.execute(
                "SELECT * FROM session_cache_minute WHERE session_ref=? AND bucket>=? AND bucket<? "
                "ORDER BY bucket",
                (chart_ref, format_instant(window.start), format_instant(window.end)),
            )
        }
        if chart_ref
        else {}
    )
    window_values = [sum(value[index] for value in buckets.values()) for index in range(len(COUNTERS))]
    trend: list[dict[str, Any]] = []
    for index in range(60):
        stamp = Instant(window.start.value + timedelta(minutes=index))
        values = buckets.get(format_instant(stamp), [0] * len(COUNTERS))
        if max(values[:6]) > 2**53 - 1:
            raise DomainError("COUNT_OVERFLOW", "监控分钟计数超过安全整数范围")
        bucket = align(stamp, offset_minutes, "minute")
        trend.append(
            {
                "bucket": bucket_key(bucket),
                "label": bucket_label(bucket, "minute"),
                "inputTokens": values[1],
                "outputTokens": values[2],
                "cacheReadTokens": values[3],
                "cacheHitRate": values[5] / values[4] if values[4] else None,
                "cacheBust": bool(values[7]),
                "cacheLoss": bool(values[8]),
                "contextCompacted": bool(values[9]),
                "picoUsd": "0",
                "recordCount": values[0],
            }
        )
    lifetime = [details[ref][name] or 0 for name in COUNTERS] if ref else None
    from htd.adapters.sqlite.session_activity import complete_evidence
    from htd.adapters.sqlite.session_speed import live

    speeds = live(db, harness, now, [selected] if selected else [])
    # Legacy aliases are used only when they identify exactly this durable session.
    if (
        selected
        and db.execute(
            "SELECT COUNT(*) FROM session_alias WHERE harness=? AND alias=?",
            (harness, selected["sessionKey"]),
        ).fetchone()[0]
        != 1
    ):
        speeds = []
    if selected:
        current_speed = next((row for row in speeds if row["modelKey"] == selected["modelKey"]), None)
        if current_speed:
            selected["tokensPerSecond"] = current_speed["tokensPerSecond"]
    return {
        "start": format_instant(window.start),
        "end": format_instant(window.end),
        "offsetMinutes": offset_minutes,
        "grain": "minute",
        "windowKey": "|".join((chart_ref or "", *map(str, window.key))),
        "nextRefreshAt": format_instant(next_minute(now)),
        "trend": trend,
        "revision": db.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0],
        "outputRevision": (db.execute("SELECT value FROM revision WHERE scope='output'").fetchone() or [0])[
            0
        ],
        "sessionHitRates": sessions,
        "selectedSessionRef": ref,
        "chartSessionRef": chart_ref,
        "nextCursor": next_cursor,
        "windowTotals": _totals(window_values),
        "sessionTotals": _totals(lifetime),
        "batchTotals": _totals(None),
        "evidenceComplete": complete_evidence(db, harness, ref) if ref else False,
        "outputSpeeds": speeds,
    }
