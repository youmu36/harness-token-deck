"""Additive, permanent target ownership, explicit allocation and append-only audit evidence."""

import sqlite3


def install(db: sqlite3.Connection) -> None:
    for sql in (
        "CREATE TABLE IF NOT EXISTS calibration_revision(id INTEGER PRIMARY KEY CHECK(id=1),"
        "value INTEGER NOT NULL) STRICT",
        "INSERT OR IGNORE INTO calibration_revision VALUES(1,0)",
        "CREATE TABLE IF NOT EXISTS calibration_target("
        "id TEXT PRIMARY KEY,vendor_id TEXT NOT NULL REFERENCES vendor(id),state TEXT NOT NULL,"
        "target_pico TEXT NOT NULL,estimate_pico TEXT NOT NULL,delta_pico TEXT NOT NULL,"
        "scope_json TEXT NOT NULL,evidence_json TEXT NOT NULL,member_digest TEXT NOT NULL,"
        "member_count INTEGER NOT NULL,created_at TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE,"
        "revision INTEGER NOT NULL DEFAULT 1) STRICT",
        "CREATE INDEX IF NOT EXISTS calibration_target_vendor "
        "ON calibration_target(vendor_id,state,created_at DESC,id)",
        "CREATE TABLE IF NOT EXISTS calibration_bucket("
        "id TEXT PRIMARY KEY,target_id TEXT NOT NULL REFERENCES calibration_target(id),"
        "day TEXT NOT NULL,target_pico TEXT NOT NULL,estimate_pico TEXT NOT NULL,delta_pico TEXT NOT NULL,"
        "scope_json TEXT NOT NULL,model_allocated INTEGER NOT NULL,"
        "coverage_start TEXT NOT NULL,coverage_end TEXT NOT NULL,member_count INTEGER NOT NULL,"
        "revision INTEGER NOT NULL DEFAULT 1) STRICT",
        "CREATE INDEX IF NOT EXISTS calibration_bucket_target ON calibration_bucket(target_id,day)",
        "CREATE TABLE IF NOT EXISTS calibration_member("
        "target_id TEXT NOT NULL REFERENCES calibration_target(id),harness TEXT NOT NULL,"
        "dedupe_key BLOB NOT NULL,bucket_id TEXT NOT NULL REFERENCES calibration_bucket(id),"
        "basis_revision INTEGER NOT NULL,PRIMARY KEY(target_id,harness,dedupe_key)) STRICT, WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS calibration_member_bucket ON calibration_member(bucket_id)",
        "CREATE INDEX IF NOT EXISTS calibration_member_request ON "
        "calibration_member(harness,dedupe_key,target_id)",
        "CREATE TABLE IF NOT EXISTS calibration_scope_count(bucket_id TEXT NOT NULL "
        "REFERENCES calibration_bucket(id),dimension TEXT NOT NULL,value TEXT NOT NULL,"
        "count INTEGER NOT NULL,PRIMARY KEY(bucket_id,dimension,value)) STRICT, WITHOUT ROWID",
        "CREATE TABLE IF NOT EXISTS calibration_owner(harness TEXT NOT NULL,dedupe_key BLOB NOT NULL,"
        "target_id TEXT NOT NULL REFERENCES calibration_target(id),"
        "PRIMARY KEY(harness,dedupe_key)) STRICT, WITHOUT ROWID",
        "CREATE TABLE IF NOT EXISTS calibration_audit("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT,target_id TEXT NOT NULL REFERENCES calibration_target(id),"
        "kind TEXT NOT NULL,occurred_at TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE,"
        "before_estimate TEXT NOT NULL,after_estimate TEXT NOT NULL,"
        "before_delta TEXT NOT NULL,after_delta TEXT NOT NULL,note TEXT NOT NULL) STRICT",
        "CREATE TABLE IF NOT EXISTS calibration_legacy_replacement("
        "flow_id TEXT PRIMARY KEY,target_id TEXT NOT NULL REFERENCES calibration_target(id),"
        "occurred_at TEXT NOT NULL) STRICT",
        "CREATE INDEX IF NOT EXISTS calibration_legacy_target ON calibration_legacy_replacement(target_id)",
        "CREATE TABLE IF NOT EXISTS calibration_legacy_deferred(flow_id TEXT PRIMARY "
        "KEY,usage_revision INTEGER NOT NULL) STRICT",
    ):
        db.execute(sql)
