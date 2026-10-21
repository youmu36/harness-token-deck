"""Bounded, quiet-period wallet journals. Financial facts remain in their own books.

Pending rows coalesce by vendor, book, business day and type. No request reads are
needed by a journal query, and pruning the journal never changes money or day books.
"""

import json
import sqlite3
from datetime import timedelta
from typing import Any, Literal, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.token_activity import QUIET, account_state
from htd.application.ports import UnitOfWork
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant


def install(db: sqlite3.Connection) -> None:
    for sql in (
        "CREATE TABLE IF NOT EXISTS wallet_flow_pending("
        "vendor_id TEXT NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,scope TEXT NOT NULL,"
        "day TEXT NOT NULL,kind TEXT NOT NULL,first_at TEXT NOT NULL,last_at TEXT NOT NULL,"
        "received_at TEXT NOT NULL,r TEXT NOT NULL,c TEXT NOT NULL,a TEXT NOT NULL,note TEXT NOT NULL,"
        "source_first INTEGER NOT NULL,source_last INTEGER NOT NULL,"
        "PRIMARY KEY(vendor_id,scope,day,kind)) STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS wallet_flow_pending_due ON wallet_flow_pending(received_at)",
        "CREATE TABLE IF NOT EXISTS wallet_flow("
        "id TEXT PRIMARY KEY,vendor_id TEXT NOT NULL REFERENCES vendor(id) ON DELETE CASCADE,"
        "scope TEXT NOT NULL,day TEXT NOT NULL,kind TEXT NOT NULL,occurred_at TEXT NOT NULL,"
        "posted_at TEXT NOT NULL,r TEXT NOT NULL,c TEXT NOT NULL,a TEXT NOT NULL,"
        "before_r TEXT NOT NULL,before_c TEXT NOT NULL,before_b TEXT NOT NULL,"
        "after_r TEXT NOT NULL,after_c TEXT NOT NULL,after_b TEXT NOT NULL,note TEXT NOT NULL,"
        "revision INTEGER NOT NULL DEFAULT 1,source_first INTEGER NOT NULL DEFAULT 0,"
        "source_last INTEGER NOT NULL DEFAULT 0,calibrated INTEGER NOT NULL DEFAULT 0,"
        "estimate_c TEXT NOT NULL DEFAULT '0') STRICT",
        "CREATE INDEX IF NOT EXISTS wallet_flow_page "
        "ON wallet_flow(vendor_id,scope,occurred_at DESC,id DESC)",
        "CREATE INDEX IF NOT EXISTS wallet_flow_retention ON wallet_flow(posted_at)",
        "CREATE INDEX IF NOT EXISTS wallet_flow_day ON wallet_flow(vendor_id,scope,day,kind,occurred_at)",
        "CREATE TABLE IF NOT EXISTS wallet_flow_notice("
        "flow_id TEXT PRIMARY KEY REFERENCES wallet_flow(id) ON DELETE CASCADE,"
        "id TEXT NOT NULL UNIQUE) STRICT",
        "CREATE INDEX IF NOT EXISTS account_posting_totals ON account_posting(vendor_id,kind,interval_id)",
        "CREATE TABLE IF NOT EXISTS vendor_wallet_visibility("
        "vendor_id TEXT PRIMARY KEY REFERENCES vendor(id) ON DELETE CASCADE,"
        "mode TEXT NOT NULL CHECK(mode IN ('both','local','remote'))) STRICT",
        "CREATE TABLE IF NOT EXISTS local_wallet_flow_override("
        "flow_id TEXT PRIMARY KEY,vendor_id TEXT NOT NULL REFERENCES vendor(id),"
        "source_first INTEGER NOT NULL,source_last INTEGER NOT NULL,day TEXT NOT NULL,"
        "occurred_at TEXT NOT NULL) STRICT",
        "CREATE INDEX IF NOT EXISTS local_wallet_flow_override_source "
        "ON local_wallet_flow_override(vendor_id,day,source_first,source_last)",
        "CREATE TABLE IF NOT EXISTS remote_wallet_correction_total("
        "vendor_id TEXT PRIMARY KEY REFERENCES vendor(id) ON DELETE CASCADE,"
        "r TEXT NOT NULL,c TEXT NOT NULL,a TEXT NOT NULL) STRICT",
        "CREATE TABLE IF NOT EXISTS account_correction_total("
        "vendor_id TEXT PRIMARY KEY REFERENCES vendor(id) ON DELETE CASCADE,"
        "r TEXT NOT NULL,c TEXT NOT NULL,a TEXT NOT NULL) STRICT",
        "CREATE TABLE IF NOT EXISTS remote_wallet_correction_cursor("
        "id INTEGER PRIMARY KEY CHECK(id=1),last_rowid INTEGER NOT NULL) STRICT",
    ):
        db.execute(sql)
    cursor = db.execute("SELECT last_rowid FROM remote_wallet_correction_cursor WHERE id=1").fetchone()
    # Incremental startup catch-up also covers corrections written during a rollback.
    # Page reads never walk the correction history.
    for row in db.execute(
        "SELECT p.*,s.kind segment_kind FROM account_posting p "
        "LEFT JOIN account_segment s ON s.id=p.segment_id "
        "WHERE p.rowid>? AND "
        "p.kind IN ('correction','consumption_correction','balance_correction')",
        (cursor[0] if cursor else 0,),
    ):
        correction_total(
            db,
            row["vendor_id"],
            int(row["r"]),
            int(row["c"]),
            int(row["a"]),
            remote=row["segment_kind"] == "remote",
        )
    db.execute(
        "INSERT INTO remote_wallet_correction_cursor SELECT 1,COALESCE(MAX(rowid),0) FROM account_posting "
        "WHERE true ON CONFLICT(id) DO UPDATE SET last_rowid=excluded.last_rowid"
    )


