"""Current and recently frozen per-session cache ratios, retained only until the next session."""

import hashlib
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.usage.model import Harness, UsageEvent
from htu.domain.usage.model_key import model_key

QUIET = timedelta(minutes=10)


def record(db: sqlite3.Connection, event: UsageEvent, at: Instant) -> None:
    if event.session_id is None:
        return
    harness = event.harness.value
    key = hashlib.sha256(event.session_id.encode("utf-8")).hexdigest()[:16]
    stamp = format_instant(at)
    cutoff = format_instant(Instant(at.value - QUIET))
    previous = cast(
        sqlite3.Row | None,
        db.execute(
            "SELECT * FROM session_hit_rate WHERE harness=? AND session_key=?", (harness, key)
        ).fetchone(),
    )
    if previous is None or previous["last_received_at"] <= cutoff:
        db.execute("DELETE FROM session_hit_rate WHERE harness=? AND last_received_at<=?", (harness, cutoff))
        started_at = stamp
        input_tokens = cache_tokens = missing_input = missing_cache = 0
    else:
        started_at = previous["started_at"]
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
    if previous is None:
        db.execute(
            "DELETE FROM session_hit_rate WHERE harness=? AND session_key NOT IN "
            "(SELECT session_key FROM session_hit_rate WHERE harness=? "
            "ORDER BY last_received_at DESC LIMIT 100)",
            (harness, harness),
        )


def read(db: sqlite3.Connection, harness: str, now: Instant) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in db.execute(
        "SELECT * FROM session_hit_rate WHERE harness=? ORDER BY last_received_at DESC,session_key LIMIT 100",
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
                "frozen": now.value - parse_instant(row["last_received_at"]).value >= QUIET,
            }
        )
    return values
