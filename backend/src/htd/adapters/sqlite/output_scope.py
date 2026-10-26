"""Sixty-two-day scoped throughput evidence; no raw session IDs or request history."""

import sqlite3
from datetime import timedelta
from decimal import Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.wallet_closing import timezone
from htd.application.ports.query_control import check_read
from htd.domain.common.time import Instant


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
    db.execute(
        "CREATE TABLE IF NOT EXISTS output_scope_published AS SELECT * FROM output_session_scope WHERE 0"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS output_scope_published_key ON output_scope_published"
        "(day,harness,session_key,model_key,vendor_id,group_id,version_id)"
    )
    if not db.execute("SELECT 1 FROM runtime_state WHERE key='output_scope_identity_v2'").fetchone():
        had_rows = db.execute("SELECT 1 FROM output_session_scope LIMIT 1").fetchone() is not None
        # Old aggregates have lost request identity. Keep ordinary published daily
        # speeds, but never present ambiguous dimensional evidence as repaired.
        db.execute("DELETE FROM output_session_scope")
        db.execute(
            "INSERT INTO runtime_state VALUES('output_scope_identity_v2',?)",
            ("partial" if had_rows else "complete",),
        )


def record(
    db: sqlite3.Connection,
    harness: str,
    hashed: str,
    model: str,
    name: str,
    day: str,
    output: int,
    elapsed: int,
    received: str,
    attribution: tuple[str, str, str] | None,
) -> None:
    if attribution is None:
        return
    key = (
        day,
        harness,
        hashed,
        model,
        *attribution,
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


def publish(db: sqlite3.Connection, day: str, harness: str, session: str, model: str) -> None:
    key = day, harness, session, model
    db.execute(
        "DELETE FROM output_scope_published WHERE day=? AND harness=? AND session_key=? AND model_key=?", key
    )
    db.execute(
        "INSERT INTO output_scope_published SELECT * FROM output_session_scope "
        "WHERE day=? AND harness=? AND session_key=? AND model_key=?",
        key,
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
        "SELECT o.* FROM output_scope_published o WHERE o.day>=? AND o.day<=?" + clause,
        (
            query.get("startDay", (today - timedelta(days=29)).isoformat()),
            query.get("endDay", today.isoformat()),
            *args,
        ),
    )
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    # A session spanning multiple selected versions is one weighted session sample.
    sessions: dict[tuple[str, str, str, str], list[Any]] = {}
    for row in rows:
        check_read()
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
