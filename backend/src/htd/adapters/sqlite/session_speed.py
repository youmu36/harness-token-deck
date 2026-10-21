"""Session/model speed projections, updated only by the existing ingest/quiet jobs."""

import json
import sqlite3
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.session_state import frozen, session_key
from htd.adapters.sqlite.token_activity import QUIET
from htd.adapters.sqlite.wallet_closing import timezone
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.output_window import SCALE, OutputWindow


def install(db: sqlite3.Connection) -> None:
    for sql in (
        "CREATE TABLE IF NOT EXISTS output_session("
        "harness TEXT NOT NULL,session_key TEXT NOT NULL,model_key TEXT NOT NULL,display_name TEXT NOT NULL,"
        "started_at TEXT NOT NULL,last_received_at TEXT NOT NULL,last_output_at TEXT,"
        "output_tokens TEXT NOT NULL,duration_ms TEXT NOT NULL,window_json TEXT NOT NULL,"
        "PRIMARY KEY(harness,session_key,model_key)) STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS output_session_activity ON output_session(harness,last_received_at DESC)",
        "CREATE TABLE IF NOT EXISTS output_session_day("
        "day TEXT NOT NULL,timezone TEXT NOT NULL,harness TEXT NOT NULL,model_key TEXT NOT NULL,"
        "session_key TEXT NOT NULL,display_name TEXT NOT NULL,output_tokens TEXT NOT NULL,"
        "duration_ms TEXT NOT NULL,last_received_at TEXT NOT NULL,"
        "published_tps TEXT,pending INTEGER NOT NULL,"
        "PRIMARY KEY(day,timezone,harness,model_key,session_key)) STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS output_session_day_pending "
        "ON output_session_day(pending,last_received_at,day)",
        "CREATE INDEX IF NOT EXISTS output_session_day_session ON output_session_day(harness,session_key)",
        "CREATE TABLE IF NOT EXISTS output_speed_sum("
        "day TEXT NOT NULL,timezone TEXT NOT NULL,harness TEXT NOT NULL,model_key TEXT NOT NULL,"
        "total_tps TEXT NOT NULL,PRIMARY KEY(day,timezone,harness,model_key)) STRICT, WITHOUT ROWID",
    ):
        db.execute(sql)


def record(
    db: sqlite3.Connection,
    harness: str,
    model: str,
    name: str,
    samples: list[tuple[str, int | None, int | None]],
    sessions: list[str | None],
    at: Instant,
) -> None:
    zone = timezone(db)
    received = format_instant(at)
    grouped: dict[str, list[tuple[str, int | None, int | None]]] = {}
    for sample, session in zip(samples, sessions, strict=True):
        if session:
            key = session_key(session)
            grouped.setdefault(key, []).append(sample)
    for key, values in grouped.items():
        identity = harness, key, model
        old = db.execute(
            "SELECT * FROM output_session WHERE harness=? AND session_key=? AND model_key=?", identity
        ).fetchone()
        window = OutputWindow.load(json.loads(old["window_json"])) if old else OutputWindow()
        if old and at.value - parse_instant(old["last_received_at"]).value >= QUIET:
            window.cursors.clear()
        output = int(old["output_tokens"]) if old else 0
        duration = int(old["duration_ms"]) if old else 0
        last = old["last_output_at"] if old else None
        first = db.execute(
            "SELECT MIN(started_at) FROM output_session WHERE harness=? AND session_key=?", (harness, key)
        ).fetchone()[0]
        start = min([sample[0] for sample in values] + ([first] if first else []))
        session_day = parse_instant(start).value.astimezone(ZoneInfo(zone)).date().isoformat()
        from htd.adapters.sqlite.output_day import rebase

        rebase(db, harness, key, session_day)
        db.execute(
            "UPDATE output_session SET started_at=MIN(started_at,?) WHERE harness=? AND session_key=?",
            (start, harness, key),
        )
        days: dict[str, list[int]] = {}
        for stamp, count, elapsed in sorted(values, key=lambda sample: sample[0]):
            effective = window.add(stamp, count, elapsed, key, received)
            if effective is None:
                continue
            assert count is not None
            output += count
            duration += effective
            last = max(last or stamp, stamp)
            day = session_day
            totals = days.setdefault(day, [0, 0])
            totals[0] += count
            totals[1] += effective
            from htd.adapters.sqlite.output_scope import record as record_scope

            session = next(value for value in sessions if value and session_key(value) == key)
            record_scope(db, harness, session, key, model, name, stamp, day, count, effective, received)
        db.execute(
            "INSERT INTO output_session VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
            "display_name=excluded.display_name,started_at=excluded.started_at,"
            "last_received_at=excluded.last_received_at,last_output_at=excluded.last_output_at,"
            "output_tokens=excluded.output_tokens,duration_ms=excluded.duration_ms,window_json=excluded.window_json",
            (*identity, name, start, received, last, str(output), str(duration), json.dumps(window.dump())),
        )
        for day, (count, elapsed) in days.items():
            day_key = day, zone, harness, model, key
            prior = db.execute(
                "SELECT output_tokens,duration_ms FROM output_session_day "
                "WHERE day=? AND timezone=? AND harness=? AND model_key=? AND session_key=?",
                day_key,
            ).fetchone()
            db.execute(
                "INSERT INTO output_session_day VALUES(?,?,?,?,?,?,?,?,?,NULL,1) ON CONFLICT DO UPDATE SET "
                "display_name=excluded.display_name,output_tokens=excluded.output_tokens,"
                "duration_ms=excluded.duration_ms,last_received_at=excluded.last_received_at,pending=1",
                (
                    *day_key,
                    name,
                    str(count + (int(prior[0]) if prior else 0)),
                    str(elapsed + (int(prior[1]) if prior else 0)),
                    received,
                ),
            )


