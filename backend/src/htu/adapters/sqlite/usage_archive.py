"""Bounded monthly partitions of lossless minute counters, without per-minute indexes."""

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Generator, Sequence
from datetime import timedelta
from typing import Any

from htu.adapters.sqlite.counter_codec import merge_minutes, pack
from htu.adapters.sqlite.counter_codec import unpack as decode_frame
from htu.adapters.sqlite.transaction import require_transaction
from htu.adapters.sqlite.usage_codec import dumps
from htu.adapters.sqlite.usage_hours import hours
from htu.application.ports import UnitOfWork
from htu.domain.common.time import Instant, format_instant

type ArchiveRow = dict[str, Any]


def unpack(payload: bytes) -> list[ArchiveRow]:
    return decode_frame(payload)


def _insert(db: sqlite3.Connection, rows: Sequence[ArchiveRow]) -> None:
    # Keep each compressed payload specific to a model. SQL metadata can then
    # skip unrelated blocks before allocating or decompressing their payload.
    partitions: dict[tuple[str, str, str], list[ArchiveRow]] = defaultdict(list)
    for row in rows:
        partitions[row["harness"], row["bucket"][:10], row["model_key"]].append(row)
    for partition in partitions.values():
        _insert_model(db, partition)


def _insert_model(db: sqlite3.Connection, rows: Sequence[ArchiveRow]) -> None:
    rows = merge_minutes(rows)
    keys = {row["model_key"] for row in rows}
    model_key = rows[0]["model_key"] if len(keys) == 1 else None
    day = rows[0]["bucket"][:10]
    # Merge one bounded tail in the same partition; late increments remain additive.
    if model_key is not None:
        tail = db.execute(
            "SELECT id,payload,checksum FROM usage_archive WHERE harness=? AND model_key=? AND "
            "start_at>=? AND start_at<? AND encoding=2 AND minute_count<=? ORDER BY minute_count,id LIMIT 1",
            (rows[0]["harness"], model_key, day, day + "Z", 500 - len(rows)),
        ).fetchone()
        if tail is not None:
            _verify(tail["payload"], tail["checksum"])
            rows = merge_minutes([*unpack(tail["payload"]), *rows])
            db.execute("DELETE FROM usage_archive WHERE id=?", (tail["id"],))
    models: dict[str, ArchiveRow] = {}
    for row in rows:
        if row["model_id"] is not None:
            candidate = {key: row[key] for key in ("model_key", "model_id", "bucket")}
            candidate["tie_key"] = json.dumps(
                [row["vendor_id"], row["model_key"]], ensure_ascii=False, separators=(",", ":")
            )
            old = models.get(row["model_key"])
            if old is None or (candidate["bucket"], candidate["tie_key"]) > (old["bucket"], old["tie_key"]):
                models[row["model_key"]] = candidate
    body = pack(rows)
    db.execute(
        "INSERT INTO usage_archive(month,harness,start_at,end_at,records,models_json,payload,hours_json,"
        "model_key,encoding,minute_count,hours_payload,checksum) VALUES (?,?,?,?,?,?,?,NULL,?,2,?,?,?)",
        (
            rows[0]["bucket"][:7],
            rows[0]["harness"],
            min(row["bucket"] for row in rows),
            max(row["bucket"] for row in rows),
            sum(row["records"] for row in rows),
            dumps(list(models.values())),
            body,
            model_key,
            len(rows),
            pack(hours(rows)),
            hashlib.sha256(body).hexdigest(),
        ),
    )


def archive_minutes(db: sqlite3.Connection, work: UnitOfWork, *, at: Instant, limit: int = 500) -> int:
    require_transaction(work, db)
    cutoff = format_instant(
        Instant((at.value - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0))
    )
    # Each block belongs to one source and UTC day; month partitions can later
    # support longer range queries without expanding request identities.
    rows = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM usage_rollup WHERE bucket<? ORDER BY bucket,harness,vendor_id,model_key LIMIT ?",
            (cutoff, limit),
        )
    ]
    partitions: dict[tuple[str, str], list[ArchiveRow]] = defaultdict(list)
    for row in rows:
        partitions[row["harness"], row["bucket"][:10]].append(row)
    for partition in partitions.values():
        _insert(db, partition)
    db.executemany(
        "DELETE FROM usage_rollup WHERE harness=? AND bucket=? AND vendor_id=? AND model_key=?",
        ((row["harness"], row["bucket"], row["vendor_id"], row["model_key"]) for row in rows),
    )
    if rows:
        work.touch("usage")
        work.touch("sources")
    return len(rows)


