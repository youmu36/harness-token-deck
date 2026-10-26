"""Durable original-voucher members and independent local session publication."""

import sqlite3
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.wallet_closing import bounds, timezone
from htd.application.ports import UnitOfWork
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.activity import QUIET
from htd.domain.usage.model import UsageEvent
from htd.domain.usage.model_key import model_key


def install(db: sqlite3.Connection) -> None:
    from htd.adapters.sqlite.reconciliation_scope import install as install_scope

    install_scope(db)
    for sql in (
        "CREATE TABLE IF NOT EXISTS reconciliation_local_batch("
        "id TEXT PRIMARY KEY,vendor_id TEXT NOT NULL,session_ref TEXT,activity_id TEXT,"
        "start_at TEXT NOT NULL,end_at TEXT NOT NULL,last_received_at TEXT NOT NULL,"
        "revision INTEGER NOT NULL DEFAULT 1,published_revision INTEGER NOT NULL DEFAULT 0) STRICT",
        "CREATE INDEX IF NOT EXISTS reconciliation_local_vendor "
        "ON reconciliation_local_batch(vendor_id,start_at,id)",
        "CREATE TABLE IF NOT EXISTS reconciliation_member("
        "harness TEXT NOT NULL,dedupe_key BLOB NOT NULL,batch_id TEXT NOT NULL "
        "REFERENCES reconciliation_local_batch(id),session_ref TEXT,vendor_id TEXT NOT NULL,"
        "occurred_at TEXT NOT NULL,received_at TEXT NOT NULL,watermark TEXT NOT NULL,"
        "model_key TEXT NOT NULL,group_id TEXT NOT NULL,version_id TEXT NOT NULL,"
        "original_group_id TEXT NOT NULL,original_version_id TEXT NOT NULL,"
        "original_pico TEXT NOT NULL,estimate_pico TEXT NOT NULL,effective_pico TEXT NOT NULL,"
        "voucher_seq INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 1,"
        "incomplete INTEGER NOT NULL,unpriced INTEGER NOT NULL,"
        "PRIMARY KEY(harness,dedupe_key)) STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS reconciliation_member_range "
        "ON reconciliation_member(vendor_id,occurred_at,harness,dedupe_key)",
        "CREATE INDEX IF NOT EXISTS reconciliation_member_batch ON reconciliation_member(batch_id)",
        "CREATE INDEX IF NOT EXISTS reconciliation_member_voucher "
        "ON reconciliation_member(vendor_id,voucher_seq)",
        "CREATE INDEX IF NOT EXISTS reconciliation_member_source "
        "ON reconciliation_member(vendor_id,harness,received_at DESC)",
        "CREATE INDEX IF NOT EXISTS reconciliation_member_activity "
        "ON reconciliation_member(vendor_id,received_at DESC)",
        "CREATE TABLE IF NOT EXISTS reconciliation_interval_member(interval_id TEXT NOT NULL "
        "REFERENCES account_interval(id),harness TEXT NOT NULL,dedupe_key BLOB NOT NULL,"
        "member_revision INTEGER NOT NULL,PRIMARY KEY(interval_id,harness,dedupe_key)) "
        "STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS reconciliation_interval_request "
        "ON reconciliation_interval_member(harness,dedupe_key)",
        "CREATE INDEX IF NOT EXISTS account_segment_reconciliation ON account_segment(vendor_id,id)",
        "CREATE TABLE IF NOT EXISTS session_wallet_pending("
        "activity_id TEXT NOT NULL,vendor_id TEXT NOT NULL,day TEXT NOT NULL,first_at TEXT NOT NULL,"
        "last_at TEXT NOT NULL,received_at TEXT NOT NULL,c TEXT NOT NULL,source_first INTEGER NOT NULL,"
        "source_last INTEGER NOT NULL,PRIMARY KEY(activity_id,vendor_id,day)) STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS session_wallet_due ON session_wallet_pending(received_at)",
        "CREATE TABLE IF NOT EXISTS wallet_flow_member(flow_id TEXT NOT NULL,harness TEXT NOT NULL,"
        "dedupe_key BLOB NOT NULL,source_seq INTEGER NOT NULL,PRIMARY KEY(flow_id,harness,dedupe_key)) "
        "STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS wallet_flow_member_source ON wallet_flow_member(source_seq,flow_id)",
    ):
        db.execute(sql)
    db.execute(
        "INSERT OR IGNORE INTO runtime_state SELECT 'reconciliation_backfill',"
        "CASE WHEN EXISTS(SELECT 1 FROM ledger) THEN "
        '\'{"harness":"","key":"","done":false}\' ELSE '
        '\'{"harness":"","key":"","done":true}\' END'
    )


