"""Cold financial evidence, independent of usage retention and exact integer money."""

import hashlib
import sqlite3
from collections import defaultdict
from collections.abc import Generator
from datetime import timedelta
from typing import Any, cast

from htu.adapters.sqlite.counter_codec import compress, decompress
from htu.domain.common.time import Instant, format_instant


def unpack(row: sqlite3.Row) -> dict[str, list[dict[str, Any]]]:
    payload = row["payload"]
    if hashlib.sha256(payload).hexdigest() != row["checksum"]:
        raise ValueError("Financial archive checksum mismatch")
    raw: object = decompress(payload)
    if not isinstance(raw, dict):
        raise ValueError("Unsupported financial archive")
    frame = cast(dict[str, Any], raw)
    if frame.get("v") != 1:
        raise ValueError("Unsupported financial archive")
    if (
        not isinstance(frame.get("activity"), list)
        or not isinstance(frame.get("rollup"), list)
        or not 1 <= len(frame["activity"]) + len(frame["rollup"]) <= 500
    ):
        raise ValueError("Invalid financial archive size")
    if len(frame["activity"]) != row["activity_count"] or len(frame["rollup"]) != row["rollup_count"]:
        raise ValueError("Financial archive directory mismatch")
    for name in ("consumption", "adjustment", "unpriced_count", "incomplete_count"):
        if sum(int(item[name]) for item in frame["rollup"]) != int(row[name]):
            raise ValueError("Financial archive summary mismatch")
    return cast(dict[str, list[dict[str, Any]]], frame)


def compact(db: sqlite3.Connection, *, at: Instant, limit: int = 500) -> int:
    if db.execute("SELECT 1 FROM wallet_day_offset WHERE ready=0 LIMIT 1").fetchone():
        return 0
    cutoff = format_instant(Instant(at.value - timedelta(days=30)))
    activity = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM wallet_activity WHERE occurred_at<? AND "
            "(kind='consumption' OR note IN ('recalc','late_offset',"
            "'legacy_compacted')) AND vendor_id NOT IN (SELECT vendor_id FROM "
            "wallet_validation_job) ORDER BY occurred_at,seq LIMIT ?",
            (cutoff, limit // 2),
        )
    ]
    rollups = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM wallet_rollup WHERE bucket<? ORDER BY bucket,vendor_id LIMIT ?",
            (cutoff, limit - len(activity)),
        )
    ]
    partitions: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(
        lambda: {"activity": [], "rollup": []}
    )
    for row in activity:
        partitions[row["vendor_id"], row["occurred_at"][:10]]["activity"].append(row)
    for row in rollups:
        partitions[row["vendor_id"], row["bucket"][:10]]["rollup"].append(row)
    for (vendor, day), rows in partitions.items():
        tail = db.execute(
            "SELECT * FROM wallet_archive WHERE vendor_id=? AND utc_day=? AND "
            "activity_count+rollup_count<=? ORDER BY id DESC LIMIT 1",
            (vendor, day, 500 - len(rows["activity"]) - len(rows["rollup"])),
        ).fetchone()
        if tail is not None:
            old = unpack(tail)
            rows["activity"] = old["activity"] + rows["activity"]
            rows["rollup"] = old["rollup"] + rows["rollup"]
            db.execute("INSERT INTO temp.allow_compaction VALUES(1)")
            try:
                db.execute("DELETE FROM wallet_archive WHERE id=?", (tail["id"],))
            finally:
                db.execute("DELETE FROM temp.allow_compaction")
        stamps = [row["occurred_at"] for row in rows["activity"]] + [row["bucket"] for row in rows["rollup"]]
        first = [row["seq"] for row in rows["activity"]] + [row["first_seq"] for row in rows["rollup"]]
        last = [row["seq"] for row in rows["activity"]] + [row["last_seq"] for row in rows["rollup"]]
        payload = compress({"v": 1, **rows})
        # Decode and compare before retiring either authoritative hot representation.
        if decompress(payload) != {"v": 1, **rows}:
            raise ValueError("Financial archive verification failed")
        db.execute(
            "INSERT INTO wallet_archive(vendor_id,utc_day,start_at,end_at,"
            "first_seq,last_seq,activity_count,rollup_count,correction_count,"
            "consumption,adjustment,unpriced_count,incomplete_count,payload,"
            "checksum,encoding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                vendor,
                day,
                min(stamps),
                max(stamps),
                min(first),
                max(last),
                len(rows["activity"]),
                len(rows["rollup"]),
                sum(row["kind"] != "consumption" for row in rows["activity"]),
                str(sum(int(row["consumption"]) for row in rows["rollup"])),
                str(sum(int(row["adjustment"]) for row in rows["rollup"])),
                sum(row["unpriced_count"] for row in rows["rollup"]),
                sum(row["incomplete_count"] for row in rows["rollup"]),
                payload,
                hashlib.sha256(payload).hexdigest(),
            ),
        )
    db.executemany("DELETE FROM wallet_activity WHERE seq=?", ((row["seq"],) for row in activity))
    db.executemany(
        "DELETE FROM wallet_rollup WHERE vendor_id=? AND bucket=?",
        ((row["vendor_id"], row["bucket"]) for row in rollups),
    )
    return len(activity) + len(rollups)


def rollup_totals(db: sqlite3.Connection, vendor_id: str) -> Generator[sqlite3.Row, None, None]:
    yield from db.execute(
        "SELECT consumption,adjustment,first_seq,last_seq,unpriced_count,"
        "incomplete_count FROM wallet_rollup WHERE vendor_id=?",
        (vendor_id,),
    )
    for block in db.execute(
        "SELECT * FROM wallet_archive WHERE vendor_id=? AND rollup_count>0", (vendor_id,)
    ):
        unpack(block)
        yield block


def corrections(db: sqlite3.Connection, vendor_id: str, cursor: int, limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for block in db.execute(
        "SELECT * FROM wallet_archive WHERE vendor_id=? AND correction_count>0 "
        "AND first_seq<? ORDER BY last_seq DESC",
        (vendor_id, cursor),
    ):
        if len(selected) >= limit and block["last_seq"] < selected[limit - 1]["seq"]:
            break
        selected.extend(
            row for row in unpack(block)["activity"] if row["kind"] != "consumption" and row["seq"] < cursor
        )
        selected.sort(key=lambda row: row["seq"], reverse=True)
        selected = selected[:limit]
    return selected


def corrected(db: sqlite3.Connection, vendor_id: str, start: str, end: str) -> bool:
    for block in db.execute(
        "SELECT * FROM wallet_archive WHERE vendor_id=? AND correction_count>0 AND end_at>=? AND start_at<?",
        (vendor_id, start, end),
    ):
        if any(
            row["note"] in ("recalc", "late_offset") and start <= row["occurred_at"] < end
            for row in unpack(block)["activity"]
        ):
            return True
    return False