def archived_rows(
    db: sqlite3.Connection,
    start: str,
    end: str,
    harness: str = "all",
    *,
    model_key: str | None = None,
    coarse: bool = False,
) -> Generator[ArchiveRow, None, None]:
    columns = {row[1] for row in db.execute("PRAGMA table_info(usage_archive)")}
    if "encoding" not in columns:
        yield from _legacy_rows(
            db, start, end, harness, model_key=model_key, coarse=coarse, hours_json="hours_json" in columns
        )
        return
    clause, harness_args = ("", ()) if harness == "all" else (" AND harness=?", (harness,))
    select = (
        "SELECT id,CASE WHEN ? AND (hours_json IS NOT NULL OR hours_payload IS "
        "NOT NULL) AND start_at>=? AND end_at<? "
        "THEN NULL ELSE payload END payload,hours_json,hours_payload,checksum,"
        "start_at,end_at FROM usage_archive "
        "WHERE end_at>=? AND start_at<?" + clause
    )
    args = (int(coarse), _ceil_hour(start), _floor_hour(end), start, end, *harness_args)
    if model_key is None:
        query, params = select + " ORDER BY id", args
    else:
        query = (
            select
            + " AND model_key=? UNION ALL "
            + select
            + " AND model_key IS NULL AND (?='' OR EXISTS(SELECT 1 FROM "
            "json_each(models_json) WHERE json_extract(value,'$.model_key')=?)) "
            "ORDER BY id"
        )
        params = (*args, model_key, *args, model_key, model_key)
    cursor = db.execute(query, params)
    try:
        for block in cursor:
            if block["payload"] is None:
                counters = (
                    decode_frame(block["hours_payload"])
                    if block["hours_payload"] is not None
                    else json.loads(block["hours_json"])
                )
            else:
                _verify(block["payload"], block["checksum"])
                counters = unpack(block["payload"])
            for row in counters:
                if start <= row["bucket"] < end and (model_key is None or row["model_key"] == model_key):
                    yield row
    finally:
        cursor.close()


def _legacy_rows(
    db: sqlite3.Connection,
    start: str,
    end: str,
    harness: str,
    *,
    model_key: str | None,
    coarse: bool,
    hours_json: bool,
) -> Generator[ArchiveRow, None, None]:
    clause, args = ("", ()) if harness == "all" else (" AND harness=?", (harness,))
    if model_key is not None and model_key != "":
        clause += (
            " AND EXISTS(SELECT 1 FROM json_each(models_json) WHERE json_extract(value,'$.model_key')=?)"
        )
        args += (model_key,)
    hour = "hours_json" if hours_json else "NULL"
    for block in db.execute(
        f"SELECT payload,{hour} hours_json,start_at,end_at FROM usage_archive WHERE end_at>=? AND start_at<?"
        + clause
        + " ORDER BY id",
        (start, end, *args),
    ):
        if (
            coarse
            and block["hours_json"] is not None
            and block["start_at"] >= _ceil_hour(start)
            and block["end_at"] < _floor_hour(end)
        ):
            rows = json.loads(block["hours_json"])
        else:
            rows = unpack(block["payload"])
        for row in rows:
            if start <= row["bucket"] < end and (model_key is None or row["model_key"] == model_key):
                yield row


def _floor_hour(stamp: str) -> str:
    return stamp[:13] + ":00:00.000Z"


def _ceil_hour(stamp: str) -> str:
    from htu.domain.common.time import parse_instant

    floor = _floor_hour(stamp)
    return (
        floor if stamp == floor else format_instant(Instant(parse_instant(floor).value + timedelta(hours=1)))
    )


def optimize_archives(db: sqlite3.Connection, *, limit: int = 1) -> int:
    blocks = db.execute(
        "SELECT id,payload FROM usage_archive WHERE encoding<>2 OR model_key IS NULL LIMIT ?", (limit,)
    ).fetchall()
    for block in blocks:
        _insert(db, unpack(block["payload"]))
        db.execute("DELETE FROM usage_archive WHERE id=?", (block["id"],))
    return len(blocks)


def _verify(payload: bytes, checksum: str | None) -> None:
    if checksum is not None and hashlib.sha256(payload).hexdigest() != checksum:
        raise ValueError("Usage archive checksum mismatch")


def expire_archives(db: sqlite3.Connection, cutoff: str, *, limit: int) -> int:
    """Preserve the live suffix of a boundary block, in the caller's transaction."""
    count = 0
    while count < limit:
        block = db.execute(
            "SELECT id,payload,start_at,end_at,minute_count,checksum FROM "
            "usage_archive WHERE start_at<? ORDER BY start_at,id LIMIT 1",
            (cutoff,),
        ).fetchone()
        if block is None:
            break
        if block["end_at"] < cutoff and 0 < block["minute_count"] <= limit - count:
            count += block["minute_count"]
            db.execute("DELETE FROM usage_archive WHERE id=?", (block["id"],))
            continue
        _verify(block["payload"], block["checksum"])
        rows = sorted(unpack(block["payload"]), key=lambda row: row["bucket"])
        expired = [row for row in rows if row["bucket"] < cutoff][: limit - count]
        count += len(expired)
        remaining = rows[len(expired) :]
        db.execute("DELETE FROM usage_archive WHERE id=?", (block["id"],))
        if remaining:
            _insert(db, remaining)
    return count


def archive_preview(db: sqlite3.Connection, cutoff: str) -> tuple[int, str | None, str | None]:
    result = db.execute(
        "SELECT COALESCE(SUM(records),0),MIN(start_at),MAX(end_at) FROM usage_archive WHERE end_at<?",
        (cutoff,),
    ).fetchone()
    count, earliest, latest = result
    for block in db.execute(
        "SELECT payload FROM usage_archive WHERE start_at<? AND end_at>=?", (cutoff, cutoff)
    ):
        for row in unpack(block[0]):
            if row["bucket"] < cutoff:
                count += row["records"]
                earliest = min(earliest, row["bucket"]) if earliest else row["bucket"]
                latest = max(latest, row["bucket"]) if latest else row["bucket"]
    return count, earliest, latest


def insert_counters(db: sqlite3.Connection, rows: Sequence[ArchiveRow]) -> None:
    """Re-publish a bounded retained portion after verified splitting."""
    _insert(db, rows)
