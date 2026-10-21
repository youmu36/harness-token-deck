"""Complete an explicitly repriced request using its historical supplier link."""

import json
import sqlite3
from typing import Any, cast

from htd.adapters.sqlite.command_store import connection, read_snapshot
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.recalc_codec import audit
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import MAINTENANCE, WriteExecutor
from htd.application.ports import BilledUsage, UnitOfWork, WalletChanged
from htd.domain.billing.estimate import CostEstimate, RuleApplied
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.pricing.history import HISTORY_GROUP_ID
from htd.domain.usage.model import Harness, SourceKind, Tokens, UsageEvent, UsageStatus
from htd.domain.wallet.model import WalletEntry

KEY = "wallet_missing_reprice_043"


def append_missing(
    db: sqlite3.Connection,
    work: UnitOfWork,
    event: UsageEvent,
    values: dict[str, Any],
    batch: str,
    at: Instant,
) -> WalletEntry | None:
    if values["price_group_id"] in (None, HISTORY_GROUP_ID):
        return None
    if db.execute(
        "SELECT 1 FROM wallet_entry WHERE usage_harness=? AND usage_key=? AND kind='consumption'",
        (event.harness.value, key_bytes(event.dedupe_key)),
    ).fetchone():
        return None
    return WalletRepository(db).append_consumption(
        work,
        BilledUsage(
            event,
            CostEstimate(
                int(values["pico"]),
                bool(values["incomplete"]),
                bool(values["unpriced"]),
                cast(RuleApplied, values["rule_applied"]),
            ),
            values["price_group_id"],
            values["price_version_id"],
            values["price_revision_id"],
            values["display_name"],
        ),
        at=at,
        recalc_batch_id=batch,
        ownership_at=parse_instant(values["billed_at"]),
    )


def install(db: sqlite3.Connection) -> None:
    if db.execute("SELECT 1 FROM runtime_state WHERE key=?", (KEY,)).fetchone():
        return
    end = db.execute("SELECT COALESCE(MAX(rowid),0) FROM recalc_item").fetchone()[0]
    db.execute("INSERT INTO runtime_state VALUES(?,?)", (KEY, json.dumps({"cursor": 0, "end": end})))


def repair(readers: ReadPool, writer: WriteExecutor, at: Instant) -> int:
    with read_snapshot(readers) as db:
        saved = db.execute("SELECT value FROM runtime_state WHERE key=?", (KEY,)).fetchone()
        if saved is None:
            return 0
        state = json.loads(saved[0])
        if state["cursor"] >= state["end"]:
            return 0

    def run(work: UnitOfWork) -> int:
        db = connection(work)
        saved = db.execute("SELECT value FROM runtime_state WHERE key=?", (KEY,)).fetchone()
        if saved is None:
            return 0
        state = json.loads(saved[0])
        if state["cursor"] >= state["end"]:
            return 0
        items = db.execute(
            "SELECT rowid audit_seq,* FROM recalc_item WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 500",
            (state["cursor"], state["end"]),
        ).fetchall()
        changed: set[str] = set()
        count = 0
        for item in items:
            _, new = audit(item)
            row = db.execute(
                "SELECT * FROM ledger WHERE harness=? AND dedupe_key=?",
                (item["harness"], key_bytes(item["dedupe_key"])),
            ).fetchone()
            if row is None or any(row[field] != value for field, value in new.items()):
                continue
            event = UsageEvent(
                Harness(row["harness"]),
                row["dedupe_key"],
                row["session_id"],
                row["request_id"],
                row["model_id"],
                row["occurred_at"],
                Tokens(row["input"], row["cache_read"], row["cache_write"], row["output"], row["reasoning"]),
                cast(UsageStatus, row["status"]),
                tuple(json.loads(row["issues_json"])),
                cast(SourceKind | None, row["source_kind"]),
            )
            entry = append_missing(db, work, event, dict(row), item["batch_id"], at)
            if entry is not None:
                count += 1
                changed.add(entry.vendor_id)
        state["cursor"] = items[-1]["audit_seq"] if items else state["end"]
        db.execute("UPDATE runtime_state SET value=? WHERE key=?", (json.dumps(state), KEY))
        if changed:
            db.execute(
                "INSERT INTO diagnostic_log VALUES(NULL,?,NULL,'wallet_reprice_repaired',?)",
                (format_instant(at), f"补记 {count} 条已有补价审计的漏记消费"),
            )
            work.emit(WalletChanged(tuple(sorted(changed)), work.revision("wallet")))
        return count

    return writer.execute(run, priority=MAINTENANCE)