def correction_total(
    db: sqlite3.Connection, vendor: str, r: int, c: int, a: int, *, remote: bool = True
) -> None:
    for table in (
        ("account_correction_total", "remote_wallet_correction_total")
        if remote
        else ("account_correction_total",)
    ):
        db.execute(
            "INSERT INTO " + table + " VALUES(?,?,?,?) ON CONFLICT(vendor_id) DO UPDATE SET "
            "r=htd_pico_add(r,excluded.r),c=htd_pico_add(c,excluded.c),a=htd_pico_add(a,excluded.a)",
            (vendor, str(r), str(c), str(a)),
        )
    db.execute(
        "INSERT INTO remote_wallet_correction_cursor SELECT 1,COALESCE(MAX(rowid),0) FROM account_posting "
        "WHERE true ON CONFLICT(id) DO UPDATE SET last_rowid=excluded.last_rowid"
    )


def seed_recent(db: sqlite3.Connection) -> None:
    """Carry recent released journals forward once; keep all older financial facts."""
    from datetime import UTC, datetime

    from htd.adapters.sqlite.account_book import AccountBook
    from htd.adapters.sqlite.local_wallet import entries as local_entries
    from htd.adapters.sqlite.local_wallet import remote_summary, summary
    from htd.adapters.sqlite.wallet_closing import timezone

    if db.execute("SELECT 1 FROM meta WHERE key='wallet_flow_seeded_042'").fetchone():
        return
    cutoff = format_instant(Instant(datetime.now(UTC) - timedelta(days=30)))
    zone = ZoneInfo(timezone(db))
    for vendor_row in db.execute("SELECT id FROM vendor").fetchall():
        vendor = vendor_row[0]
        for scope in ("local", "remote"):
            current = summary(db, vendor) if scope == "local" else remote_summary(db, vendor)
            if current is None:
                continue
            r, c, b = (int(current[key]) for key in ("R", "C", "B"))
            cursor: str | None = None
            finished = False
            while not finished:
                page: dict[str, Any] = cast(
                    dict[str, Any],
                    (
                        local_entries(db, vendor, cursor, 100)
                        if scope == "local"
                        else AccountBook(db).entries(vendor, cursor, 100, remote_only=True)
                    ),
                )
                for row in page["entries"]:
                    posted = row.get("postedAt") or row["occurredAt"]
                    if posted < cutoff:
                        finished = True
                        break
                    dr, dc, da = (int(row[key]) for key in "RCA")
                    if not (dr or dc or da):
                        continue
                    before = r - dr, c - dc, b - dr + dc - da
                    day = parse_instant(row["occurredAt"]).value.astimezone(zone).date().isoformat()
                    kind = (
                        "top_up"
                        if dr > 0 and not (dc or da)
                        else "consumption"
                        if dc > 0 and not (dr or da)
                        else "adjustment"
                    )
                    source_first = source_last = 0
                    if scope == "remote":
                        source = db.execute(
                            "SELECT rowid FROM account_posting WHERE id=?", (row["id"],)
                        ).fetchone()
                        source_first = source_last = source[0] if source else 0
                    elif row["id"].startswith("d:"):
                        from htd.adapters.sqlite.wallet_closing import bounds

                        _, source_day, source_zone = row["id"].split(":", 2)
                        start, end = map(format_instant, bounds(source_day, source_zone))
                        source = db.execute(
                            "SELECT COALESCE(MIN(seq),0),COALESCE(MAX(seq),0) FROM wallet_entry "
                            "WHERE vendor_id=? AND occurred_at>=? AND occurred_at<? AND kind='consumption'",
                            (vendor, start, end),
                        ).fetchone()
                        source_first, source_last = source[0], source[1]
                    elif scope == "local" and kind == "top_up":
                        source = db.execute(
                            "SELECT seq FROM wallet_entry WHERE id=? AND vendor_id=?",
                            (row["id"].removeprefix("v:"), vendor),
                        ).fetchone()
                        source_first = source_last = source[0] if source else 0
                    db.execute(
                        "INSERT INTO wallet_flow(id,vendor_id,scope,day,kind,occurred_at,posted_at,r,c,a,"
                        "before_r,before_c,before_b,after_r,after_c,after_b,note,"
                        "source_first,source_last,estimate_c) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            "f:" + str(uuid4()),
                            vendor,
                            scope,
                            day,
                            kind,
                            row["occurredAt"],
                            posted,
                            str(dr),
                            str(dc),
                            str(da),
                            *map(str, before),
                            str(r),
                            str(c),
                            str(b),
                            row["note"],
                            source_first,
                            source_last,
                            row["C"] if scope == "local" and kind == "consumption" else "0",
                        ),
                    )
                    r, c, b = before
                cursor = page["nextCursor"]
                finished |= cursor is None
    db.execute("INSERT INTO meta VALUES('wallet_flow_seeded_042','1')")


