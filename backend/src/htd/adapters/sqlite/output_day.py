"""Withdraw a published numeric sample before repairing or moving its business day."""

import sqlite3
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any


def withdraw(db: sqlite3.Connection, row: Any) -> None:
    from htd.adapters.sqlite.output_speed import daily_sum

    if row["published_tps"] is None:
        return
    key = row["day"], row["timezone"], row["harness"], row["model_key"]
    old = db.execute(
        "SELECT * FROM output_daily_speed WHERE day=? AND timezone=? AND harness=? AND model_key=?", key
    ).fetchone()
    if old is None:
        return
    with localcontext() as context:
        context.prec = 60
        total = daily_sum(db, key, old) - Decimal(row["published_tps"])
        samples = old["samples"] - 1
        daily = (total / samples).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) if samples else Decimal(0)
        if samples:
            db.execute(
                "UPDATE output_daily_speed SET tps=?,samples=? "
                "WHERE day=? AND timezone=? AND harness=? AND model_key=?",
                (str(daily), samples, *key),
            )
            db.execute(
                "UPDATE output_speed_sum SET total_tps=? "
                "WHERE day=? AND timezone=? AND harness=? AND model_key=?",
                (str(total), *key),
            )
        else:
            db.execute(
                "DELETE FROM output_daily_speed WHERE day=? AND timezone=? AND harness=? AND model_key=?", key
            )
            db.execute(
                "DELETE FROM output_speed_sum WHERE day=? AND timezone=? AND harness=? AND model_key=?", key
            )
        rank = db.execute(
            "SELECT * FROM output_rank_speed WHERE harness=? AND model_key=?", key[2:]
        ).fetchone()
        if rank:
            days = rank["days"] - int(not samples)
            if days:
                db.execute(
                    "UPDATE output_rank_speed SET total_tps=?,days=? WHERE harness=? AND model_key=?",
                    (str(Decimal(rank["total_tps"]) + daily - Decimal(old["tps"])), days, *key[2:]),
                )
            else:
                db.execute("DELETE FROM output_rank_speed WHERE harness=? AND model_key=?", key[2:])


def rebase(db: sqlite3.Connection, harness: str, session: str, day: str) -> None:
    rows = db.execute(
        "SELECT * FROM output_session_day WHERE harness=? AND session_key=? AND day<>?",
        (harness, session, day),
    ).fetchall()
    for row in rows:
        withdraw(db, row)
        key = day, row["timezone"], harness, row["model_key"], session
        prior = db.execute(
            "SELECT * FROM output_session_day "
            "WHERE day=? AND timezone=? AND harness=? AND model_key=? AND session_key=?",
            key,
        ).fetchone()
        db.execute(
            "INSERT INTO output_session_day VALUES(?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT DO UPDATE SET "
            "output_tokens=excluded.output_tokens,duration_ms=excluded.duration_ms,"
            "last_received_at=excluded.last_received_at,pending=1",
            (
                *key,
                row["display_name"],
                str(int(row["output_tokens"]) + (int(prior["output_tokens"]) if prior else 0)),
                str(int(row["duration_ms"]) + (int(prior["duration_ms"]) if prior else 0)),
                max(row["last_received_at"], prior["last_received_at"] if prior else ""),
                prior["published_tps"] if prior else None,
            ),
        )
        db.execute(
            "DELETE FROM output_session_day "
            "WHERE day=? AND timezone=? AND harness=? AND model_key=? AND session_key=?",
            (row["day"], *key[1:]),
        )
    if not rows:
        return
    for row in db.execute(
        "SELECT * FROM output_session_scope WHERE harness=? AND session_key=? AND day<>?",
        (harness, session, day),
    ).fetchall():
        key = day, harness, session, row["model_key"], row["vendor_id"], row["group_id"], row["version_id"]
        where = (
            "day=? AND harness=? AND session_key=? AND model_key=? "
            "AND vendor_id=? AND group_id=? AND version_id=?"
        )
        prior = db.execute("SELECT * FROM output_session_scope WHERE " + where, key).fetchone()
        db.execute(
            "INSERT INTO output_session_scope VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
            "output_tokens=excluded.output_tokens,duration_ms=excluded.duration_ms,"
            "last_received_at=excluded.last_received_at",
            (
                *key,
                row["display_name"],
                str(int(row["output_tokens"]) + (int(prior["output_tokens"]) if prior else 0)),
                str(int(row["duration_ms"]) + (int(prior["duration_ms"]) if prior else 0)),
                max(row["last_received_at"], prior["last_received_at"] if prior else ""),
            ),
        )
        db.execute("DELETE FROM output_session_scope WHERE " + where, (row["day"], *key[1:]))
