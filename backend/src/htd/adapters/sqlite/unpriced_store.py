"""Independent pending-price queries; ordinary recalculation keeps its 72-hour window."""

import sqlite3
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any, cast

from htd.adapters.sqlite.command_store import connection, read_snapshot
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.pricing_codec import decode_rule
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.recalc_command_store import SqliteRecalcStore
from htd.adapters.sqlite.usage_archive import ArchiveRow, archived_rows
from htd.adapters.sqlite.usage_codec import dumps, object_json
from htd.application.ports import UnitOfWork
from htd.application.ports.maintenance import RecalcRow, RecalcSnapshot, UnpricedDecision
from htd.domain.common.time import Instant, format_instant
from htd.domain.pricing.catalog import Catalog
from htd.domain.pricing.model import ModelRate
from htd.domain.usage.model import Harness


class SqliteUnpricedStore(SqliteRecalcStore):
    def save_unpriced_catalog(self, work: UnitOfWork, old: Catalog, new: Catalog) -> None:
        from htd.adapters.sqlite.writer import UnitOfWork as SqlWork

        if not isinstance(work, SqlWork):
            raise TypeError("Requires SQLite UnitOfWork")
        PricingRepository(connection(work)).save(work, old, new)

    def save_unpriced(
        self, work: UnitOfWork, rows: tuple[RecalcRow, ...], decision: UnpricedDecision, *, at: Instant
    ) -> None:
        db = connection(work)
        for row in rows:
            db.execute(
                "INSERT INTO unpriced_resolution VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(harness,dedupe_key) DO UPDATE SET ignored=excluded.ignored,"
                "group_id=excluded.group_id,version_id=excluded.version_id,model_json=excluded.model_json,"
                "multiplier=excluded.multiplier,updated_at=excluded.updated_at",
                (
                    row.event.harness.value,
                    key_bytes(row.event.dedupe_key),
                    int(decision.ignored),
                    decision.group_id,
                    decision.version_id,
                    dumps(asdict(decision.model)) if decision.model else None,
                    decision.multiplier,
                    format_instant(at),
                ),
            )
        if rows:
            work.touch("quality")

    def _pending(
        self,
        db: sqlite3.Connection,
        start: Instant,
        end: Instant,
        members: tuple[tuple[str, str], ...] | None = None,
    ) -> tuple[RecalcSnapshot, ...]:
        catalog = PricingRepository(db).load()
        snapshots = tuple(
            self.snapshot_rows(
                db,
                h,
                start,
                end,
                unpriced=True,
                catalog=catalog,
                keys=tuple(key for name, key in members if name == h.value) if members else None,
            )
            for h in Harness
            if members is None or any(name == h.value for name, _ in members)
        )
        identities = [
            (row.event.harness.value, row.event.dedupe_key) for snapshot in snapshots for row in snapshot.rows
        ]
        decisions: dict[tuple[str, bytes], UnpricedDecision] = {}
        for offset in range(0, len(identities), 500):
            chunk = identities[offset : offset + 500]
            for row in db.execute(
                "SELECT * FROM unpriced_resolution WHERE (harness,dedupe_key) IN (VALUES "
                + ",".join("(?,?)" for _ in chunk)
                + ")",
                tuple(value for name, key in chunk for value in (name, key_bytes(key))),
            ):
                model = None
                if row["model_json"]:
                    data = cast(dict[str, Any], object_json(row["model_json"]))
                    rule = data.pop("billing_rule")
                    model = ModelRate(**data, billing_rule=decode_rule(dumps(rule) if rule else None))
                decisions[row["harness"], key_bytes(row["dedupe_key"])] = UnpricedDecision(
                    bool(row["ignored"]), row["group_id"], row["version_id"], model, row["multiplier"]
                )
        quality = db.execute("SELECT value FROM revision WHERE scope='quality'").fetchone()[0]
        return tuple(
            replace(
                snapshot,
                usage_revision=quality,
                rows=tuple(
                    replace(
                        row,
                        decision=decisions.get((row.event.harness.value, key_bytes(row.event.dedupe_key))),
                    )
                    for row in snapshot.rows
                ),
            )
            for snapshot in snapshots
        )

    def unpriced_snapshots(
        self, start: Instant, end: Instant, work: UnitOfWork | None = None
    ) -> tuple[RecalcSnapshot, ...]:
        if work is not None:
            return self._pending(connection(work), start, end)
        with read_snapshot(self.readers) as db:
            return self._pending(db, start, end)

    def archived_unpriced(self, start: Instant, end: Instant) -> list[dict[str, object]]:
        with read_snapshot(self.readers) as db:
            first, last = format_instant(start), format_instant(end)
            groups: dict[tuple[str, str], dict[tuple[str, str], str | None]] = {}

            def include(row: sqlite3.Row | ArchiveRow) -> None:
                # The same minute can have a late increment in another block.
                # Count each financial/model minute once, across both stores.
                if row["unpriced"]:
                    groups.setdefault((row["harness"], row["model_key"]), {})[
                        row["bucket"], row["vendor_id"]
                    ] = row["model_id"]

            for row in db.execute(
                "SELECT * FROM usage_rollup WHERE unpriced=1 AND bucket>=? AND bucket<?",
                (first, last),
            ):
                include(row)
            for row in archived_rows(db, first, last):
                include(row)
            result: list[dict[str, object]] = []
            for (harness, _), minutes in sorted(groups.items()):
                newest = max(minutes)
                result.append(
                    {
                        "harness": harness,
                        "modelId": minutes[newest],
                        "buckets": len(minutes),
                        "firstAt": min(minutes)[0],
                        "lastAt": newest[0],
                    }
                )
            return result

    def unpriced_notice(self, at: Instant) -> dict[str, object]:
        with read_snapshot(self.readers) as db:
            rows = list(
                db.execute(
                    "SELECT l.harness,COUNT(*) count FROM ledger l LEFT JOIN unpriced_resolution r "
                    "USING(harness,dedupe_key) WHERE l.unpriced=1 AND COALESCE(r.ignored,0)=0 "
                    "AND l.occurred_at>=? GROUP BY l.harness",
                    (format_instant(Instant(at.value - timedelta(days=30))),),
                )
            )
            revision = db.execute("SELECT value FROM revision WHERE scope='quality'").fetchone()[0]
            return {
                "count": sum(row["count"] for row in rows),
                "harnesses": [row["harness"] for row in rows],
                "signature": str(revision) + ":" + str(sum(row["count"] for row in rows)),
            }

    def freeze_unpriced(
        self, work: UnitOfWork, operation: str, pricing_revision: int, start: Instant, end: Instant
    ) -> dict[str, object]:
        db = connection(work)
        db.execute(
            "INSERT INTO unpriced_job(id,pricing_revision,start_at,end_at) VALUES(?,?,?,?)",
            (operation, pricing_revision, format_instant(start), format_instant(end)),
        )
        db.execute(
            "INSERT INTO unpriced_job_member(job_id,harness,dedupe_key) "
            "SELECT ?,l.harness,l.dedupe_key FROM ledger l LEFT JOIN "
            "unpriced_resolution r USING(harness,dedupe_key) "
            "WHERE l.unpriced=1 AND COALESCE(r.ignored,0)=0 AND l.occurred_at>=? AND l.occurred_at<?",
            (operation, format_instant(start), format_instant(end)),
        )
        return self.unpriced_progress(operation, work)

    def unpriced_progress(self, operation: str, work: UnitOfWork | None = None) -> dict[str, object]:
        if work is None:
            with read_snapshot(self.readers) as db:
                return self._progress(db, operation)
        return self._progress(connection(work), operation)

    @staticmethod
    def _progress(db: sqlite3.Connection, operation: str) -> dict[str, object]:
        job = db.execute("SELECT * FROM unpriced_job WHERE id=?", (operation,)).fetchone()
        unresolved = db.execute(
            "SELECT COUNT(*) FROM unpriced_job_member WHERE job_id=? AND status IN ('pending','unresolved')",
            (operation,),
        ).fetchone()[0]
        return {
            "recalculated": job["recalculated"],
            "remaining": unresolved,
            "costDeltaPico": job["cost_delta"],
            "revision": db.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0],
            "complete": bool(job["complete"]),
            "errorCode": None,
        }

    def unpriced_batch(
        self, work: UnitOfWork, operation: str
    ) -> tuple[tuple[tuple[str, str], ...], tuple[RecalcSnapshot, ...]]:
        from htd.domain.common.time import parse_instant

        db = connection(work)
        job = db.execute("SELECT * FROM unpriced_job WHERE id=?", (operation,)).fetchone()
        members = tuple(
            (row[0], row[1])
            for row in db.execute(
                "SELECT harness,dedupe_key FROM unpriced_job_member "
                "WHERE job_id=? AND status='pending' ORDER BY harness,dedupe_key LIMIT 500",
                (operation,),
            )
        )
        snapshots = (
            self._pending(db, parse_instant(job["start_at"]), parse_instant(job["end_at"]), members)
            if members
            else ()
        )
        return members, snapshots

    def advance_unpriced(
        self,
        work: UnitOfWork,
        operation: str,
        members: tuple[tuple[str, str], ...],
        completed: tuple[tuple[str, str], ...],
        delta: int,
    ) -> dict[str, object]:
        db = connection(work)
        db.executemany(
            "UPDATE unpriced_job_member SET status='unresolved' WHERE job_id=? "
            "AND harness=? AND dedupe_key=?",
            ((operation, h, key_bytes(key)) for h, key in members),
        )
        db.executemany(
            "UPDATE unpriced_job_member SET status='completed' WHERE job_id=? AND harness=? AND dedupe_key=?",
            ((operation, h, key_bytes(key)) for h, key in completed),
        )
        db.execute(
            "UPDATE unpriced_job_member SET status='already_priced' WHERE job_id=? AND status='unresolved' "
            "AND EXISTS(SELECT 1 FROM ledger l WHERE l.harness=unpriced_job_member.harness "
            "AND l.dedupe_key=unpriced_job_member.dedupe_key AND l.unpriced=0)",
            (operation,),
        )
        db.execute(
            "UPDATE unpriced_job_member SET status='ignored' WHERE job_id=? AND status='unresolved' "
            "AND EXISTS(SELECT 1 FROM unpriced_resolution r WHERE r.harness=unpriced_job_member.harness "
            "AND r.dedupe_key=unpriced_job_member.dedupe_key AND r.ignored=1)",
            (operation,),
        )
        job = db.execute("SELECT * FROM unpriced_job WHERE id=?", (operation,)).fetchone()
        more = db.execute(
            "SELECT 1 FROM unpriced_job_member WHERE job_id=? AND status='pending' LIMIT 1", (operation,)
        ).fetchone()
        db.execute(
            "UPDATE unpriced_job SET recalculated=recalculated+?,cost_delta=?,complete=? WHERE id=?",
            (len(completed), str(int(job["cost_delta"]) + delta), int(more is None), operation),
        )
        result = self.unpriced_progress(operation, work)
        parameters = {"expectedPricingRevision": job["pricing_revision"]}
        db.execute(
            "UPDATE operation SET result_json=? WHERE id=?",
            (dumps({"parameters": parameters, "result": result}), operation),
        )
        return result