def stage(
    db: sqlite3.Connection,
    vendor: str,
    scope: str,
    kind: str,
    occurred: str,
    received: str,
    r: int = 0,
    c: int = 0,
    a: int = 0,
    note: str = "",
    *,
    work: UnitOfWork | None = None,
    source_seq: int = 0,
) -> None:
    if not (r or c or a):
        return
    from htd.adapters.sqlite.wallet_closing import timezone

    if work is not None:
        from htd.adapters.sqlite.writer import UnitOfWork as SqlWork

        transaction = cast(SqlWork, work)
        if transaction.wallet_zone is None:
            transaction.wallet_zone = ZoneInfo(timezone(db))
        day = parse_instant(occurred).value.astimezone(transaction.wallet_zone).date().isoformat()
        key = vendor, scope, day, kind
        old = transaction.wallet_flows.get(key)
        transaction.wallet_flows[key] = (
            min(old[0], occurred) if old else occurred,
            max(old[1], occurred) if old else occurred,
            max(old[2], received) if old else received,
            r + (old[3] if old else 0),
            c + (old[4] if old else 0),
            a + (old[5] if old else 0),
            note,
            (min(old[7], source_seq) if old[7] and source_seq else max(old[7], source_seq))
            if old
            else source_seq,
            max(old[8], source_seq) if old else source_seq,
        )
        return
    day = parse_instant(occurred).value.astimezone(ZoneInfo(timezone(db))).date().isoformat()
    _save_stage(
        db, (vendor, scope, day, kind), (occurred, occurred, received, r, c, a, note, source_seq, source_seq)
    )


def _save_stage(
    db: sqlite3.Connection,
    key: tuple[str, str, str, str],
    value: tuple[str, str, str, int, int, int, str, int, int],
) -> None:
    first, last, received, r, c, a, note, source_first, source_last = value
    db.execute(
        "INSERT INTO wallet_flow_pending VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(vendor_id,scope,day,kind) DO UPDATE SET "
        "last_at=MAX(last_at,excluded.last_at),received_at=MAX(received_at,excluded.received_at),"
        "r=htd_pico_add(r,excluded.r),c=htd_pico_add(c,excluded.c),a=htd_pico_add(a,excluded.a),"
        "note=excluded.note,source_first=CASE WHEN source_first=0 THEN excluded.source_first "
        "WHEN excluded.source_first=0 THEN source_first ELSE MIN(source_first,excluded.source_first) END,"
        "source_last=MAX(source_last,excluded.source_last)",
        (*key, first, last, received, str(r), str(c), str(a), note, source_first, source_last),
    )