def backfill(db: sqlite3.Connection, *, limit: int = 500) -> int:
    from htd.adapters.sqlite.session_core import remember_ledger
    from htd.adapters.sqlite.usage_codec import dumps, object_json

    saved = db.execute("SELECT value FROM runtime_state WHERE key='reconciliation_backfill'").fetchone()
    state = object_json(saved[0])
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
    db.execute("UPDATE runtime_state SET value=? WHERE key='reconciliation_backfill'", (dumps(state),))
    return len(rows)


def valued(db: sqlite3.Connection, event: UsageEvent, pico: int, attribution: tuple[str, str, str]) -> None:
    identity = event.harness.value, key_bytes(event.dedupe_key)
    member = db.execute("SELECT * FROM session_request WHERE harness=? AND dedupe_key=?", identity).fetchone()
    if member is None:
        return
    activity = db.execute(
        "SELECT batch_id FROM session_request_batch WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    voucher = db.execute(
        "SELECT * FROM wallet_entry WHERE usage_harness=? AND usage_key=? AND kind='consumption'", identity
    ).fetchone()
    group, version, vendor = attribution
    if voucher is not None:
        vendor = voucher["vendor_id"]
    raw = db.execute("SELECT * FROM ledger WHERE harness=? AND dedupe_key=?", identity).fetchone()
    from htd.adapters.sqlite.explorer_storage import detail

    # Freeze the same sample eligibility used by local statistics before detail retirement.
    quality = (int(not detail(db, dict(raw))["eligible_records"]), raw["unpriced"]) if raw else (1, 1)
    batch_id = str(
        uuid5(
            NAMESPACE_URL,
            "htd:reconciliation:"
            + vendor
            + ":"
            + (
                activity[0]
                if activity
                else (member["session_ref"] or "unassigned") + ":" + event.occurred_at[:10]
            ),
        )
    )
    previous = db.execute(
        "SELECT * FROM reconciliation_member WHERE harness=? AND dedupe_key=?", identity
    ).fetchone()
    if previous and (
        previous["estimate_pico"],
        previous["vendor_id"],
        previous["group_id"],
        previous["version_id"],
        previous["incomplete"],
        previous["unpriced"],
    ) == (str(pico), vendor, group, version, *quality):
        return
    db.execute(
        "INSERT INTO reconciliation_local_batch(id,vendor_id,session_ref,activity_id,start_at,end_at,"
        "last_received_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "start_at=MIN(start_at,excluded.start_at),end_at=MAX(end_at,excluded.end_at),"
        "last_received_at=MAX(last_received_at,excluded.last_received_at),revision=revision+1",
        (
            batch_id,
            vendor,
            member["session_ref"],
            activity[0] if activity else None,
            event.occurred_at,
            event.occurred_at,
            member["received_at"],
        ),
    )
    db.execute(
        "INSERT INTO reconciliation_member VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(harness,dedupe_key) DO UPDATE SET batch_id=excluded.batch_id,"
        "vendor_id=excluded.vendor_id,group_id=excluded.group_id,version_id=excluded.version_id,"
        "estimate_pico=excluded.estimate_pico,effective_pico=excluded.effective_pico,"
        "voucher_seq=excluded.voucher_seq,revision=revision+1,"
        "incomplete=excluded.incomplete,unpriced=excluded.unpriced",
        (
            *identity,
            batch_id,
            member["session_ref"],
            vendor,
            event.occurred_at,
            member["received_at"],
            member["watermark"],
            model_key(event.model_id) if event.model_id else "",
            group,
            version,
            group,
            version,
            voucher["amount_pico"] if voucher else str(pico),
            str(pico),
            str(pico),
            voucher["seq"] if voucher else 0,
            1,
            *quality,
        ),
    )
    from htd.adapters.sqlite.calibration_projection import refresh

    refresh(
        db,
        event.harness.value,
        key_bytes(event.dedupe_key),
        raw["billed_at"] if raw else event.occurred_at,
        previous,
    )


def freeze_interval(db: sqlite3.Connection, interval_id: str, completed: str) -> None:
    from htd.adapters.sqlite.reconciliation_scope import freeze

    freeze(db, interval_id, completed)


