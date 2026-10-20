"""Exact, disposable hourly counters; minute rows remain the source of truth."""

import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from htu.adapters.sqlite.counter_codec import pack


def hours(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    flags = {"incomplete", "unpriced", "cache_loss", "cache_bust", "large_miss"}
    metadata = {"harness", "bucket", "vendor_id", "model_key", "model_id", "display_name", "latest_bucket"}
    for row in rows:
        bucket = row["bucket"][:13] + ":00:00.000Z"
        key = row["harness"], bucket, row["vendor_id"], row["model_key"]
        old = groups.get(key)
        if old is None:
            groups[key] = {**row, "bucket": bucket, "latest_bucket": row["bucket"]}
            continue
        for column, value in row.items():
            if column in metadata:
                continue
            old[column] = (
                str(int(old[column]) + int(value))
                if column == "pico"
                else (int(bool(old[column] or value)) if column in flags else old[column] + value)
            )
        if row["bucket"] >= old["latest_bucket"]:
            old.update(
                model_id=row["model_id"], display_name=row["display_name"], latest_bucket=row["bucket"]
            )
    return list(groups.values())


def refresh_hours(db: sqlite3.Connection, *, limit: int = 24) -> int:
    dirty = db.execute("SELECT * FROM usage_hour_dirty ORDER BY bucket,harness LIMIT ?", (limit,)).fetchall()
    for item in dirty:
        rows = (
            dict(row)
            for row in db.execute(
                "SELECT * FROM usage_rollup WHERE harness=? AND bucket>=? AND bucket<?",
                (item["harness"], item["bucket"], item["bucket"][:13] + ":59:59.999Z"),
            )
        )
        counters = hours(rows)
        db.execute("DELETE FROM usage_hour WHERE harness=? AND bucket=?", tuple(item))
        db.execute("DELETE FROM usage_hour_model WHERE harness=? AND bucket=?", tuple(item))
        if counters:
            db.execute("INSERT INTO usage_hour VALUES(?,?,?)", (*item, ""))
            models: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in counters:
                models[row["model_key"]].append(row)
            for model, partition in models.items():
                for begin in range(0, len(partition), 500):
                    db.execute(
                        "INSERT INTO usage_hour_model VALUES(?,?,?,?,?)",
                        (*item, model, begin // 500, pack(partition[begin : begin + 500])),
                    )
        db.execute("DELETE FROM usage_hour_dirty WHERE harness=? AND bucket=?", tuple(item))
    return len(dirty)
