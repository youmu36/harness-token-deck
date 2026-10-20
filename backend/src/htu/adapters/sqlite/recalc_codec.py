"""Lossless before-state plus changed fields, independent of retained ledger rows."""

import sqlite3
from typing import Any, cast

from htu.adapters.sqlite.counter_codec import compress, decompress
from htu.adapters.sqlite.key_codec import key_bytes
from htu.adapters.sqlite.usage_codec import object_json


def pack(old: dict[str, object], new: dict[str, object]) -> bytes:
    fields = sorted(set(old) | set(new))
    return compress(
        {
            "v": 1,
            "fields": fields,
            "old": old,
            "delta": {key: value for key, value in new.items() if key not in old or old[key] != value},
            "removed": sorted(set(old) - set(new)),
        }
    )


def unpack(payload: bytes) -> tuple[dict[str, Any], dict[str, Any]]:
    raw: object = decompress(payload)
    if not isinstance(raw, dict):
        raise ValueError("Unsupported recalculation audit")
    frame = cast(dict[str, Any], raw)
    if (
        frame.get("v") != 1
        or not isinstance(frame.get("old"), dict)
        or not isinstance(frame.get("delta"), dict)
    ):
        raise ValueError("Unsupported recalculation audit")
    old = cast(dict[str, Any], frame["old"])
    new = {key: value for key, value in old.items() if key not in frame["removed"]} | frame["delta"]
    return old, new


def audit(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        unpack(row["payload"])
        if row["payload"] is not None
        else (object_json(row["old_json"]), object_json(row["new_json"]))
    )


def equivalent(
    old_json: str,
    new_json: str,
    payload: bytes | None,
    next_old: str,
    next_new: str,
    next_payload: bytes | None,
) -> int:
    try:
        before = unpack(payload) if payload is not None else (object_json(old_json), object_json(new_json))
        after = (
            unpack(next_payload)
            if next_payload is not None
            else (object_json(next_old), object_json(next_new))
        )
        return int(before == after)
    except Exception:
        return 0


def compact_audits(db: sqlite3.Connection, *, limit: int = 100) -> int:
    rows = db.execute("SELECT * FROM recalc_item WHERE payload IS NULL LIMIT ?", (limit,)).fetchall()
    db.execute("INSERT INTO temp.allow_compaction VALUES(1)")
    try:
        for row in rows:
            old, new = audit(row)
            payload = pack(old, new)
            if unpack(payload) != (old, new):
                raise ValueError("Recalculation audit verification failed")
            db.execute(
                (
                    "UPDATE recalc_item SET old_json='',new_json='',payload=? WHERE "
                    "batch_id=? AND harness=? AND dedupe_key=?"
                ),
                (payload, row["batch_id"], row["harness"], key_bytes(row["dedupe_key"])),
            )
    finally:
        db.execute("DELETE FROM temp.allow_compaction")
    return len(rows)