def commit_staged(work: UnitOfWork) -> None:
    from htd.adapters.sqlite.writer import UnitOfWork as SqlWork

    transaction = cast(SqlWork, work)
    for key, value in transaction.wallet_flows.items():
        received = max(value[2], transaction.wallet_received.get(key[0], value[2]))
        _save_stage(transaction.connection, key, (value[0], value[1], received, *value[3:]))
    transaction.wallet_flows.clear()
    if transaction.wallet_estimates:
        from htd.adapters.sqlite.wallet_pending import commit_estimates

        commit_estimates(transaction.connection, transaction.wallet_estimates)
        transaction.wallet_estimates.clear()


def restate_source(
    db: sqlite3.Connection,
    vendor: str,
    scope: str,
    kind: str,
    source_seq: int,
    occurred: str,
    delta: int,
    work: UnitOfWork | None = None,
) -> Literal["missing", "pending", "posted"]:
    """Keep the source flow's effective amount current; absorb unposted repricing."""
    if not delta or not source_seq:
        return "missing"
    from htd.adapters.sqlite.wallet_closing import timezone
    from htd.adapters.sqlite.writer import UnitOfWork as SqlWork

    day = parse_instant(occurred).value.astimezone(ZoneInfo(timezone(db))).date().isoformat()
    field = "r" if kind == "top_up" else "c"
    if work is not None:
        transaction = cast(SqlWork, work)
        key = vendor, scope, day, kind
        cached = transaction.wallet_flows.get(key)
        if cached and cached[7] and cached[7] <= source_seq <= cached[8]:
            values = list(cached)
            index = 3 if kind == "top_up" else 4
            values[index] = int(values[index]) + delta
            transaction.wallet_flows[key] = cast(
                tuple[str, str, str, int, int, int, str, int, int], tuple(values)
            )
            return "pending"
    pending = db.execute(
        "SELECT 1 FROM wallet_flow_pending WHERE vendor_id=? AND scope=? AND day=? AND kind=? "
        "AND source_first>0 AND source_first<=? AND source_last>=?",
        (vendor, scope, day, kind, source_seq, source_seq),
    ).fetchone()
    if pending:
        received = cast(SqlWork, work).wallet_received.get(vendor, occurred) if work else occurred
        db.execute(
            "UPDATE wallet_flow_pending SET " + field + "=htd_pico_add(" + field + ",?),"
            "received_at=MAX(received_at,?) WHERE vendor_id=? AND scope=? AND day=? AND kind=?",
            (str(delta), received, vendor, scope, day, kind),
        )
        return "pending"
    balance_delta = delta if kind == "top_up" else -delta
    cursor = db.execute(
        "UPDATE wallet_flow SET "
        + field
        + "=htd_pico_add("
        + field
        + ",?),after_"
        + field
        + "=htd_pico_add(after_"
        + field
        + ",?),after_b=htd_pico_add(after_b,?),revision=revision+1 "
        + (",estimate_c=htd_pico_add(estimate_c,?) " if scope == "local" and kind == "consumption" else "")
        + "WHERE vendor_id=? AND scope=? AND day=? AND kind=? AND source_first>0 "
        "AND source_first<=? AND source_last>=?",
        (
            str(delta),
            str(delta),
            str(balance_delta if scope == "local" else 0),
            *((str(delta),) if scope == "local" and kind == "consumption" else ()),
            vendor,
            scope,
            day,
            kind,
            source_seq,
            source_seq,
        ),
    )
    return "posted" if cursor.rowcount else "missing"


def local_override(db: sqlite3.Connection, vendor: str, source_seq: int, occurred: str) -> sqlite3.Row | None:
    from htd.adapters.sqlite.wallet_closing import timezone

    day = parse_instant(occurred).value.astimezone(ZoneInfo(timezone(db))).date().isoformat()
    return db.execute(
        "SELECT * FROM local_wallet_flow_override WHERE vendor_id=? AND day=? "
        "AND source_first<=? AND source_last>=? LIMIT 1",
        (vendor, day, source_seq, source_seq),
    ).fetchone()


