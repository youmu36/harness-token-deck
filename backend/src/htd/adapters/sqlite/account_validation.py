"""Rebuild one account projection in bounded, restartable interval batches."""

import json
import sqlite3
from datetime import timedelta
from typing import Any, cast

from htd.application.ports import UnitOfWork
from htd.domain.common.time import Instant, format_instant, parse_instant


def _save(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO runtime_state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def step(
    db: sqlite3.Connection, work: UnitOfWork, *, at: Instant, force: bool = False, limit: int = 500
) -> tuple[str, ...]:
    last_check = db.execute("SELECT value FROM runtime_state WHERE key='last_account_check_at'").fetchone()
    if not force and last_check and at.value - parse_instant(last_check[0]).value < timedelta(days=1):
        return ()
    saved = db.execute("SELECT value FROM runtime_state WHERE key='account_validation_job'").fetchone()
    marker = db.execute("SELECT value FROM runtime_state WHERE key='account_validation_segment'").fetchone()
    segment = db.execute(
        "SELECT * FROM account_segment WHERE kind='remote' AND id>? ORDER BY id LIMIT 1",
        (marker[0] if marker else "",),
    ).fetchone()
    if segment is None:
        db.execute(
            "DELETE FROM runtime_state WHERE key IN ('account_validation_job','account_validation_segment')"
        )
        _save(db, "last_account_check_at", format_instant(at))
        return ()
    mutation = db.execute(
        "SELECT value FROM runtime_state WHERE key=?", ("account_interval_mutation:" + segment["id"],)
    ).fetchone()
    revision = mutation[0] if mutation else "0"
    job: dict[str, Any] | None = cast(dict[str, Any], json.loads(saved[0])) if saved else None
    if job is None or job["segment"] != segment["id"] or job["revision"] != revision:
        job = {
            "segment": segment["id"],
            "revision": revision,
            "target": db.execute("SELECT COALESCE(MAX(rowid),0) FROM account_interval").fetchone()[0],
            "captured": {key: segment[key] for key in "rca"},
            "capturedMethods": json.loads(segment["methods"]),
            "cursor": "",
            "r": "0",
            "c": "0",
            "a": "0",
            "methods": [],
        }
    rows = list(
        db.execute(
            "SELECT id,r,c,a,method FROM account_interval WHERE segment_id=? AND id>? "
            "AND rowid<=? ORDER BY id LIMIT ?",
            (segment["id"], job["cursor"], job["target"], limit),
        )
    )
    for row in rows:
        for key in "rca":
            job[key] = str(int(job[key]) + int(row[key]))
        job["methods"] = sorted(set(job["methods"]) | {row["method"]})
        job["cursor"] = row["id"]
    if len(rows) == limit:
        _save(db, "account_validation_job", json.dumps(job))
        return ()
    # Appended intervals are outside the frozen target. Preserve their committed
    # delta without restarting a large, active account on every observation.
    for key in "rca":
        job[key] = str(int(job[key]) + int(segment[key]) - int(job["captured"][key]))
    job["methods"] = sorted(
        set(job["methods"]) | (set(json.loads(segment["methods"])) - set(job["capturedMethods"]))
    )
    changed = (
        any(segment[key] != job[key] for key in "rca") or json.loads(segment["methods"]) != job["methods"]
    )
    if changed:
        db.execute(
            "UPDATE account_segment SET r=?,c=?,a=?,methods=? WHERE id=?",
            (*(job[key] for key in "rca"), json.dumps(job["methods"]), segment["id"]),
        )
        db.execute(
            "INSERT INTO diagnostic_log VALUES(NULL,?,NULL,'account_totals_rebuilt',?)",
            (format_instant(at), "账户累计已按保留区间重建: " + segment["vendor_id"][:100]),
        )
        work.touch("account")
        work.touch("wallet")
    db.execute("DELETE FROM runtime_state WHERE key='account_validation_job'")
    _save(db, "account_validation_segment", segment["id"])
    return (segment["vendor_id"],) if changed else ()
