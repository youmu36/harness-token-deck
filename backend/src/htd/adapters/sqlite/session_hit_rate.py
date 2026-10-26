"""Compatibility session totals; durable sessions never reset on quiet or another session."""

import sqlite3
from decimal import Decimal
from typing import Any, cast

from htd.adapters.sqlite.session_state import frozen, session_key
from htd.domain.common.time import Instant, format_instant
from htd.domain.usage.model import Harness, UsageEvent
from htd.domain.usage.model_key import model_key


def record(db: sqlite3.Connection, event: UsageEvent, at: Instant) -> None:
    if event.session_id is None:
        return
    harness = event.harness.value
    key = session_key(event.session_id)
    stamp = format_instant(at)
    previous = cast(
        sqlite3.Row | None,
        db.execute(
            "SELECT * FROM session_hit_rate WHERE harness=? AND session_key=?", (harness, key)
        ).fetchone(),
    )
    if previous is None:
        started_at = event.occurred_at
        input_tokens = cache_tokens = missing_input = missing_cache = 0
    else:
        started_at = min(previous["started_at"], event.occurred_at)
        input_tokens = int(previous["input_tokens"])
        cache_tokens = int(previous["cache_tokens"])
        missing_input = previous["missing_input"]
        missing_cache = previous["missing_cache"]

    tokens = event.tokens
    if tokens.input is None:
        missing_input += 1
    if tokens.cache_read is None:
        missing_cache += 1
    elif tokens.input is not None and not (
        event.harness == Harness.ZCODE
        and event.model_id is not None
        and "grok" in model_key(event.model_id)
        and tokens.cache_read == 0
    ):
        input_tokens += tokens.input
        cache_tokens += tokens.cache_read

    db.execute(
        "INSERT INTO session_hit_rate VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(harness,session_key) DO UPDATE SET display_name=excluded.display_name,"
        "started_at=excluded.started_at,"
        "last_received_at=excluded.last_received_at,input_tokens=excluded.input_tokens,"
        "cache_tokens=excluded.cache_tokens,missing_input=excluded.missing_input,"
        "missing_cache=excluded.missing_cache",
        (
            harness,
            key,
            event.model_id or "模型未知",
            started_at,
            stamp,
            str(input_tokens),
            str(cache_tokens),
            missing_input,
            missing_cache,
        ),
    )


def read(db: sqlite3.Connection, harness: str, now: Instant) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in db.execute(
        "SELECT * FROM session_hit_rate WHERE harness=? ORDER BY started_at,session_key LIMIT 100",
        (harness,),
    ):
        denominator = int(row["input_tokens"])
        numerator = int(row["cache_tokens"])
        values.append(
            {
                "sessionKey": row["session_key"],
                "displayName": row["display_name"],
                "startedAt": row["started_at"],
                "lastActivityAt": row["last_received_at"],
                "cacheHitRate": float(Decimal(numerator) / Decimal(denominator)) if denominator else None,
                "cacheCoverageIncomplete": bool(row["missing_input"] or row["missing_cache"]),
                "frozen": frozen(row["last_received_at"], now),
            }
        )
    return values