def stage(db: sqlite3.Connection, vendor: str, seq: int, occurred: str, amount: int) -> bool:
    row = db.execute(
        "SELECT b.batch_id,r.received_at FROM wallet_entry w JOIN session_request_batch b "
        "ON b.harness=w.usage_harness AND b.dedupe_key=w.usage_key "
        "JOIN session_request r ON r.harness=b.harness AND r.dedupe_key=b.dedupe_key "
        "WHERE w.vendor_id=? AND w.seq=? AND w.kind='consumption'",
        (vendor, seq),
    ).fetchone()
    if row is None:
        return False
    day = parse_instant(occurred).value.astimezone(ZoneInfo(timezone(db))).date().isoformat()
    db.execute(
        "INSERT INTO session_wallet_pending VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(activity_id,vendor_id,day) DO UPDATE SET last_at=MAX(last_at,excluded.last_at),"
        "received_at=MAX(received_at,excluded.received_at),c=htd_pico_add(c,excluded.c),"
        "source_first=MIN(source_first,excluded.source_first),source_last=MAX(source_last,excluded.source_last)",
        (row[0], vendor, day, occurred, occurred, row[1], str(amount), seq, seq),
    )
    return True


def flush(work: UnitOfWork, db: sqlite3.Connection, at: Instant) -> set[str]:
    from htd.adapters.sqlite.local_wallet import summary
    from htd.adapters.sqlite.session_activity import seal

    seal(db, at)
    due = db.execute(
        "SELECT p.* FROM session_wallet_pending p JOIN session_activity_batch b ON b.id=p.activity_id "
        "WHERE b.state='sealed' AND p.received_at<=? ORDER BY p.last_at,p.activity_id LIMIT 200",
        (format_instant(Instant(at.value - QUIET)),),
    ).fetchall()
    changed: set[str] = set()
    for row in due:
        vendor, day = row["vendor_id"], row["day"]
        pending = [0, 0, 0]
        for value in db.execute(
            "SELECT '0',c,'0' FROM session_wallet_pending WHERE vendor_id=? UNION ALL "
            "SELECT r,c,a FROM wallet_flow_pending WHERE vendor_id=? AND scope='local' UNION ALL "
            "SELECT '0',c,'0' FROM wallet_recalc_pending WHERE vendor_id=?",
            (vendor, vendor, vendor),
        ):
            for index, amount in enumerate(value):
                pending[index] += int(amount)
        current = summary(db, vendor)
        before = (
            int(current["R"]) - pending[0],
            int(current["C"]) - pending[1],
            int(current["B"]) - pending[0] + pending[1] - pending[2],
        )
        amount = int(row["c"])
        flow = "f:" + str(uuid4())
        db.execute(
            "INSERT INTO wallet_flow(id,vendor_id,scope,day,kind,occurred_at,posted_at,r,c,a,"
            "before_r,before_c,before_b,after_r,after_c,after_b,note,source_first,source_last,estimate_c) "
            "VALUES(?,?,'local',?,'consumption',?,?,'0',?,'0',?,?,?,?,?,?,?, ?,?,?)",
            (
                flow,
                vendor,
                day,
                row["last_at"],
                format_instant(at),
                str(amount),
                *map(str, before),
                str(before[0]),
                str(before[1] + amount),
                str(before[2] - amount),
                "会话活动批次消费",
                row["source_first"],
                row["source_last"],
                str(amount),
            ),
        )
        db.execute(
            "INSERT INTO wallet_flow_member SELECT ?,m.harness,m.dedupe_key,m.voucher_seq "
            "FROM reconciliation_member m JOIN reconciliation_local_batch b ON b.id=m.batch_id "
            "WHERE b.activity_id=? AND m.vendor_id=? AND m.voucher_seq BETWEEN ? AND ? "
            "AND m.occurred_at>=? AND m.occurred_at<?",
            (
                flow,
                row["activity_id"],
                vendor,
                row["source_first"],
                row["source_last"],
                *map(format_instant, bounds(day, timezone(db))),
            ),
        )
        db.execute(
            "UPDATE session_request SET published_revision=revision WHERE (harness,dedupe_key) IN "
            "(SELECT harness,dedupe_key FROM wallet_flow_member WHERE flow_id=?)",
            (flow,),
        )
        db.execute(
            "UPDATE reconciliation_local_batch SET published_revision=revision WHERE activity_id=? "
            "AND vendor_id=?",
            (row["activity_id"], vendor),
        )
        db.execute(
            "DELETE FROM session_wallet_pending WHERE activity_id=? AND vendor_id=? AND day=?",
            (row["activity_id"], vendor, day),
        )
        changed.add(vendor)
    return changed
