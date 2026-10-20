"""Copy the released wallet once; each new book then follows its own deltas."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, format_instant


def seed(db: sqlite3.Connection, vendor: str) -> dict[str, Any] | None:
    row = db.execute("SELECT payload FROM wallet_inheritance WHERE vendor_id=?", (vendor,)).fetchone()
    return json.loads(row[0]) if row else None


def inherit(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM meta WHERE key='wallet_inheritance_042'").fetchone():
        return
    from htu.adapters.sqlite.account_book import AccountBook
    from htu.adapters.sqlite.local_wallet import remote_summary, summary
    from htu.adapters.sqlite.wallet_closing import bounds, timezone

    book = AccountBook(db)
    for row in db.execute("SELECT id FROM vendor").fetchall():
        vendor = row[0]
        legacy = book.summary(vendor)
        metadata = db.execute(
            "SELECT past_top_up,past_consumption FROM vendor_accounting WHERE vendor_id=?", (vendor,)
        ).fetchone()
        target = {key: legacy[key] for key in ("R", "C", "A", "B")}
        target.update(
            pastTopUpPico=metadata[0] if metadata else "0",
            pastConsumptionPico=metadata[1] if metadata else "0",
        )
        remote = remote_summary(db, vendor)
        payload: dict[str, Any] = {
            "target": target,
            "local": summary(db, vendor),
            "remote": remote or dict.fromkeys("RCAB", "0"),
            "remote_initialized": remote is not None,
            "wallet_seq": db.execute("SELECT value FROM wallet_sequence WHERE id=1").fetchone()[0],
            "local_seq": db.execute("SELECT COALESCE(MAX(id),0) FROM local_wallet_posting").fetchone()[0],
        }
        db.execute(
            "INSERT INTO wallet_inherited_daily SELECT vendor_id,day,timezone,current "
            "FROM wallet_daily_close WHERE vendor_id=?",
            (vendor,),
        )
        zone = timezone(db)
        today = datetime.now(UTC).astimezone(ZoneInfo(zone)).date().isoformat()
        pending_days = {
            r[0]
            for r in db.execute(
                "SELECT day FROM wallet_daily_dirty WHERE vendor_id=? AND timezone=?", (vendor, zone)
            )
        }
        pending_days.add(today)
        for day in pending_days:
            start, end = bounds(day, zone)
            amount = book.estimate(
                vendor,
                format_instant(Instant(start.value - timedelta(milliseconds=1))),
                format_instant(Instant(end.value - timedelta(milliseconds=1))),
            )
            db.execute(
                "INSERT INTO wallet_inherited_daily VALUES(?,?,?,?) ON CONFLICT DO UPDATE SET c=excluded.c",
                (vendor, day, zone, str(amount)),
            )
        cursor = None
        while True:
            page: dict[str, Any] = book.entries(vendor, cursor, 100)
            for entry in page["entries"]:
                db.execute(
                    "INSERT INTO wallet_inherited_entry VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        vendor,
                        entry["id"],
                        entry["occurredAt"],
                        entry["postedAt"],
                        entry["kind"],
                        entry["R"],
                        entry["C"],
                        entry["A"],
                        entry["note"],
                        entry["originalId"],
                        entry["intervalId"],
                        entry["availablePico"],
                    ),
                )
            cursor = page["nextCursor"]
            if cursor is None:
                break
        db.execute("INSERT INTO wallet_inheritance VALUES(?,?)", (vendor, json.dumps(payload)))
        db.execute(
            "INSERT INTO local_wallet_metadata VALUES(?,?,?) ON CONFLICT(vendor_id) DO UPDATE SET "
            "past_r=excluded.past_r,past_c=excluded.past_c",
            (vendor, target["pastTopUpPico"], target["pastConsumptionPico"]),
        )
    db.execute("INSERT INTO meta VALUES('wallet_inheritance_042','1')")


def apply(db: sqlite3.Connection, vendor: str, scope: str, current: dict[str, Any]) -> dict[str, Any]:
    inherited = seed(db, vendor)
    if inherited is None:
        return current
    keys = "RCAB" if scope == "local" or inherited["remote_initialized"] else "RCA"
    for key in keys:
        current[key] = str(int(inherited["target"][key]) + int(current[key]) - int(inherited[scope][key]))
    return current


def movements(db: sqlite3.Connection, vendor: str, scope: str) -> tuple[str, tuple[Any, ...]]:
    """Queryable wallet-level movements; no request scan or full-history list."""
    inherited = seed(db, vendor)
    assert inherited is not None
    if scope == "local":
        from htu.adapters.sqlite.local_wallet import day_end

        db.create_function("htu_local_day_end", 2, day_end, deterministic=True)
        newer = (
            "SELECT 'v:'||id id,occurred_at,occurred_at posted_at,kind,"
            "CASE WHEN kind='top_up' THEN amount_pico ELSE '0' END r,"
            "CASE WHEN kind IN ('consumption','consumption_delta') THEN amount_pico ELSE '0' END c,"
            "CASE WHEN kind='adjustment' THEN amount_pico ELSE '0' END a,note,NULL original_id,"
            "NULL interval_id,amount_pico available "
            "FROM wallet_entry WHERE vendor_id=? AND seq>? AND kind IN ('top_up','adjustment') "
            "UNION ALL SELECT 'l:'||id,occurred_at,occurred_at,'adjustment',r,c,a,note,original_id,NULL,'0' "
            "FROM local_wallet_posting WHERE vendor_id=? AND id>? "
            "UNION ALL SELECT 'd:'||d.day||':'||d.timezone,htu_local_day_end(d.day,d.timezone),"
            "htu_local_day_end(d.day,d.timezone),'consumption','0',"
            "htu_pico_sub(d.current,COALESCE(b.c,'0')),'0','升级后的本地业务日消费差额',NULL,NULL,'0' "
            "FROM wallet_daily_close d LEFT JOIN wallet_inherited_daily b "
            "ON (b.vendor_id,b.day,b.timezone)=(d.vendor_id,d.day,d.timezone) WHERE d.vendor_id=?"
        )
        args = (vendor, inherited["wallet_seq"], vendor, inherited["local_seq"], vendor)
    else:
        newer = (
            "SELECT p.id,p.occurred_at,p.posted_at,p.kind,p.r,p.c,p.a,p.note,p.original_id,p.interval_id,"
            "'0' available FROM account_posting p JOIN account_segment s ON s.id=p.segment_id "
            "WHERE p.vendor_id=? AND s.kind='remote' AND NOT EXISTS "
            "(SELECT 1 FROM wallet_inherited_entry e WHERE e.vendor_id=p.vendor_id AND e.id=p.id)"
        )
        args = (vendor,)
    return (
        "SELECT id,occurred_at,posted_at,kind,r,c,a,note,original_id,interval_id,available "
        "FROM wallet_inherited_entry WHERE vendor_id=? UNION ALL " + newer,
        (vendor, *args),
    )


def entries(
    db: sqlite3.Connection, vendor: str, cursor: str | None, limit: int, scope: str
) -> dict[str, Any]:
    inherited = seed(db, vendor)
    assert inherited is not None
    try:
        parsed: object = json.loads(cursor) if cursor else ["9999", "~"]
        if (
            not isinstance(parsed, list)
            or len(cast(list[object], parsed)) != 2
            or not all(isinstance(v, str) for v in cast(list[object], parsed))
        ):
            raise ValueError
        edge = cast(list[str], parsed)
    except (ValueError, TypeError) as error:
        raise DomainError("ACCOUNT_CURSOR_INVALID", "流水游标无效") from error
    query, args = movements(db, vendor, scope)
    rows = db.execute(
        "SELECT * FROM (" + query + ") "
        "WHERE (posted_at,id)<(?,?) AND (r<>'0' OR c<>'0' OR a<>'0') "
        "ORDER BY posted_at DESC,id DESC LIMIT ?",
        (*args, *edge, limit + 1),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if scope == "local":
            used = sum(
                max(abs(int(p[k])) for k in "rca")
                for p in db.execute(
                    "SELECT r,c,a FROM local_wallet_posting WHERE vendor_id=? AND original_id=? AND id>?",
                    (vendor, row["id"], inherited["local_seq"]),
                )
            )
            available = max(0, abs(int(row["available"])) - used)
        else:
            # Original availability stays live after remote reclassification.
            original = db.execute("SELECT r,c,a FROM account_posting WHERE id=?", (row["id"],)).fetchone()
            voucher = (
                db.execute("SELECT amount_pico FROM wallet_entry WHERE id=?", (row["id"],)).fetchone()
                if original is None
                else None
            )
            amount = (
                max(abs(int(original[k])) for k in "rca")
                if original
                else abs(int(voucher[0] if voucher else row["available"]))
            )
            used = sum(
                abs(int(p[0]))
                for p in db.execute("SELECT matched FROM account_posting WHERE original_id=?", (row["id"],))
            )
            available = max(0, amount - used)
        interval = (
            db.execute(
                "SELECT e1.observed_at,e2.observed_at FROM account_interval i "
                "JOIN account_endpoint e1 ON e1.id=i.start_endpoint "
                "JOIN account_endpoint e2 ON e2.id=i.end_endpoint WHERE i.id=?",
                (row["interval_id"],),
            ).fetchone()
            if row["interval_id"]
            else None
        )
        result.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "occurredAt": row["occurred_at"],
                "postedAt": row["posted_at"],
                **{k: row[k.lower()] for k in "RCA"},
                "note": row["note"],
                "originalId": row["original_id"],
                "intervalId": row["interval_id"] if scope == "remote" else None,
                "availablePico": str(available),
                "fromAt": interval[0] if interval else None,
                "toAt": interval[1] if interval else None,
            }
        )
    return {
        "entries": result,
        "nextCursor": json.dumps([rows[limit - 1]["posted_at"], rows[limit - 1]["id"]])
        if len(rows) > limit
        else None,
        "revision": db.execute(
            "SELECT value FROM revision WHERE scope=?", ("wallet" if scope == "local" else "account",)
        ).fetchone()[0],
    }