def reprice_local(
    db: sqlite3.Connection,
    work: UnitOfWork,
    vendor: str,
    source_seq: int,
    occurred: str,
    delta: int,
    identity: str,
    *,
    journal: bool = True,
) -> Literal["absorbed", "consumption", "adjustment"]:
    from htd.adapters.sqlite.local_wallet import post

    if not delta:
        return "absorbed"
    override = local_override(db, vendor, source_seq, occurred)
    if override:
        post(
            db,
            work,
            vendor,
            parse_instant(override["occurred_at"]),
            "preserve:" + identity,
            0,
            -delta,
            0,
            "重算估算依据，保留该流水的人工校准金额",
            override["flow_id"],
            journal=False,
        )
        db.execute(
            "UPDATE wallet_flow SET estimate_c=htd_pico_add(estimate_c,?) WHERE id=?",
            (str(delta), override["flow_id"]),
        )
        db.execute(
            "DELETE FROM wallet_flow_notice WHERE flow_id=? AND EXISTS "
            "(SELECT 1 FROM wallet_flow WHERE id=? AND c=estimate_c)",
            (override["flow_id"], override["flow_id"]),
        )
        return "absorbed"
    if not journal:
        return "absorbed"
    state = restate_source(db, vendor, "local", "consumption", source_seq, occurred, delta, work)
    return "absorbed" if state == "pending" else "adjustment" if state == "posted" else "consumption"


def flush(work: UnitOfWork, db: sqlite3.Connection, at: Instant) -> tuple[str, ...]:
    from htd.adapters.sqlite.local_wallet import remote_summary, summary

    cutoff = format_instant(Instant(at.value - QUIET))
    vendors = db.execute(
        "SELECT vendor_id,scope FROM (SELECT vendor_id,scope,received_at FROM wallet_flow_pending "
        "UNION ALL SELECT vendor_id,'local',received_at FROM wallet_recalc_pending) GROUP BY vendor_id,scope "
        "HAVING MAX(received_at)<=? LIMIT 200",
        (cutoff,),
    ).fetchall()
    changed: set[str] = set()
    states: dict[str, tuple[str | None, bool]] = {}
    for item in vendors:
        vendor, scope = item["vendor_id"], item["scope"]
        if vendor not in states:
            states[vendor] = account_state(db, vendor)
        last, busy = states[vendor]
        if busy or (last is not None and last > cutoff):
            continue
        rows: list[dict[str, Any]] = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM wallet_flow_pending WHERE vendor_id=? AND scope=? ORDER BY last_at,kind",
                (vendor, scope),
            )
        ]
        if scope == "local":
            rows.extend(
                dict(row)
                | {
                    "kind": "adjustment",
                    "last_at": row["occurred_at"],
                    "r": "0",
                    "a": "0",
                    "source_first": 0,
                    "source_last": 0,
                    "forced": True,
                }
                for row in db.execute("SELECT * FROM wallet_recalc_pending WHERE vendor_id=?", (vendor,))
            )
        rows.sort(key=lambda row: (row["last_at"], bool(row.get("forced")), row["kind"]))
        current = summary(db, vendor) if scope == "local" else remote_summary(db, vendor)
        if current is None:
            continue
        r, c, b = (int(current[key]) for key in ("R", "C", "B"))
        r -= sum(int(row["r"]) for row in rows)
        c -= sum(int(row["c"]) for row in rows)
        b -= sum(int(row["r"]) - int(row["c"]) + int(row["a"]) for row in rows)
        for row in rows:
            dr, dc, da = (int(row[key]) for key in "rca")
            if not (dr or dc or da) and not row.get("forced"):
                continue
            after = r + dr, c + dc, b + dr - dc + da
            db.execute(
                "INSERT INTO wallet_flow(id,vendor_id,scope,day,kind,occurred_at,posted_at,r,c,a,"
                "before_r,before_c,before_b,after_r,after_c,after_b,note,"
                "source_first,source_last,estimate_c) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "f:" + str(uuid4()),
                    vendor,
                    scope,
                    row["day"],
                    row["kind"],
                    row["last_at"],
                    format_instant(at),
                    str(dr),
                    str(dc),
                    str(da),
                    str(r),
                    str(c),
                    str(b),
                    *map(str, after),
                    row["note"],
                    row["source_first"],
                    row["source_last"],
                    row["c"] if scope == "local" and row["kind"] == "consumption" else "0",
                ),
            )
            r, c, b = after
        db.execute("DELETE FROM wallet_flow_pending WHERE vendor_id=? AND scope=?", (vendor, scope))
        if scope == "local":
            db.execute("DELETE FROM wallet_recalc_pending WHERE vendor_id=?", (vendor,))
        changed.add(vendor)
    # Bounded pruning is shared by the existing background cadence, once per day.
    day = format_instant(at)[:10]
    old = db.execute("SELECT value FROM runtime_state WHERE key='wallet_flow_pruned'").fetchone()
    if old is None or old[0] != day:
        db.execute(
            "DELETE FROM wallet_flow WHERE posted_at<?",
            (format_instant(Instant(at.value - timedelta(days=30))),),
        )
        db.execute(
            "INSERT INTO runtime_state VALUES('wallet_flow_pruned',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (day,),
        )
    if changed:
        work.touch("wallet")
    return tuple(sorted(changed))