def close_quiet(db: sqlite3.Connection, at: Instant) -> bool:
    from htd.adapters.sqlite.output_speed import daily_sum, rate_ms

    cutoff = format_instant(Instant(at.value - QUIET))
    today = at.value.astimezone(ZoneInfo(timezone(db))).date().isoformat()
    rows = db.execute(
        "SELECT d.* FROM output_session_day d LEFT JOIN session_hit_rate s "
        "ON s.harness=d.harness AND s.session_key=d.session_key WHERE d.pending=1 AND "
        "(MAX(d.last_received_at,COALESCE(s.last_received_at,d.last_received_at))<=? OR d.day<?) LIMIT 200",
        (cutoff, today),
    ).fetchall()
    for row in rows:
        speed = rate_ms(int(row["output_tokens"]), int(row["duration_ms"]))
        assert speed is not None
        key = row["day"], row["timezone"], row["harness"], row["model_key"]
        old = db.execute(
            "SELECT * FROM output_daily_speed WHERE day=? AND timezone=? AND harness=? AND model_key=?", key
        ).fetchone()
        with localcontext() as context:
            context.prec = 60
            total = daily_sum(db, key, old)
            total += Decimal(speed) - Decimal(row["published_tps"] or "0")
            samples = (old["samples"] if old else 0) + int(row["published_tps"] is None)
            daily = str((total / samples).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
            db.execute(
                "INSERT INTO output_speed_sum VALUES(?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "total_tps=excluded.total_tps",
                (*key, str(total)),
            )
            db.execute(
                "INSERT INTO output_daily_speed VALUES(?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "display_name=excluded.display_name,tps=excluded.tps,samples=excluded.samples",
                (*key, row["display_name"], daily, samples),
            )
            rank = db.execute(
                "SELECT * FROM output_rank_speed WHERE harness=? AND model_key=?", key[2:]
            ).fetchone()
            rank_total = (
                Decimal(rank["total_tps"] if rank else "0")
                + Decimal(daily)
                - Decimal(old["tps"] if old else "0")
            )
            db.execute(
                "INSERT INTO output_rank_speed VALUES(?,?,?,?) ON CONFLICT DO UPDATE SET "
                "total_tps=excluded.total_tps,days=excluded.days",
                (*key[2:], str(rank_total), (rank["days"] if rank else 0) + int(old is None)),
            )
        db.execute(
            "UPDATE output_session_day SET published_tps=?,pending=0 WHERE "
            "day=? AND timezone=? AND harness=? AND model_key=? AND session_key=?",
            (speed, *key, row["session_key"]),
        )
    return bool(rows)


def live(
    db: sqlite3.Connection,
    harness: str,
    at: Instant,
    sessions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    from htd.adapters.sqlite.output_speed import rate_ms

    current = {value["sessionKey"]: value for value in sessions} if sessions is not None else None
    if current is not None and not current:
        return []
    clause = " AND session_key IN (" + ",".join("?" for _ in current) + ")" if current else ""
    rows = db.execute(
        "SELECT * FROM output_session WHERE harness=?" + clause + " ORDER BY last_received_at DESC LIMIT 100",
        (harness, *(current or {})),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda row: (row["started_at"], row["session_key"], row["model_key"])):
        window = OutputWindow.load(json.loads(row["window_json"]))
        tokens, milliseconds, timed = window.totals(recent=True)
        activity = current.get(row["session_key"]) if current else None
        result.append(
            {
                "sessionKey": row["session_key"],
                "startedAt": activity["startedAt"] if activity else row["started_at"],
                "modelKey": row["model_key"],
                "displayName": row["display_name"],
                "tokensPerSecond": rate_ms(tokens, milliseconds * SCALE),
                "sessionAverageTps": rate_ms(int(row["output_tokens"]), int(row["duration_ms"])),
                "outputTokens": row["output_tokens"],
                "activeMinutes": len(window.minutes),
                "lastOutputAt": row["last_output_at"],
                "frozen": activity["frozen"] if activity else frozen(row["last_received_at"], at),
                "basis": ("request_estimate" if harness == "codex" else "generation")
                if timed
                else "minute_estimate",
            }
        )
    return result
