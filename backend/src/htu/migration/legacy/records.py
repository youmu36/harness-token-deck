"""Map legacy collection and maintenance rows into the new storage representation."""

from dataclasses import dataclass
from typing import Any, cast

from htu.adapters.sqlite.pico_codec import decode_pico
from htu.adapters.sqlite.usage_codec import decode_event, dumps, loads, object_json
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.pricing.catalog import Catalog
from htu.domain.usage.model_key import model_key
from htu.migration.legacy.checkpoints import convert_checkpoint
from htu.migration.legacy.reader import LegacySnapshot
from htu.migration.wallet_convert import stable_id


@dataclass(frozen=True, slots=True)
class MappedRecords:
    tables: dict[str, tuple[dict[str, Any], ...]]
    dropped_checkpoints: tuple[str, ...]


def _time(value: str) -> str:
    return format_instant(parse_instant(value))


def _revision(catalog: Catalog, version: str | None, occurred: str) -> str | None:
    return catalog.revision_at(version, parse_instant(occurred)).id if version else None


def map_records(source: LegacySnapshot, catalog: Catalog, *, at: Instant) -> MappedRecords:
    stamp = format_instant(at)
    tables: dict[str, tuple[dict[str, Any], ...]] = {}
    tables["source"] = tuple(
        {
            **{
                key: row[key] for key in ("harness", "path", "started_at", "last_scan_at", "diagnostics_json")
            },
            "status": "pending",
            "unmetered_json": None,
        }
        for row in source.rows("collection_sources")
    )
    checkpoints: list[dict[str, Any]] = []
    dropped: list[str] = []
    for row in source.rows("collection_checkpoints"):
        try:
            payload = convert_checkpoint(row)
        except (KeyError, TypeError, ValueError, DomainError) as error:
            dropped.append(f"{row['harness']}:{row['source_key']}: {error}")
            continue
        checkpoints.append(
            {
                "harness": row["harness"],
                "source_key": row["source_key"],
                "state_json": payload,
                "updated_at": stamp,
            }
        )
    tables["checkpoint"] = tuple(checkpoints)
    tables["identity"] = tuple(
        {
            **{key: row[key] for key in ("harness", "dedupe_key", "billed")},
            "occurred_at": _time(row["occurred_at"]),
            "first_seen_at": stamp,
            "origin": "legacy",
        }
        for row in source.rows("usage_identities")
    )
    inbox: list[dict[str, Any]] = []
    for row in source.rows("collection_inbox"):
        data = object_json(row["event_json"])
        tokens = object_json(dumps(data["tokens"]))
        mapped = {
            "schema": 1,
            **{
                name: data[old]
                for name, old in (
                    ("harness", "harness"),
                    ("dedupe_key", "dedupeKey"),
                    ("session_id", "sessionId"),
                    ("request_id", "requestId"),
                    ("model_id", "modelId"),
                    ("occurred_at", "occurredAt"),
                    ("status", "status"),
                    ("issues", "issues"),
                    ("time_precision", "timePrecision"),
                )
            },
            "source_kind": data.get("sourceKind"),
            "tokens": {
                name: tokens[old]
                for name, old in (
                    ("input", "input"),
                    ("cache_read", "cacheRead"),
                    ("cache_write", "cacheWrite"),
                    ("output", "output"),
                    ("reasoning", "reasoning"),
                )
            },
        }
        payload = dumps(mapped)
        event = decode_event(payload)
        if (event.harness, event.dedupe_key, event.occurred_at) != (
            row["harness"],
            row["dedupe_key"],
            _time(row["occurred_at"]),
        ):
            raise DomainError("LEGACY_DATA_INVALID", "inbox 事件与索引不一致", "collection_inbox")
        inbox.append(
            {
                **{key: row[key] for key in ("harness", "dedupe_key")},
                "occurred_at": _time(row["occurred_at"]),
                "collected_at": _time(row["collected_at"]),
                "event_json": payload,
            }
        )
    tables["inbox"] = tuple(inbox)
    ledger: list[dict[str, Any]] = []
    for row in source.rows("usage_ledger"):
        if row["time_precision"] != "request_completion":
            raise DomainError("LEGACY_DATA_INVALID", "账本时间精度不受支持", "usage_ledger.time_precision")
        if decode_pico(row["pico_usd"]) < 0:
            raise DomainError("LEGACY_DATA_INVALID", "账本金额不能为负", "usage_ledger.pico_usd")
        issues = loads(row["issues_json"])
        if type(issues) is not list or any(type(issue) is not str for issue in cast(list[object], issues)):
            raise DomainError(
                "LEGACY_DATA_INVALID", "账本 issues 必须是字符串数组", "usage_ledger.issues_json"
            )
        item = {
            name: row[name]
            for name in (
                "harness",
                "dedupe_key",
                "session_id",
                "request_id",
                "model_id",
                "display_name",
                "status",
                "issues_json",
                "price_group_id",
                "price_version_id",
                "incomplete",
                "unpriced",
            )
        }
        item.update(
            occurred_at=_time(row["occurred_at"]),
            billed_at=_time(row["billed_at"]),
            source_kind=row.get("source_kind"),
            model_key=model_key(row["model_id"]) if row["model_id"] else "",
            price_revision_id=_revision(catalog, row["price_version_id"], row["occurred_at"]),
            rule_applied="legacy",
            pico=row["pico_usd"],
        )
        for name in ("input", "cache_read", "cache_write", "output", "reasoning"):
            item[name] = row[name + "_tokens"]
        ledger.append(item)
    tables["ledger"] = tuple(ledger)
    tables["recalc_batch"] = tuple(
        {
            **{
                key: row[key]
                for key in (
                    "id",
                    "harness",
                    "start_at",
                    "end_at",
                    "group_id",
                    "version_id",
                    "record_count",
                    "unpriced_count",
                    "incomplete_count",
                    "committed_at",
                )
            },
            "old_pico": row["old_pico_usd"],
            "new_pico": row["new_pico_usd"],
            "fingerprint": row["price_fingerprint"],
            "operation_id": stable_id("usage_recalc_batches", row["id"]),
        }
        for row in source.rows("usage_recalc_batches")
    )
    for batch in tables["recalc_batch"]:
        decode_pico(batch["old_pico"])
        decode_pico(batch["new_pico"])
    items: list[dict[str, Any]] = []
    by_key = {(row["harness"], row["dedupe_key"]): row for row in source.rows("usage_ledger")}
    for row in source.rows("usage_recalc_items"):
        item = {key: row[key] for key in ("batch_id", "harness", "dedupe_key")}
        occurred = by_key.get((row["harness"], row["dedupe_key"]), {}).get("occurred_at")
        for prefix in ("old", "new"):
            state = {
                key.removeprefix(prefix + "_"): value
                for key, value in row.items()
                if key.startswith(prefix + "_")
            }
            state["pico"] = state.pop("pico_usd")
            decode_pico(state["pico"])
            state["rule_applied"] = "legacy"
            state["price_revision_id"] = (
                _revision(catalog, state["price_version_id"], occurred) if occurred else None
            )
            item[prefix + "_json"] = dumps(state)
        items.append(item)
    tables["recalc_item"] = tuple(items)
    retention = source.state("configuration")["settings"]["retention"]
    tables["retention_run"] = tuple(
        {
            "id": stable_id("retention_runs", str(row.get("id", index))),
            "retention": row.get("retention", retention),
            "cutoff_at": row.get("cutoff_at", row.get("cutoff")),
            "deleted_ledger": row.get("deleted_ledger", row.get("deleted_ledger_rows")),
            "expired_inbox": row.get("expired_inbox_rows", 0),
            "committed_at": row["committed_at"],
        }
        for index, row in enumerate(source.rows("retention_runs"))
    )
    tables["diagnostic_log"] = tuple(
        {
            "id": row.get("id", index + 1),
            **{key: row[key] for key in ("occurred_at", "harness", "code", "detail")},
        }
        for index, row in enumerate(source.rows("diagnostic_logs"))
    )
    runtime: list[dict[str, Any]] = []
    for row in source.rows("runtime_state"):
        for key, value in row.items():
            if key == "id" or value is None:
                continue
            runtime.append(
                {
                    "key": key,
                    "value": ("true" if value else "false") if key == "collection_paused" else str(value),
                }
            )
    tables["runtime_state"] = tuple(runtime)
    return MappedRecords(tables, tuple(dropped))
