"""Bounded per-model windows; one durable daily contribution per quiet session."""

import json
import sqlite3
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.token_activity import QUIET
from htd.adapters.sqlite.wallet_closing import bounds, timezone
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.output_window import MIN_TIMING_MS, SCALE, OutputWindow


def rate(output: int, minutes: int) -> str | None:
    return rate_ms(output, minutes * 60_000)


def rate_ms(output: int, milliseconds: int) -> str | None:
    if milliseconds <= 0 or output <= 0:
        return None
    with localcontext() as context:
        context.prec = max(40, len(str(output)) + 10)
        return str((Decimal(output) * 1000 / milliseconds).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def daily_sum(db: sqlite3.Connection, key: tuple[str, str, str, str], old: sqlite3.Row | None) -> Decimal:
    """Accept authoritative daily changes made by a released binary after downgrade."""
    if old is None:
        return Decimal(0)
    accumulator = db.execute(
        "SELECT total_tps FROM output_speed_sum WHERE day=? AND timezone=? AND harness=? AND model_key=?",
        key,
    ).fetchone()
    with localcontext() as context:
        context.prec = 60
        if accumulator and old["samples"]:
            total = Decimal(accumulator[0])
            published = (total / old["samples"]).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            if published == Decimal(old["tps"]):
                return total
        # Old binaries ignore the additive sum; retain their new daily values
        # at the rounded precision they published.
        return Decimal(old["tps"]) * old["samples"]


def recent_estimate(row: sqlite3.Row) -> tuple[str | None, bool]:
    """Use the last three reconstructed positive minutes; never average empty polls."""
    timing = json.loads(row["timing_json"])
    if row["finalized"] and "rate" in timing:
        return cast(str | None, timing["rate"]), bool(timing.get("timed", int(row["decode_ms"]) > 0))
    if "!window" in timing:
        tokens, milliseconds, timed = OutputWindow.load(timing["!window"]).totals(recent=True)
        return rate_ms(tokens, milliseconds * SCALE), timed
    buckets = json.loads(row["buckets_json"])
    keys = sorted(key for key, value in buckets.items() if int(value) > 0)[-3:]
    samples: list[tuple[int, int]] = []
    all_timed = True
    for key in keys:
        count = int(buckets[key])
        known = timing.get(key)
        timed = known is not None and int(known[0]) == count and int(known[1]) > 0
        samples.append((count, int(known[1]) if timed and known is not None else 60_000))
        all_timed &= timed
    if not samples:
        return (
            rate_ms(int(row["timed_output"]), int(row["decode_ms"]))
            if int(row["decode_ms"])
            else rate(int(row["output_tokens"]), row["active_minutes"])
        ), int(row["decode_ms"]) > 0
    return rate_ms(sum(count for count, _ in samples), sum(duration for _, duration in samples)), all_timed


def reference_values(row: sqlite3.Row) -> tuple[int, int, int, int]:
    timing = json.loads(row["timing_json"])
    if "!window" in timing:
        window = OutputWindow.load(timing["!window"])
        tokens, milliseconds, _ = window.totals()
        count = (tokens + SCALE // 2) // SCALE
        return count, len(window.minutes), count, milliseconds
    return int(row["output_tokens"]), row["active_minutes"], int(row["timed_output"]), int(row["decode_ms"])


def add_reference(db: sqlite3.Connection, row: sqlite3.Row) -> None:
    if row["active_minutes"] and row["last_output_at"]:
        output, minutes, timed, duration = (
            reference_values(row)
            if "timing_json" in row.keys()  # noqa: SIM118 -- sqlite3.Row membership tests values.
            else (
                int(row["output_tokens"]),
                row["active_minutes"],
                int(row["timed_output"]),
                int(row["decode_ms"]),
            )
        )
        zone = timezone(db)
        day = parse_instant(row["last_output_at"]).value.astimezone(ZoneInfo(zone)).date().isoformat()
        key = day, zone, row["harness"], row["model_key"]
        old = db.execute(
            "SELECT * FROM output_reference WHERE day=? AND timezone=? AND harness=? AND model_key=?", key
        ).fetchone()
        db.execute(
            "INSERT INTO output_reference VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
            "display_name=excluded.display_name,output_tokens=excluded.output_tokens,"
            "active_minutes=excluded.active_minutes,samples=excluded.samples,"
            "last_output_at=excluded.last_output_at,"
            "timed_output=excluded.timed_output,decode_ms=excluded.decode_ms",
            (
                *key,
                row["display_name"],
                str(output + (int(old["output_tokens"]) if old else 0)),
                minutes + (old["active_minutes"] if old else 0),
                1 + (old["samples"] if old else 0),
                max(row["last_output_at"], old["last_output_at"] if old else row["last_output_at"]),
                str(timed + (int(old["timed_output"]) if old else 0)),
                str(duration + (int(old["decode_ms"]) if old else 0)),
            ),
        )


def finalize(db: sqlite3.Connection, row: sqlite3.Row) -> None:
    if row["finalized"]:
        return
    add_reference(db, row)
    speed, timed = recent_estimate(row)
    db.execute(
        "UPDATE output_live SET finalized=1,buckets_json='{}',timing_json=? WHERE harness=? AND model_key=?",
        (json.dumps({"rate": speed, "timed": timed}), row["harness"], row["model_key"]),
    )


def begin_run(db: sqlite3.Connection) -> None:
    for row in db.execute("SELECT * FROM output_live WHERE finalized=0").fetchall():
        activity = db.execute(
            "SELECT last_received_at FROM token_activity WHERE harness=? AND model_key=?",
            (row["harness"], row["model_key"]),
        ).fetchone()
        speed, timed = recent_estimate(row)
        output, minutes, timed_output, duration = reference_values(row)
        db.execute(
            "INSERT INTO output_pending VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                uuid4().hex,
                row["harness"],
                row["model_key"],
                row["display_name"],
                max(row["last_received_at"], activity[0] if activity else row["last_received_at"]),
                row["last_output_at"],
                str(output),
                minutes,
                str(timed_output),
                str(duration),
            ),
        )
        db.execute(
            "UPDATE output_live SET finalized=1,buckets_json='{}',timing_json=? "
            "WHERE harness=? AND model_key=?",
            (json.dumps({"rate": speed, "timed": timed}), row["harness"], row["model_key"]),
        )


def close_quiet(db: sqlite3.Connection, at: Instant) -> bool:
    from htd.adapters.sqlite.session_speed import close_quiet as close_sessions

    cutoff = format_instant(Instant(at.value - QUIET))
    today = at.value.astimezone(ZoneInfo(timezone(db))).date().isoformat()
    midnight = format_instant(bounds(today, timezone(db))[0])
    changed = False
    for row in db.execute(
        "SELECT * FROM output_pending WHERE last_received_at<=? LIMIT 200", (cutoff,)
    ).fetchall():
        add_reference(db, row)
        db.execute("DELETE FROM output_pending WHERE id=?", (row["id"],))
        changed = True
    for row in db.execute(
        "SELECT o.* FROM output_live o LEFT JOIN token_activity t USING(harness,model_key) "
        "WHERE o.finalized=0 AND (MAX(o.last_received_at,COALESCE(t.last_received_at,o.last_received_at))<=? "
        "OR o.last_output_at<?)",
        (cutoff, midnight),
    ).fetchall():
        finalize(db, row)
        changed = True
    compacted = compact_days(db, at)
    return close_sessions(db, at) or compacted or changed


def compact_days(db: sqlite3.Connection, at: Instant) -> bool:
    """Bounded daily folding: keep one rounded speed, discard numeric raw evidence."""
    zone = timezone(db)
    today = at.value.astimezone(ZoneInfo(zone)).date().isoformat()
    rows = db.execute("SELECT * FROM output_reference WHERE day<? LIMIT 200", (today,)).fetchall()
    for row in rows:
        speed = (
            rate_ms(int(row["timed_output"]), int(row["decode_ms"]))
            if int(row["decode_ms"])
            else rate(int(row["output_tokens"]), row["active_minutes"])
        )
        key = row["day"], row["timezone"], row["harness"], row["model_key"]
        if speed is not None:
            old = db.execute(
                "SELECT tps,samples FROM output_daily_speed WHERE day=? AND timezone=? "
                "AND harness=? AND model_key=?",
                key,
            ).fetchone()
            samples = row["samples"] + (old["samples"] if old else 0)
            total_tps = Decimal(speed) * row["samples"] + daily_sum(db, key, old)
            value = total_tps / samples
            db.execute(
                "INSERT INTO output_speed_sum VALUES(?,?,?,?,?) "
                "ON CONFLICT DO UPDATE SET total_tps=excluded.total_tps",
                (*key, str(total_tps)),
            )
            rounded = str(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
            db.execute(
                "INSERT INTO output_daily_speed VALUES(?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "tps=excluded.tps,samples=excluded.samples,display_name=excluded.display_name",
                (*key, row["display_name"], rounded, samples),
            )
            rank = db.execute(
                "SELECT * FROM output_rank_speed WHERE harness=? AND model_key=?", key[2:]
            ).fetchone()
            total = (
                Decimal(rank["total_tps"] if rank else "0")
                + Decimal(rounded)
                - Decimal(old["tps"] if old else "0")
            )
            db.execute(
                "INSERT INTO output_rank_speed VALUES(?,?,?,?) ON CONFLICT DO UPDATE SET "
                "total_tps=excluded.total_tps,days=excluded.days",
                (*key[2:], str(total), (rank["days"] if rank else 0) + int(old is None)),
            )
        db.execute(
            "DELETE FROM output_reference WHERE day=? AND timezone=? AND harness=? AND model_key=?", key
        )
    # Frozen windows are display aggregates only. No stale minute/timing arrays.
    db.execute(
        "DELETE FROM output_live WHERE finalized=1 AND last_output_at<?",
        (format_instant(bounds(today, zone)[0]),),
    )
    return bool(rows)


def ranking(db: sqlite3.Connection, harness: str, models: tuple[str, ...]) -> dict[str, str]:
    if not models:
        return {}
    clause = "model_key IN (" + ",".join("?" for _ in models) + ")"
    args: tuple[str, ...] = models
    if harness != "all":
        clause += " AND harness=?"
        args += (harness,)
    grouped: dict[str, tuple[Decimal, int]] = {}
    for row in db.execute("SELECT * FROM output_rank_speed WHERE " + clause, args):
        total, days = grouped.get(row["model_key"], (Decimal(0), 0))
        grouped[row["model_key"]] = total + Decimal(row["total_tps"]), days + row["days"]
    return {
        key: str((total / days).quantize(Decimal("0.1"))) for key, (total, days) in grouped.items() if days
    }


def record(
    db: sqlite3.Connection,
    *,
    harness: str,
    model_key: str,
    display_name: str,
    output: int | None,
    occurred_at: str,
    at: Instant,
    samples: list[tuple[str, int | None, int | None]] | None = None,
    generation_milliseconds: int | None = None,
    session_ids: list[str | None] | None = None,
) -> None:
    if session_ids and model_key:
        from htd.adapters.sqlite.session_speed import record as record_sessions

        records = samples if samples is not None else [(occurred_at, output, generation_milliseconds)]
        record_sessions(db, harness, model_key, display_name, records, session_ids, at)
        # Rows with no session evidence retain the released fallback. Never
        # count the same known-session contribution in both reference paths.
        unknown = [sample for sample, session in zip(records, session_ids, strict=True) if not session]
        if not unknown:
            return
        samples = unknown
        session_ids = [None for _ in unknown]
    zone = ZoneInfo(timezone(db))
    grouped: dict[str, list[tuple[tuple[str, int | None, int | None], str | None]]] = {}
    for index, sample in enumerate(
        samples if samples is not None else [(occurred_at, output, generation_milliseconds)]
    ):
        day = parse_instant(sample[0]).value.astimezone(zone).date().isoformat()
        grouped.setdefault(day, []).append((sample, session_ids[index] if session_ids else None))
    for _, records in sorted(grouped.items()):
        _record_window(
            db,
            harness=harness,
            model_key=model_key,
            display_name=display_name,
            output=output,
            occurred_at=records[-1][0][0],
            at=at,
            samples=[sample for sample, _ in records],
            session_ids=[session for _, session in records],
        )


def _record_window(
    db: sqlite3.Connection,
    *,
    harness: str,
    model_key: str,
    display_name: str,
    output: int | None,
    occurred_at: str,
    at: Instant,
    samples: list[tuple[str, int | None, int | None]],
    session_ids: list[str | None],
) -> None:
    if not model_key:
        return
    stamp = format_instant(at)
    prior = db.execute(
        "SELECT * FROM output_live WHERE harness=? AND model_key=?", (harness, model_key)
    ).fetchone()
    fresh = (
        prior is None
        or prior["finalized"]
        or (
            prior["last_output_at"] is not None
            and parse_instant(prior["last_output_at"]).value.astimezone(ZoneInfo(timezone(db))).date()
            != parse_instant(occurred_at).value.astimezone(ZoneInfo(timezone(db))).date()
        )
        or (at.value - parse_instant(prior["last_received_at"]).value >= QUIET)
    )
    if prior is not None and fresh:
        finalize(db, prior)
    buckets: dict[str, str] = {}
    timings: dict[str, list[str]] = {}
    last: str | None = None
    if not fresh and prior is not None:
        buckets = json.loads(prior["buckets_json"])
        last = prior["last_output_at"]
        timings = json.loads(prior["timing_json"])
    window = OutputWindow.load(timings.pop("!window", None))
    if not window.minutes:
        # Released rows have only completion-minute aggregates. Keep their known
        # numbers until they roll out; missing per-session history is not guessed.
        for bucket, value in buckets.items():
            count = int(value)
            old_timing = timings.get(bucket)
            valid = old_timing is not None and int(old_timing[0]) == count
            milliseconds = int(old_timing[1]) if valid and old_timing is not None else 60_000
            window.minutes[bucket] = [count * SCALE, milliseconds, milliseconds if valid else 0]
        window.latest = last
    for (sample_at, count, duration), session in sorted(
        zip(samples, session_ids, strict=True), key=lambda pair: pair[0][0]
    ):
        if duration is not None and duration < MIN_TIMING_MS:
            window.add(sample_at, None, None, session, stamp)
            continue
        window.add(sample_at, count, duration, session, stamp)
        if count is None or count <= 0:
            continue
        last = max(last or sample_at, sample_at)
        bucket = sample_at[:16] + ":00.000Z"
        buckets[bucket] = str(int(buckets.get(bucket, "0")) + count)
        if duration is not None and duration > 0:
            previous = timings.get(bucket, ["0", "0"])
            timings[bucket] = [str(int(previous[0]) + count), str(int(previous[1]) + duration)]
    if last is not None:
        # At most 60 positive minute buckets, ending at the latest output minute.
        edge = format_instant(Instant(parse_instant(last[:16] + ":00.000Z").value - timedelta(minutes=59)))
        buckets = {key: value for key, value in buckets.items() if key >= edge}
        timings = {key: value for key, value in timings.items() if key >= edge}
    if not buckets:
        return
    db.execute(
        "INSERT INTO output_live VALUES(?,?,?,?,?,?,?,?,0,?,?,?) "
        "ON CONFLICT(harness,model_key) DO UPDATE SET "
        "display_name=excluded.display_name,last_received_at=excluded.last_received_at,"
        "last_output_at=excluded.last_output_at,buckets_json=excluded.buckets_json,"
        "output_tokens=excluded.output_tokens,active_minutes=excluded.active_minutes,finalized=0,"
        "timing_json=excluded.timing_json,timed_output=excluded.timed_output,decode_ms=excluded.decode_ms",
        (
            harness,
            model_key,
            display_name,
            stamp,
            last,
            json.dumps(buckets, separators=(",", ":")),
            str(sum(map(int, buckets.values()))),
            len(buckets),
            json.dumps({**timings, "!window": window.dump()}, separators=(",", ":")),
            str(sum(int(value[0]) for value in timings.values())),
            str(sum(int(value[1]) for value in timings.values())),
        ),
    )


def live(
    db: sqlite3.Connection,
    harness: str,
    at: Instant,
    sessions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    from htd.adapters.sqlite.session_speed import live as session_live

    speeds = session_live(db, harness, at, sessions)
    if speeds:
        return speeds
    return [
        {
            "modelKey": row["model_key"],
            "displayName": row["display_name"],
            "tokensPerSecond": speed,
            "basis": ("request_estimate" if harness == "codex" else "generation")
            if timed
            else "minute_estimate",
            "outputTokens": row["output_tokens"],
            "activeMinutes": row["active_minutes"],
            "lastOutputAt": row["last_output_at"],
            "frozen": bool(row["finalized"]) or at.value - parse_instant(row["activity_at"]).value >= QUIET,
        }
        for row in db.execute(
            "SELECT o.*,MAX(o.last_received_at,COALESCE(t.last_received_at,o.last_received_at)) activity_at "
            "FROM output_live o LEFT JOIN token_activity t USING(harness,model_key) WHERE harness=? "
            "ORDER BY o.last_received_at DESC,model_key LIMIT 200",
            (harness,),
        )
        if row["active_minutes"] > 0
        for speed, timed in [recent_estimate(row)]
    ]


def references(
    db: sqlite3.Connection,
    harness: str,
    start: str,
    end: str,
    model_key: str | None,
    model_keys: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    clause, args = ("", ()) if harness == "all" else (" AND harness=?", (harness,))
    if model_key is not None:
        clause += " AND model_key=?"
        args += (model_key,)
    elif model_keys:
        clause += " AND model_key IN (" + ",".join("?" for _ in model_keys) + ")"
        args += model_keys
    else:
        return []
    # Daily authority remains independent of token-detail retention. Page/chart limits
    # are applied by the caller; exact integers never go through SQLite REAL/SUM.
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    # A daily line has at most 120 days per page of models; longer calendar usage
    # queries retain their normal month/year resolution without expanding this series.
    end_day = parse_instant(end).value.astimezone(ZoneInfo(timezone(db)))
    start_day = parse_instant(start).value.astimezone(ZoneInfo(timezone(db))).date().isoformat()
    first_day = max(start_day, (end_day - timedelta(days=29)).date().isoformat())
    for row in db.execute(
        "SELECT * FROM output_reference WHERE day>=? AND day<=?" + clause + " ORDER BY day,model_key",
        (first_day, end_day.date().isoformat(), *args),
    ):
        key = row["day"], row["model_key"]
        group = groups.setdefault(
            key,
            {
                "day": row["day"],
                "modelKey": row["model_key"],
                "displayName": row["display_name"],
                "output": 0,
                "minutes": 0,
                "samples": 0,
                "timed_output": 0,
                "decode_ms": 0,
            },
        )
        group["output"] += int(row["output_tokens"])
        group["minutes"] += row["active_minutes"]
        group["samples"] += row["samples"]
        group["timed_output"] += int(row["timed_output"])
        group["decode_ms"] += int(row["decode_ms"])
    result: list[dict[str, Any]] = [
        {
            "day": item["day"],
            "modelKey": item["modelKey"],
            "displayName": item["displayName"],
            "tokensPerSecond": rate_ms(item["timed_output"], item["decode_ms"])
            if item["decode_ms"]
            else rate(item["output"], item["minutes"]),
            "samples": item["samples"],
        }
        for item in groups.values()
    ]
    for row in db.execute(
        "SELECT * FROM output_daily_speed WHERE day>=? AND day<=?" + clause + " ORDER BY day,model_key",
        (first_day, end_day.date().isoformat(), *args),
    ):
        result.append(
            {
                "day": row["day"],
                "modelKey": row["model_key"],
                "displayName": row["display_name"],
                "tokensPerSecond": row["tps"],
                "samples": row["samples"],
            }
        )
    # Different harness/timezone contributions share one model/day on the chart.
    combined: dict[tuple[str, str], dict[str, Any]] = {}
    for item in result:
        key = item["day"], item["modelKey"]
        group = combined.setdefault(key, {**item, "sum": Decimal(0), "samples": 0})
        if item["tokensPerSecond"] is not None:
            group["sum"] += Decimal(item["tokensPerSecond"]) * item["samples"]
            group["samples"] += item["samples"]
    return [
        {
            "day": item["day"],
            "modelKey": item["modelKey"],
            "displayName": item["displayName"],
            "tokensPerSecond": str((item["sum"] / item["samples"]).quantize(Decimal("0.1")))
            if item["samples"]
            else None,
            "samples": item["samples"],
        }
        for _, item in sorted(combined.items())
    ]
