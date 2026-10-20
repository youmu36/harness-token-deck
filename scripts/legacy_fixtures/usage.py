"""Synthetic legacy ledger, collection checkpoints and maintenance audit records."""

import hashlib
import sqlite3
from typing import Any

from scripts.legacy_fixtures.schema import ROOT, SOURCE_PATHS, at, compact, insert, key, pico


def event(
    harness: str,
    request: str,
    occurred_at: str,
    *,
    input_tokens: int | None = 1_000_000,
    output_tokens: int = 0,
    model_id: str = "legacy-model",
) -> dict[str, Any]:
    session = f"synthetic-legacy-{harness}"
    parts: list[str | int] = [session, request]
    if harness == "zcode":
        parts.append(0)
    elif harness == "dsh":
        parts = [session, int(request.split(":")[0]), int(request.split(":")[1])]
    incomplete = input_tokens is None
    return {
        "harness": harness,
        "dedupeKey": key(parts),
        "sessionId": session,
        "requestId": request,
        "modelId": model_id,
        "occurredAt": occurred_at,
        "timePrecision": "request_completion",
        "status": "completed" if harness == "zcode" else "reported",
        "sourceKind": "desktop" if harness == "codex" else None,
        "tokens": {
            "input": input_tokens,
            "cacheRead": None if incomplete else 0,
            "cacheWrite": None if incomplete else 0,
            "output": output_tokens,
            "reasoning": 0,
        },
        "issues": ["usage_incomplete", "cache_unknown", "cache_write_unknown"] if incomplete else [],
    }


def ledger_row(
    usage: dict[str, Any], usd: str, group: str | None, version: str | None, *, unpriced: bool = False
) -> dict[str, Any]:
    tokens = usage["tokens"]
    return {
        "harness": usage["harness"],
        "dedupe_key": usage["dedupeKey"],
        "occurred_at": usage["occurredAt"],
        "session_id": usage["sessionId"],
        "request_id": usage["requestId"],
        "model_id": usage["modelId"],
        "display_name": "Legacy Other" if group == "group-b" else "Legacy Model",
        "time_precision": usage["timePrecision"],
        "status": usage["status"],
        "input_tokens": tokens["input"],
        "cache_read_tokens": tokens["cacheRead"],
        "cache_write_tokens": tokens["cacheWrite"],
        "output_tokens": tokens["output"],
        "reasoning_tokens": tokens["reasoning"],
        "issues_json": compact(usage["issues"]),
        "price_group_id": group,
        "price_version_id": version,
        "pico_usd": str(pico(usd)),
        "incomplete": int(tokens["input"] is None),
        "unpriced": int(unpriced),
        "billed_at": at(7),
    }


def ledger(version: str) -> list[dict[str, Any]]:
    rows = [
        ledger_row(event("codex", "pre-price", "2026-08-31T00:00:00.000Z"), "0", None, None, unpriced=True),
        ledger_row(event("codex", "created", at(1)), "1", "group-a", "a-v1"),
        ledger_row(
            event("codex", "before-calibration", at(2, 12), input_tokens=2_000_000), "2", "group-a", "a-v1"
        ),
        ledger_row(event("codex", "at-calibration", at(3)), "3", "group-a", "a-v2"),
        ledger_row(event("codex", "recalculated", at(4, 1)), "4", "group-a", "a-v2"),
        ledger_row(
            event(
                "zcode",
                "before-created",
                at(1),
                input_tokens=0,
                output_tokens=1_000_000,
                model_id="legacy-other",
            ),
            "5",
            "group-b",
            "b-v1",
        ),
        ledger_row(event("zcode", "created", at(2), model_id="legacy-other"), "2", "group-b", "b-v1"),
        ledger_row(
            event(
                "zcode",
                "incomplete",
                at(4, 12),
                input_tokens=None,
                output_tokens=1_000_000,
                model_id="legacy-other",
            ),
            "5",
            "group-b",
            "b-v1",
        ),
        ledger_row(
            event("codex", "unlisted", at(5), model_id="unlisted-model"),
            "0",
            "group-a",
            "a-v2",
            unpriced=True,
        ),
    ]
    if version != "v022":
        rows.append(ledger_row(event("dsh", "1:1", at(5)), "4", "group-a", "a-v2"))
    if version == "v030":
        rows.append(
            ledger_row(
                event("codex", "tiny", at(6), input_tokens=1, output_tokens=1), "0.000006", "group-a", "a-v2"
            )
        )
    return rows


