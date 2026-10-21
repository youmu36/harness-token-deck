"""Thirty-day scoped throughput evidence; no raw session IDs or request history."""

import sqlite3
from datetime import timedelta
from decimal import Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.wallet_closing import timezone
from htd.domain.common.time import Instant, format_instant


def install(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS output_session_scope(day TEXT NOT NULL,harness TEXT NOT "
        "NULL,session_key TEXT NOT NULL,model_key TEXT NOT NULL,vendor_id TEXT NOT NULL,group_id"
        " TEXT NOT NULL,version_id TEXT NOT NULL,display_name TEXT NOT NULL,output_tokens TEXT "
        "NOT NULL,duration_ms TEXT NOT NULL,last_received_at TEXT NOT NULL,PRIMARY "
        "KEY(day,harness,session_key,model_key,vendor_id,group_id,version_id)) STRICT, WITHOUT "
        "ROWID"
    )
    db.execute("CREATE INDEX IF NOT EXISTS output_scope_session ON output_session_scope(harness,session_key)")


def record(
    db: sqlite3.Connection,
    harness: str,
    session: str,
    hashed: str,
    model: str,
    name: str,
    stamp: str,
    day: str,
    output: int,
    elapsed: int,
    received: str,
) -> None:
    row = db.execute(
        "SELECT l.price_group_id,l.price_version_id,COALESCE(w.vendor_id,'') vendor_id FROM ledger l "
        "LEFT JOIN wallet_entry w ON w.usage_harness=l.harness AND w.usage_key=l.dedupe_key "
        "AND w.kind='consumption' WHERE l.harness=? AND l.session_id=? AND l.model_key=? AND "
        "l.occurred_at=? LIMIT 1",
        (harness, session, model, stamp),
    ).fetchone()
    if row is None:
        return
    key = (
        day,
        harness,
        hashed,
        model,
        row["vendor_id"],
        row["price_group_id"] or "",
        row["price_version_id"] or "",
    )
    old = db.execute(
        "SELECT output_tokens,duration_ms FROM output_session_scope WHERE day=? AND harness=? "
        "AND session_key=? AND model_key=? AND vendor_id=? AND group_id=? AND version_id=?",
        key,
    ).fetchone()
    db.execute(
        "INSERT INTO output_session_scope VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE "
        "SET "
        "output_tokens=excluded.output_tokens,duration_ms=excluded.duration_ms,last_received_at=excluded.last_received_at,display_name=excluded.display_name",
        (
            *key,
            name,
            str(output + (int(old[0]) if old else 0)),
            str(elapsed + (int(old[1]) if old else 0)),
            received,
        ),
    )


def references(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> list[dict[str, Any]]:
    from htd.adapters.sqlite.output_speed import rate_ms

    today = at.value.astimezone(ZoneInfo(timezone(db))).date()
    clauses: list[str] = []
    args: list[str] = []
    for column, name in (
        ("o.harness", "harnesses"),
        ("o.model_key", "modelKeys"),
        ("o.vendor_id", "vendorIds"),
        ("o.group_id", "groupIds"),
        ("o.version_id", "versionIds"),
    ):
        values: list[str] = list(dict.fromkeys(query.get(name, [])))
        if values:
            clauses.append(column + " IN (" + ",".join("?" for _ in values) + ")")
            args.extend(values)
    clause = " AND " + " AND ".join(clauses) if clauses else ""
    rows = db.execute(
        "SELECT o.* FROM output_session_scope o LEFT JOIN session_hit_rate s ON "
        "s.harness=o.harness AND s.session_key=o.session_key WHERE o.day>=? AND o.day<=? AND "
        "MAX(o.last_received_at,COALESCE(s.last_received_at,o.last_received_at))<=?"
        + clause,
        (
            (today - timedelta(days=29)).isoformat(),
            today.isoformat(),
            format_instant(Instant(at.value - timedelta(minutes=10))),
            *args,
        ),
    )
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    # A session spanning multiple selected versions is one weighted session sample.
    sessions: dict[tuple[str, str, str, str], list[Any]] = {}
    for row in rows:
        if len(sessions) >= 10000:
            from htd.domain.common.errors import DomainError

            raise DomainError("ANALYSIS_LIMIT", "吞吐样本达到预算，请减少比较模型或筛选供应商")
        value = sessions.setdefault(
            (row["day"], row["harness"], row["session_key"], row["model_key"]), [row["display_name"], 0, 0]
        )
        value[1] += int(row["output_tokens"])
        value[2] += int(row["duration_ms"])
    with localcontext() as context:
        context.prec = 60
        for (day, _, _, model), (name, output, elapsed) in sessions.items():
            speed = rate_ms(output, elapsed)
            if speed is None:
                continue
            item = totals.setdefault(
                (day, model),
                {"day": day, "modelKey": model, "displayName": name, "sum": Decimal(0), "samples": 0},
            )
            item["sum"] += Decimal(speed)
            item["samples"] += 1
        result: list[dict[str, Any]] = []
        for row in totals.values():
            row["tokensPerSecond"] = str((row.pop("sum") / row["samples"]).quantize(Decimal("0.1")))
            result.append(row)
        return sorted(result, key=lambda r: (r["day"], r["modelKey"]))
