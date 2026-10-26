"""An incrementally observed model directory, independent of balance updates."""

import sqlite3
from typing import Any


def install(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS analysis_model_directory(model_key TEXT PRIMARY KEY,"
        "name TEXT NOT NULL,last_at TEXT NOT NULL) STRICT, WITHOUT ROWID"
    )
    if not db.execute("SELECT 1 FROM runtime_state WHERE key='analysis_model_directory_v1'").fetchone():
        # Upgrade reads archive metadata, never decompresses history. Iteration is
        # bounded by the directory rather than a Python list of all requests.
        db.execute(
            "INSERT INTO analysis_model_directory SELECT model_key,COALESCE(model_id,'未知模型'),stamp "
            "FROM (SELECT model_key,model_id,occurred_at stamp FROM ledger UNION ALL "
            "SELECT model_key,model_id,bucket FROM usage_rollup UNION ALL "
            "SELECT model_key,model_id,start_at FROM usage_daily UNION ALL "
            "SELECT json_extract(j.value,'$.model_key'),json_extract(j.value,'$.model_id'),"
            "json_extract(j.value,'$.bucket') FROM usage_archive a,json_each(a.models_json) j) "
            "WHERE model_key IS NOT NULL ORDER BY stamp "
            "ON CONFLICT(model_key) DO UPDATE SET name=excluded.name,last_at=excluded.last_at "
            "WHERE excluded.last_at>=analysis_model_directory.last_at"
        )
        db.execute("INSERT INTO runtime_state VALUES('analysis_model_directory_v1','1')")
    for table, stamp in (("ledger", "occurred_at"), ("usage_rollup", "bucket"), ("usage_daily", "start_at")):
        for event in ("INSERT", "UPDATE"):
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS analysis_models_{table}_{event.lower()} "
                f"AFTER {event} ON {table} BEGIN INSERT INTO analysis_model_directory "
                "VALUES(new.model_key,COALESCE(new.model_id,'未知模型'),"
                f"new.{stamp}) ON CONFLICT(model_key) DO UPDATE "
                "SET name=excluded.name,last_at=excluded.last_at "
                "WHERE excluded.last_at>=analysis_model_directory.last_at; END"
            )


def options(db: sqlite3.Connection, query: dict[str, Any] | None = None) -> dict[str, object]:
    query = query or {}
    search, offset, limit = (
        query.get("modelSearch", ""),
        query.get("modelOffset", 0),
        query.get("modelLimit", 100),
    )
    # instr is literal search: '%' and '_' in model names are not SQL wildcards.
    where = " WHERE instr(lower(name),lower(?))>0 OR instr(lower(model_key),lower(?))>0"
    count = db.execute("SELECT COUNT(*) FROM analysis_model_directory" + where, (search, search)).fetchone()[
        0
    ]
    catalog = query.get("includeCatalog", True)
    selected: list[str] = list(dict.fromkeys(query.get("modelKeys", [])))
    group_vendors: dict[str, list[str]] = {}
    if catalog:
        for row in db.execute(
            "SELECT DISTINCT group_id,vendor_id FROM vendor_link WHERE vendor_id IS NOT NULL"
        ):
            group_vendors.setdefault(row[0], []).append(row[1])
    return {
        "vendors": [
            {"id": r["id"], "name": r["name"] + (" (归档)" if r["status"] == "archived" else "")}
            for r in (db.execute("SELECT id,name,status FROM vendor ORDER BY name,id") if catalog else ())
        ],
        "groups": [
            {"id": r["id"], "name": r["name"]}
            for r in (db.execute("SELECT id,name FROM price_group ORDER BY seq") if catalog else ())
        ],
        "versions": [
            {"id": r["id"], "name": r["label"] or f"版本 {r['number']}", "parentId": r["group_id"]}
            for r in (
                db.execute(
                    "SELECT v.*,COALESCE(l.label,'') label FROM price_version v "
                    "LEFT JOIN price_version_label l ON l.version_id=v.id ORDER BY v.seq"
                )
                if catalog
                else ()
            )
        ],
        "models": [
            {"id": r[0], "name": r[1]}
            for r in db.execute(
                "SELECT model_key,name FROM analysis_model_directory"
                + where
                + " ORDER BY model_key LIMIT ? OFFSET ?",
                (search, search, limit, offset),
            )
        ],
        "totalModels": count,
        "modelNextOffset": offset + limit if offset + limit < count else None,
        "groupVendors": group_vendors,
        "selectedModels": [
            {"id": row[0], "name": row[1]}
            for row in db.execute(
                "SELECT model_key,name FROM analysis_model_directory WHERE model_key IN ("
                + ",".join("?" for _ in selected)
                + ") ORDER BY model_key",
                selected,
            )
        ]
        if selected
        else [],
    }