def entries(
    db: sqlite3.Connection,
    vendor: str,
    scope: str,
    cursor: str | None,
    limit: int,
    at: Instant,
    kind: str = "all",
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    from datetime import date

    try:
        for value in (start, end):
            if value:
                date.fromisoformat(value)
        if start and end and start > end:
            raise ValueError
    except ValueError as error:
        raise DomainError("ACCOUNT_DATE_INVALID", "请选择有效的起止日期，结束日期不能早于开始日期") from error
    edge = ["9999", "~"]
    if cursor:
        try:
            parsed: object = json.loads(cursor)
            if (
                not isinstance(parsed, list)
                or len(cast(list[object], parsed)) != 2
                or not all(isinstance(v, str) for v in cast(list[object], parsed))
            ):
                raise ValueError
            edge = cast(list[str], parsed)
        except (TypeError, ValueError) as error:
            raise DomainError("ACCOUNT_CURSOR_INVALID", "流水游标无效") from error
    values: list[object] = [vendor, scope, format_instant(Instant(at.value - timedelta(days=30))), *edge]
    where = "vendor_id=? AND scope=? AND posted_at>=? AND (occurred_at,id)<(?,?)"
    if kind != "all":
        where += " AND kind=?"
        values.append(kind)
    if start:
        where += " AND day>=?"
        values.append(start)
    if end:
        where += " AND day<=?"
        values.append(end)
    rows = db.execute(
        "SELECT *, (SELECT id FROM wallet_flow_notice WHERE flow_id=wallet_flow.id) notice_id "
        "FROM wallet_flow WHERE " + where + " ORDER BY occurred_at DESC,id DESC LIMIT ?",
        (*values, limit + 1),
    ).fetchall()
    return {
        "entries": [
            {
                "id": row["id"],
                "kind": row["kind"],
                "occurredAt": row["occurred_at"],
                "postedAt": row["posted_at"],
                "R": row["r"],
                "C": row["c"],
                "A": row["a"],
                "note": row["note"],
                "originalId": None,
                "intervalId": None,
                "availablePico": "0",
                **{
                    name + key: row[prefix + key.lower()]
                    for name, prefix in (("before", "before_"), ("after", "after_"))
                    for key in "RCB"
                },
                "entryRevision": row["revision"],
                "manuallyCalibrated": bool(row["calibrated"]),
                "estimatedConsumptionPico": row["estimate_c"]
                if scope == "local" and row["kind"] == "consumption" and row["source_first"]
                else None,
                "noticeId": row["notice_id"],
            }
            for row in rows[:limit]
        ],
        "nextCursor": json.dumps([rows[limit - 1]["occurred_at"], rows[limit - 1]["id"]])
        if len(rows) > limit
        else None,
        "revision": db.execute("SELECT value FROM revision WHERE scope='wallet'").fetchone()[0],
    }


def correct(
    db: sqlite3.Connection,
    work: UnitOfWork,
    vendor: str,
    values: dict[str, object],
    operation: str,
    at: Instant,
) -> None:
    from htd.adapters.sqlite.local_wallet import post, remote_summary, summary
    from htd.adapters.sqlite.wallet_repo import WalletRepository
    from htd.domain.wallet.vendors import require_active

    require_active(WalletRepository(db).vendor(vendor))
    row = db.execute(
        "SELECT * FROM wallet_flow WHERE id=? AND vendor_id=? AND scope=?",
        (values["entryId"], vendor, values["scope"]),
    ).fetchone()
    if row is None or row["posted_at"] < format_instant(Instant(at.value - timedelta(days=30))):
        raise DomainError("ACCOUNT_ENTRY_INVALID", "流水已过保留期，请更新后重试")
    if row["revision"] != values["expectedEntryRevision"]:
        raise DomainError("ACCOUNT_REVISION_CONFLICT", "流水已变化，请重新读取后校准")
    from htd.adapters.sqlite.pico_codec import decode_pico

    target_r, target_c = decode_pico(str(values["R"])), decode_pico(str(values["C"]))
    if target_r < 0 or target_c < 0 or row["kind"] not in ("top_up", "consumption"):
        raise DomainError("ACCOUNT_AMOUNT_INVALID", "请选择充值或消费流水，金额不能为负数")
    if (row["kind"] == "top_up" and target_c) or (row["kind"] == "consumption" and target_r):
        raise DomainError("ACCOUNT_AMOUNT_INVALID", "请只修改此流水所属类型的金额")
    dr, dc = target_r - int(row["r"]), target_c - int(row["c"])
    current = summary(db, vendor) if row["scope"] == "local" else remote_summary(db, vendor)
    if current is None or int(current["R"]) + dr < 0 or int(current["C"]) + dc < 0:
        raise DomainError("ACCOUNT_AMOUNT_INVALID", "校准后的累计金额不能为负数")
    note = str(values.get("note", ""))
    if row["scope"] == "local":
        if row["kind"] == "consumption" and row["source_first"] and dc:
            db.execute(
                "INSERT OR IGNORE INTO local_wallet_flow_override VALUES(?,?,?,?,?,?)",
                (row["id"], vendor, row["source_first"], row["source_last"], row["day"], row["occurred_at"]),
            )
        movement = dr - dc
        if movement:
            repo = WalletRepository(db)
            base = repo.totals(vendor)
            repo.calibrate(
                work,
                vendor,
                "0",
                target_pico=base.b + movement,
                expected_revision=base.revision,
                at=at,
                operation_id=operation + ":local-balance",
                record_operation=False,
            )
        post(
            db,
            work,
            vendor,
            parse_instant(row["occurred_at"]),
            operation,
            dr,
            dc,
            -movement,
            note or "本地流水金额校准",
            row["id"],
            received=at,
        )
    else:
        from htd.adapters.sqlite.account_book import AccountBook

        book = AccountBook(db)
        segment = db.execute(
            "SELECT id FROM account_segment WHERE vendor_id=? AND kind='remote' "
            "ORDER BY started_at DESC,id DESC LIMIT 1",
            (vendor,),
        ).fetchone()
        if segment is None:
            raise DomainError("REMOTE_ACCOUNT_REQUIRED", "远程账户记录不存在")
        # Correct account totals while preserving the independently observed balance.
        book.post(
            work,
            vendor=vendor,
            segment=segment[0],
            interval=None,
            at=row["occurred_at"],
            kind="correction",
            r=dr,
            c=dc,
            a=dc - dr,
            note=note or "远程流水金额校准",
            operation=operation,
            original=row["id"],
            received=at,
        )
    db.execute(
        "UPDATE wallet_flow SET r=?,c=?,after_r=htd_pico_add(after_r,?),"
        "after_c=htd_pico_add(after_c,?),after_b=htd_pico_add(after_b,?),"
        "note=?,revision=revision+1,calibrated=MAX(calibrated,?) WHERE id=?",
        (
            str(target_r),
            str(target_c),
            str(dr),
            str(dc),
            str(dr - dc if row["scope"] == "local" else 0),
            note or row["note"],
            int(bool(dr or dc)),
            row["id"],
        ),
    )
    if row["scope"] == "local" and row["kind"] == "consumption" and dc:
        if row["estimate_c"] != str(target_c) and row["source_first"]:
            db.execute(
                "INSERT INTO wallet_flow_notice VALUES(?,?) "
                "ON CONFLICT(flow_id) DO UPDATE SET id=excluded.id",
                (row["id"], str(uuid4())),
            )
        else:
            db.execute("DELETE FROM wallet_flow_notice WHERE flow_id=?", (row["id"],))
    work.touch("wallet")


def dismiss_notice(db: sqlite3.Connection, work: UnitOfWork, vendor: str, flow: str, notice: str) -> None:
    cursor = db.execute(
        "DELETE FROM wallet_flow_notice WHERE flow_id=? AND id=? AND EXISTS "
        "(SELECT 1 FROM wallet_flow WHERE id=? AND vendor_id=?)",
        (flow, notice, flow, vendor),
    )
    if cursor.rowcount:
        work.touch("wallet")
