"""Resumable audit verification; new writes are preserved and publication is atomic."""

import sqlite3
from datetime import timedelta

from htd.adapters.sqlite.wallet_archive import unpack
from htd.application.ports import UnitOfWork
from htd.domain.common.time import Instant, format_instant, parse_instant


def step(
    db: sqlite3.Connection, work: UnitOfWork, *, at: Instant, limit: int = 500, force: bool = False
) -> tuple[str, ...]:
    state = db.execute("SELECT value FROM runtime_state WHERE key='last_wallet_check_at'").fetchone()
    if not force and state and at.value - parse_instant(state[0]).value < timedelta(days=1):
        return ()
    checked = db.execute("SELECT value FROM runtime_state WHERE key='wallet_validation_vendor'").fetchone()
    last = checked[0] if checked else ""
    vendor = db.execute("SELECT id FROM vendor WHERE id>? ORDER BY id LIMIT 1", (last,)).fetchone()
    if vendor is None:
        db.execute("DELETE FROM runtime_state WHERE key='wallet_validation_vendor'")
        db.execute(
            "INSERT INTO runtime_state VALUES('last_wallet_check_at',?) ON "
            "CONFLICT(key) DO UPDATE SET value=excluded.value",
            (format_instant(at),),
        )
        return ()
    vendor_id = vendor[0]
    job = db.execute("SELECT * FROM wallet_validation_job WHERE vendor_id=?", (vendor_id,)).fetchone()
    if job is None:
        total = db.execute("SELECT * FROM wallet_total WHERE vendor_id=?", (vendor_id,)).fetchone()
        summary = db.execute("SELECT * FROM wallet_summary WHERE vendor_id=?", (vendor_id,)).fetchone()
        db.execute(
            "INSERT INTO wallet_validation_job(vendor_id,target_seq,captured_r,captured_c,captured_a,"
            "captured_unpriced,captured_incomplete) VALUES(?,?,?,?,?,?,?)",
            (
                vendor_id,
                db.execute("SELECT value FROM wallet_sequence WHERE id=1").fetchone()[0],
                total["r"] if total else "0",
                total["c"] if total else "0",
                total["a"] if total else "0",
                summary["unpriced"] if summary else 0,
                summary["incomplete"] if summary else 0,
            ),
        )
        job = db.execute("SELECT * FROM wallet_validation_job WHERE vendor_id=?", (vendor_id,)).fetchone()
    assert job is not None
    r, c, a = (int(job[key]) for key in "rca")
    unpriced, incomplete = job["unpriced"], job["incomplete"]
    calibrated = job["calibrated_at"]
    last_seq = job["last_seq"]
    used = 0
    phase = job["phase"]
    if phase == 0:
        records = list(
            db.execute(
                "SELECT e.*,NOT EXISTS(SELECT 1 FROM wallet_entry n WHERE n.usage_harness=e.usage_harness "
                "AND n.usage_key=e.usage_key AND n.seq>e.seq AND n.seq<=?) latest FROM wallet_entry e "
                "WHERE e.vendor_id=? AND e.seq>? AND e.seq<=? ORDER BY e.seq LIMIT ?",
                (job["target_seq"], vendor_id, job["cursor_seq"], job["target_seq"], limit),
            )
        )
        for row in records:
            last_seq = max(last_seq, row["seq"])
            amount = int(row["amount_pico"])
            if row["kind"] == "top_up":
                r += amount
            elif row["kind"] in ("consumption", "consumption_delta"):
                c += amount
                if row["latest"]:
                    unpriced += row["unpriced"]
                    incomplete += row["incomplete"]
            else:
                a += amount
                if row["note"] == "calibration":
                    calibrated = max(calibrated or row["occurred_at"], row["occurred_at"])
        used += len(records)
        db.execute(
            "UPDATE wallet_validation_job SET cursor_seq=? WHERE vendor_id=?",
            (records[-1]["seq"] if records else job["cursor_seq"], vendor_id),
        )
        if len(records) < limit:
            phase = 1
    if phase == 1 and used < limit:
        records = list(
            db.execute(
                "SELECT * FROM wallet_rollup WHERE vendor_id=? AND bucket>? ORDER BY bucket LIMIT ?",
                (vendor_id, job["cursor_bucket"], limit - used),
            )
        )
        for row in records:
            last_seq = max(last_seq, row["last_seq"])
            c += int(row["consumption"])
            a += int(row["adjustment"])
            unpriced += row["unpriced_count"]
            incomplete += row["incomplete_count"]
        if len(records) < limit - used:
            phase = 2
        used += len(records)
        if records:
            db.execute(
                "UPDATE wallet_validation_job SET cursor_bucket=? WHERE vendor_id=?",
                (records[-1]["bucket"], vendor_id),
            )
    if phase == 2 and used < limit:
        # Each cold frame is <=500 rows; do not retain a whole history in memory.
        block = db.execute(
            "SELECT * FROM wallet_archive WHERE vendor_id=? AND id>? ORDER BY id LIMIT 1",
            (vendor_id, job["cursor_archive"]),
        ).fetchone()
        if block:
            unpack(block)
            last_seq = max(last_seq, block["last_seq"])
            c += int(block["consumption"])
            a += int(block["adjustment"])
            unpriced += block["unpriced_count"]
            incomplete += block["incomplete_count"]
            db.execute(
                "UPDATE wallet_validation_job SET cursor_archive=? WHERE vendor_id=?",
                (block["id"], vendor_id),
            )
        else:
            phase = 3
    db.execute(
        "UPDATE wallet_validation_job SET "
        "r=?,c=?,a=?,unpriced=?,incomplete=?,phase=?,calibrated_at=?,last_"
        "seq=? WHERE vendor_id=?",
        (str(r), str(c), str(a), unpriced, incomplete, phase, calibrated, last_seq, vendor_id),
    )
    if phase != 3:
        return ()
    current = db.execute("SELECT * FROM wallet_total WHERE vendor_id=?", (vendor_id,)).fetchone()
    summary = db.execute("SELECT * FROM wallet_summary WHERE vendor_id=?", (vendor_id,)).fetchone()

    def tail(key: str) -> int:
        value = current[key] if current else "0"
        original = job["captured_" + key]
        return 0 if value == original else int(value) - int(original)

    expected = tuple(str(value + tail(key)) for key, value in zip("rca", (r, c, a), strict=True))
    expected_quality = (
        unpriced + (summary["unpriced"] if summary else 0) - job["captured_unpriced"],
        incomplete + (summary["incomplete"] if summary else 0) - job["captured_incomplete"],
    )
    newest = db.execute(
        "SELECT seq FROM wallet_entry WHERE vendor_id=? ORDER BY seq DESC LIMIT 1", (vendor_id,)
    ).fetchone()
    last_seq = max(last_seq, newest[0] if newest else 0)
    changed = (
        current is None
        or summary is None
        or expected != tuple(current[key] for key in "rca")
        or expected_quality != (summary["unpriced"], summary["incomplete"])
        or current["last_seq"] != last_seq
    )
    if changed:
        db.execute(
            "INSERT INTO wallet_total VALUES(?,?,?,?,?) ON CONFLICT(vendor_id) "
            "DO UPDATE SET r=excluded.r,c=excluded.c,a=excluded.a,last_seq=exc"
            "luded.last_seq",
            (vendor_id, *expected, last_seq),
        )
        db.execute(
            "INSERT INTO wallet_summary VALUES(?,?,?,?) ON CONFLICT(vendor_id) "
            "DO UPDATE SET unpriced=excluded.unpriced,incomplete=excluded.inco"
            "mplete,calibrated_at=excluded.calibrated_at",
            (vendor_id, *expected_quality, calibrated),
        )
        db.execute(
            "INSERT INTO diagnostic_log VALUES(NULL,?,NULL,'wallet_totals_rebuilt',?)",
            (format_instant(at), "供应商 " + vendor_id[:100] + ": 累计与凭证不一致，已重建"),
        )
        work.touch("wallet")
    db.execute("DELETE FROM wallet_validation_job WHERE vendor_id=?", (vendor_id,))
    db.execute(
        "INSERT INTO runtime_state VALUES('wallet_validation_vendor',?) ON "
        "CONFLICT(key) DO UPDATE SET value=excluded.value",
        (vendor_id,),
    )
    if db.execute("SELECT 1 FROM vendor WHERE id>? LIMIT 1", (vendor_id,)).fetchone() is None:
        db.execute("DELETE FROM runtime_state WHERE key='wallet_validation_vendor'")
        db.execute(
            "INSERT INTO runtime_state VALUES('last_wallet_check_at',?) ON "
            "CONFLICT(key) DO UPDATE SET value=excluded.value",
            (format_instant(at),),
        )
    return (vendor_id,) if changed else ()
