"""Read coherent repricing inputs and append audit and original-wallet deltas."""

import sqlite3
from typing import cast
from uuid import uuid4

from htd.adapters.sqlite.command_store import connection, read_snapshot
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.manual_maintenance_store import SqliteManualMaintenanceStore
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.recalc_repo import RecalcBatch, RecalcChange, RecalcRepository
from htd.adapters.sqlite.usage_codec import loads
from htd.adapters.sqlite.wallet_flows import locked_consumption
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.wallet_store import decode_entry
from htd.application.ports import UnitOfWork
from htd.application.ports.maintenance import RecalcRow, RecalcSnapshot, RecalcUpdate
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.pricing.catalog import Catalog
from htd.domain.pricing.history import HISTORY_GROUP_ID
from htd.domain.usage.model import Harness, SourceKind, Tokens, UsageEvent, UsageStatus
from htd.domain.wallet.model import WalletEntry


class SqliteRecalcStore(SqliteManualMaintenanceStore):
    def recalc_snapshot(
        self, harness: Harness, start: Instant, end: Instant, work: UnitOfWork | None = None
    ) -> RecalcSnapshot:
        if work is not None:
            return self.snapshot_rows(connection(work), harness, start, end)
        with read_snapshot(self.readers) as db:
            return self.snapshot_rows(db, harness, start, end)

    @staticmethod
    def snapshot_rows(
        db: sqlite3.Connection,
        harness: Harness,
        start: Instant,
        end: Instant,
        *,
        unpriced: bool = False,
        catalog: Catalog | None = None,
        keys: tuple[str, ...] | None = None,
    ) -> RecalcSnapshot:
        repo, wallet = RecalcRepository(db), WalletRepository(db)
        rows: list[RecalcRow] = []
        for row in db.execute(
            "SELECT * FROM ledger WHERE harness=? AND occurred_at>=? AND occurred_at<? "
            + ("AND unpriced=1 " if unpriced else "")
            + ("AND dedupe_key IN (" + ",".join("?" for _ in keys) + ") " if keys else "")
            + (
                "ORDER BY COALESCE((SELECT ignored FROM unpriced_resolution r "
                "WHERE r.harness=ledger.harness AND "
                "r.dedupe_key=ledger.dedupe_key),0),dedupe_key"
                if unpriced
                else "ORDER BY dedupe_key"
            )
            + (" LIMIT 2500" if unpriced and keys is None else ""),
            (
                harness.value,
                format_instant(start),
                format_instant(end),
                *(key_bytes(key) for key in keys or ()),
            ),
        ):
            annotation = db.execute(
                "SELECT cache_read FROM usage_annotation WHERE harness=? AND dedupe_key=?",
                (harness.value, key_bytes(row["dedupe_key"])),
            ).fetchone()
            cache = annotation[0] if annotation and row["cache_read"] is None else row["cache_read"]
            input_tokens = row["input"]
            if row["cache_read"] is None and cache is not None and input_tokens is not None:
                input_tokens += cache
            event = UsageEvent(
                harness,
                row["dedupe_key"],
                row["session_id"],
                row["request_id"],
                row["model_id"],
                row["occurred_at"],
                Tokens(input_tokens, cache, row["cache_write"], row["output"], row["reasoning"]),
                cast(UsageStatus, row["status"]),
                tuple(cast(list[str], loads(row["issues_json"]))),
                cast(SourceKind | None, row["source_kind"]),
            )
            original = db.execute(
                "SELECT * FROM wallet_entry WHERE kind='consumption' AND usage_harness=? AND usage_key=?",
                (harness.value, key_bytes(event.dedupe_key)),
            ).fetchone()
            rows.append(
                RecalcRow(
                    event,
                    repo.snapshot(harness, event.dedupe_key),
                    decode_entry(original) if original else None,
                    local_override=bool(
                        original
                        and locked_consumption(
                            db, original["vendor_id"], original["seq"], original["occurred_at"]
                        )
                    ),
                )
            )
        vendor_ids = {r.consumption.vendor_id for r in rows if r.consumption is not None}
        return RecalcSnapshot(
            catalog if catalog is not None else PricingRepository(db).load(),
            tuple(rows),
            {v.id: v.name for v in wallet.list_vendors(include_archived=True)},
            {
                v: (
                    ()
                    if db.execute(
                        "SELECT 1 FROM account_segment WHERE vendor_id=? AND kind='remote' "
                        "AND ended_at IS NULL",
                        (v,),
                    ).fetchone()
                    else wallet.calibrations(v)
                )
                for v in vendor_ids
            },
            remote_vendors=frozenset(
                row[0]
                for row in db.execute(
                    "SELECT vendor_id FROM account_segment WHERE kind='remote' AND ended_at IS NULL"
                )
            ),
            links=wallet.links(),
            cost_revision=db.execute("SELECT value FROM calibration_revision WHERE id=1").fetchone()[0],
            unavailable=tuple(
                (str(row[0]), bool(row[1]))
                for row in db.execute(
                    "SELECT i.dedupe_key,EXISTS(SELECT 1 FROM inbox b WHERE b.harness=i.harness "
                    "AND b.dedupe_key=i.dedupe_key) FROM identity i WHERE i.harness=? "
                    "AND i.occurred_at>=? AND i.occurred_at<? AND NOT EXISTS(SELECT 1 FROM ledger l "
                    "WHERE l.harness=i.harness AND l.dedupe_key=i.dedupe_key) ORDER BY i.dedupe_key",
                    (harness.value, format_instant(start), format_instant(end)),
                )
            )
            if not unpriced
            else (),
        )

    @staticmethod
    def commit_recalc(
        work: UnitOfWork,
        parameters: dict[str, object],
        updates: tuple[RecalcUpdate, ...],
        operation_id: str,
        *,
        at: Instant,
    ) -> tuple[str, tuple[str, ...]]:
        db = connection(work)
        batch = RecalcBatch(
            str(uuid4()),
            Harness(parameters["harness"]),
            parse_instant(cast(str, parameters["start"])),
            parse_instant(cast(str, parameters["end"])),
            cast(str, parameters["groupId"]),
            cast(str, parameters["versionId"]),
            cast(str, parameters["fingerprint"]),
            operation_id,
        )
        indexed = {u.row.event.dedupe_key: u for u in updates}
        changed: set[str] = set()
        wallet = WalletRepository(db)

        def on_updated(work: UnitOfWork, change: RecalcChange) -> None:
            if change.new["price_group_id"] == HISTORY_GROUP_ID:
                return
            original = indexed[change.dedupe_key].row.consumption
            if original is None:
                return
            delta = int(cast(str, change.new["pico"])) - int(cast(str, change.old["pico"]))
            quality_changed = any(change.new[k] != change.old[k] for k in ("unpriced", "incomplete"))
            if delta == 0 and not quality_changed:
                return
            entry = WalletEntry(
                str(uuid4()),
                original.vendor_id,
                "consumption_delta",
                delta,
                original.occurred_at,
                wallet.next_seq(),
                usage_ref=original.usage_ref,
                related_entry_id=original.id,
                recalc_batch_id=batch.id,
                unpriced=bool(change.new["unpriced"]),
                incomplete=bool(change.new["incomplete"]),
                note="重算调整",
            )
            wallet.append_with_offsets(work, entry, at=at)
            changed.add(original.vendor_id)

        RecalcRepository(db).commit(
            work,
            batch,
            tuple(
                RecalcChange(u.row.event.harness, u.row.event.dedupe_key, u.row.old, u.new) for u in updates
            ),
            at=at,
            on_updated=on_updated,
            record_receipt=False,
        )
        from htd.adapters.sqlite.wallet_recalc_journal import stage
        from htd.adapters.sqlite.wallet_recovery import append_missing

        for update in updates:
            if update.row.consumption is None:
                entry = append_missing(db, work, update.row.event, update.new, batch.id, at)
                if entry is not None:
                    changed.add(entry.vendor_id)
            from htd.adapters.sqlite.session_core import remember_ledger

            raw = db.execute(
                "SELECT * FROM ledger WHERE harness=? AND dedupe_key=?",
                (update.row.event.harness.value, key_bytes(update.row.event.dedupe_key)),
            ).fetchone()
            if raw is not None:
                remember_ledger(db, raw)
        changed.update(stage(db, work, updates, batch.id, at))
        return batch.id, tuple(sorted(changed))