def write_collection(db: sqlite3.Connection, version: str) -> None:
    payload = (ROOT / "fixtures/codex/desktop.jsonl").read_bytes()
    checkpoints: dict[str, Any] = {
        "codex": {
            "offset": len(payload),
            "anchor": hashlib.sha256(payload[-512:]).hexdigest(),
            "context": {
                "desktop": True,
                "session_id": "synthetic-desktop",
                "model_id": "gpt-6-astra",
                "turn_id": "turn-1",
                "saw_request_usage": True,
            },
            "diagnostics": [],
        },
        "zcode": {"row_id": 4, "tail_id": "4", "pending": [4]},
        "dsh": {"context": {"session_id": "synthetic-dsh", "model_id": "deepseek-chat"}, "diagnostics": []},
    }
    for harness in ("codex", "zcode") if version == "v022" else SOURCE_PATHS:
        insert(
            db,
            "collection_sources",
            {
                "harness": harness,
                "path": SOURCE_PATHS[harness],
                "started_at": at(1),
                "last_scan_at": at(9),
                "status": "ready",
                "diagnostics_json": "[]",
            },
        )
        insert(
            db,
            "collection_checkpoints",
            {
                "harness": harness,
                "source_key": SOURCE_PATHS[harness],
                "state_json": compact(checkpoints[harness]),
            },
        )
        if version == "v030":
            usage = event(harness, "2:1" if harness == "dsh" else "pending", at(9, 1))
            insert(
                db,
                "usage_identities",
                {
                    "harness": harness,
                    "dedupe_key": usage["dedupeKey"],
                    "occurred_at": usage["occurredAt"],
                    "billed": 0,
                },
            )
            insert(
                db,
                "collection_inbox",
                {
                    "harness": harness,
                    "dedupe_key": usage["dedupeKey"],
                    "occurred_at": usage["occurredAt"],
                    "event_json": compact(usage),
                    "collected_at": at(9, 2),
                },
            )


def write_audit(db: sqlite3.Connection, rows: list[dict[str, Any]], version: str) -> None:
    insert(
        db,
        "runtime_state",
        {
            "id": 1,
            "collection_paused": int(version == "v030"),
            "last_retention_at": at(8) if version == "v030" else None,
            "last_vacuum_at": at(8, 1) if version == "v030" else None,
            "last_log_rotation_at": at(9),
        },
    )
    insert(
        db,
        "diagnostic_logs",
        {
            "id": 1,
            "occurred_at": at(9),
            "harness": "codex",
            "code": "scan_completed",
            "detail": "Synthetic legacy fixture",
        },
    )
    if version != "v030":
        return
    row = next(row for row in rows if row["request_id"] == "recalculated")
    insert(
        db,
        "usage_recalc_batches",
        {
            "id": "recalc-1",
            "harness": "codex",
            "start_at": at(4),
            "end_at": at(5),
            "group_id": "group-a",
            "version_id": "a-v2",
            "record_count": 1,
            "old_pico_usd": str(pico("3")),
            "new_pico_usd": row["pico_usd"],
            "unpriced_count": 0,
            "incomplete_count": 0,
            "committed_at": at(8),
            "price_fingerprint": hashlib.sha256(b"synthetic-legacy-recalc").hexdigest(),
        },
    )
    item: dict[str, Any] = {"batch_id": "recalc-1", "harness": "codex", "dedupe_key": row["dedupe_key"]}
    for column in (
        "price_group_id",
        "price_version_id",
        "display_name",
        "pico_usd",
        "incomplete",
        "unpriced",
        "billed_at",
    ):
        item[f"old_{column}"] = str(pico("3")) if column == "pico_usd" else row[column]
        item[f"new_{column}"] = at(8) if column == "billed_at" else row[column]
    db.execute(
        "UPDATE usage_ledger SET billed_at=? WHERE harness=? AND dedupe_key=?",
        (at(8), "codex", row["dedupe_key"]),
    )
    insert(db, "usage_recalc_items", item)
    insert(
        db,
        "retention_runs",
        {
            "id": 1,
            "cutoff": "2026-06-01T00:00:00.000Z",
            "deleted_ledger_rows": 1,
            "expired_inbox_rows": 0,
            "committed_at": at(8),
        },
    )
    insert(
        db,
        "usage_identities",
        {
            "harness": "codex",
            "dedupe_key": key(["synthetic-retained", "removed"]),
            "occurred_at": "2026-05-01T00:00:00.000Z",
            "billed": 1,
        },
    )
    insert(
        db,
        "diagnostic_logs",
        {
            "id": 2,
            "occurred_at": at(8),
            "harness": "codex",
            "code": "recalc_committed",
            "detail": "Synthetic recalc-1",
        },
    )
