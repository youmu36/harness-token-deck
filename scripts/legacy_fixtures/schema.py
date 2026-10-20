"""Old-format synthetic SQL and fixed primitive values."""

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_AT = "2026-09-10T00:00:00.000Z"
VERSIONS = ("v022", "v023", "v030")
PICO = 10**12
SOURCE_PATHS = {
    "codex": r"C:\HTU-Fixtures\codex\desktop.jsonl",
    "zcode": r"C:\HTU-Fixtures\zcode\usage.sqlite3",
    "dsh": r"C:\HTU-Fixtures\dsh\session.jsonl",
}

SCHEMA = """
CREATE TABLE configuration (
    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
    state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE collection_sources (
    harness TEXT PRIMARY KEY, path TEXT NOT NULL, started_at TEXT NOT NULL,
    last_scan_at TEXT, status TEXT NOT NULL, diagnostics_json TEXT NOT NULL
);
CREATE TABLE collection_checkpoints (
    harness TEXT NOT NULL, source_key TEXT NOT NULL, state_json TEXT NOT NULL,
    PRIMARY KEY(harness, source_key)
);
CREATE TABLE collection_inbox (
    harness TEXT NOT NULL, dedupe_key TEXT NOT NULL, occurred_at TEXT NOT NULL,
    event_json TEXT NOT NULL, collected_at TEXT NOT NULL, PRIMARY KEY(harness, dedupe_key)
);
CREATE TABLE usage_identities (
    harness TEXT NOT NULL, dedupe_key TEXT NOT NULL, occurred_at TEXT NOT NULL,
    billed INTEGER NOT NULL, PRIMARY KEY(harness, dedupe_key)
);
CREATE TABLE usage_ledger (
    harness TEXT NOT NULL, dedupe_key TEXT NOT NULL, occurred_at TEXT NOT NULL,
    session_id TEXT, request_id TEXT, model_id TEXT, display_name TEXT,
    time_precision TEXT NOT NULL, status TEXT NOT NULL, input_tokens INTEGER,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, output_tokens INTEGER,
    reasoning_tokens INTEGER, issues_json TEXT NOT NULL, price_group_id TEXT,
    price_version_id TEXT, pico_usd TEXT NOT NULL, incomplete INTEGER NOT NULL,
    unpriced INTEGER NOT NULL, billed_at TEXT NOT NULL, PRIMARY KEY(harness, dedupe_key)
);
CREATE TABLE usage_recalc_batches (
    id TEXT PRIMARY KEY, harness TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    group_id TEXT NOT NULL, version_id TEXT NOT NULL, record_count INTEGER NOT NULL,
    old_pico_usd TEXT NOT NULL, new_pico_usd TEXT NOT NULL, unpriced_count INTEGER NOT NULL,
    incomplete_count INTEGER NOT NULL, committed_at TEXT NOT NULL, price_fingerprint TEXT NOT NULL
);
CREATE TABLE usage_recalc_items (
    batch_id TEXT NOT NULL REFERENCES usage_recalc_batches(id), harness TEXT NOT NULL,
    dedupe_key TEXT NOT NULL, old_price_group_id TEXT, new_price_group_id TEXT,
    old_price_version_id TEXT, new_price_version_id TEXT, old_display_name TEXT, new_display_name TEXT,
    old_pico_usd TEXT NOT NULL, new_pico_usd TEXT NOT NULL,
    old_incomplete INTEGER NOT NULL, new_incomplete INTEGER NOT NULL,
    old_unpriced INTEGER NOT NULL, new_unpriced INTEGER NOT NULL,
    old_billed_at TEXT NOT NULL, new_billed_at TEXT NOT NULL,
    PRIMARY KEY(batch_id, harness, dedupe_key)
);
CREATE TABLE retention_runs (
    id INTEGER PRIMARY KEY, cutoff TEXT NOT NULL, deleted_ledger_rows INTEGER NOT NULL,
    expired_inbox_rows INTEGER NOT NULL, committed_at TEXT NOT NULL
);
CREATE TABLE runtime_state (
    id INTEGER PRIMARY KEY CHECK(id=1), collection_paused INTEGER NOT NULL,
    last_retention_at TEXT, last_vacuum_at TEXT, last_log_rotation_at TEXT
);
CREATE TABLE diagnostic_logs (
    id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, harness TEXT, code TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE price_groups (id TEXT PRIMARY KEY, name TEXT NOT NULL, disabled INTEGER NOT NULL);
CREATE TABLE price_versions (
    id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES price_groups(id), number INTEGER NOT NULL,
    label TEXT NOT NULL, effective_at TEXT NOT NULL, UNIQUE(group_id, number)
);
CREATE TABLE price_model_rates (
    version_id TEXT NOT NULL REFERENCES price_versions(id), model_id TEXT NOT NULL,
    display_name TEXT NOT NULL, input TEXT, cache TEXT, output TEXT, billing_json TEXT,
    PRIMARY KEY(version_id, model_id)
);
CREATE TABLE price_selections (
    id INTEGER PRIMARY KEY, harness TEXT NOT NULL, group_id TEXT NOT NULL REFERENCES price_groups(id),
    effective_at TEXT NOT NULL
);
"""

EXTENSION_SCHEMA = """
CREATE TABLE schema_metadata (
    id INTEGER PRIMARY KEY CHECK(id=1), storage_generation INTEGER NOT NULL, current_schema TEXT NOT NULL
);
CREATE TABLE configuration_extensions (
    id INTEGER PRIMARY KEY CHECK(id=1), configuration_revision INTEGER NOT NULL, state_json TEXT NOT NULL
);
CREATE TABLE dsh_price_selections (
    id INTEGER PRIMARY KEY, harness TEXT NOT NULL, group_id TEXT NOT NULL REFERENCES price_groups(id),
    effective_at TEXT NOT NULL
);
"""


def at(day: int, hour: int = 0) -> str:
    return f"2026-09-{day:02d}T{hour:02d}:00:00.000Z"


def compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def pico(usd: str) -> int:
    value = Decimal(usd) * PICO
    assert value == value.to_integral_value(), "Fixture amount must be exact to pico"
    return int(value)


def key(parts: list[str | int]) -> str:
    return hashlib.sha256(compact(parts).encode("utf-8")).hexdigest()


def insert(db: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()))
