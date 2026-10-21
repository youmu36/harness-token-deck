"""Atomic ledger snapshot replacement and immutable recalculation audit data."""

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.operation_repo import OperationRepository
from htd.adapters.sqlite.pico_codec import decode_pico, encode_pico
from htd.adapters.sqlite.recalc_codec import pack
from htd.adapters.sqlite.transaction import require_transaction
from htd.adapters.sqlite.usage_codec import dumps
from htd.application.ports import UnitOfWork
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.model import Harness

_FIELDS = (
    "price_group_id",
    "price_version_id",
    "price_revision_id",
    "pico",
    "incomplete",
    "unpriced",
    "display_name",
    "rule_applied",
    "billed_at",
)


@dataclass(frozen=True, slots=True)
class RecalcBatch:
    id: str
    harness: Harness
    start: Instant
    end: Instant
    group_id: str
    version_id: str
    fingerprint: str
    operation_id: str


@dataclass(frozen=True, slots=True)
class RecalcChange:
    harness: Harness
    dedupe_key: str
    old: dict[str, object]
    new: dict[str, object]


class RecalcRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def snapshot(self, harness: Harness, dedupe_key: str) -> dict[str, object]:
        row = self.connection.execute(
            "SELECT " + ",".join(_FIELDS) + " FROM ledger WHERE harness=? AND dedupe_key=?",
            (harness.value, key_bytes(dedupe_key)),
        ).fetchone()
        if row is None:
            raise DomainError("RECALC_PLAN_CHANGED", "账本已变化，请重新预览后再重算")
        return dict(row)

    def commit(
        self,
        work: UnitOfWork,
        batch: RecalcBatch,
        changes: tuple[RecalcChange, ...],
        *,
        at: Instant,
        on_updated: Callable[[UnitOfWork, RecalcChange], None] | None = None,
        record_receipt: bool = True,
    ) -> str:
        require_transaction(work, self.connection)
        if batch.end <= batch.start or len({(c.harness, c.dedupe_key) for c in changes}) != len(changes):
            raise ValueError("Invalid recalculation window or duplicate rows")
        params: dict[str, object] = {
            "harness": batch.harness.value,
            "start": format_instant(batch.start),
            "end": format_instant(batch.end),
            "group_id": batch.group_id,
            "version_id": batch.version_id,
            "fingerprint": batch.fingerprint,
            "changes": [
                {"harness": c.harness.value, "key": c.dedupe_key, "old": c.old, "new": c.new} for c in changes
            ],
        }
        operations = OperationRepository(self.connection)
        replay = (
            operations.replay(batch.operation_id, "maintenance.recalc", params) if record_receipt else None
        )
        if replay is not None:
            return str(replay["batch_id"])
        old_total = new_total = unpriced = incomplete = 0
        for change in changes:
            if (
                change.harness != batch.harness
                or set(change.new) != set(_FIELDS)
                or self.snapshot(change.harness, change.dedupe_key) != change.old
            ):
                raise DomainError("RECALC_PLAN_CHANGED", "账本已变化，请重新预览后再重算")
            occurred = self.connection.execute(
                "SELECT occurred_at FROM ledger WHERE harness=? AND dedupe_key=?",
                (change.harness.value, key_bytes(change.dedupe_key)),
            ).fetchone()[0]
            if not format_instant(batch.start) <= occurred < format_instant(batch.end):
                raise ValueError("Recalculation row outside window")
            new = change.new
            if (new["price_group_id"], new["price_version_id"]) != (batch.group_id, batch.version_id):
                raise ValueError("Recalculation target mismatch")
            for flag in ("unpriced", "incomplete"):
                if type(new[flag]) is not int or new[flag] not in (0, 1):
                    raise ValueError("Invalid quality flag")
            if new["rule_applied"] not in ("none", "base", "peak", "tier"):
                raise ValueError("Invalid billing rule")
            if new["display_name"] is not None and type(new["display_name"]) is not str:
                raise ValueError("Invalid display name")
            if new["price_revision_id"] is not None and type(new["price_revision_id"]) is not str:
                raise ValueError("Invalid revision ID")
            if type(new["billed_at"]) is not str:
                raise ValueError("Invalid billing timestamp")
            parse_instant(new["billed_at"])
            if type(new["pico"]) is not str or type(change.old["pico"]) is not str:
                raise ValueError("Invalid amount text")
            amount = decode_pico(new["pico"])
            if amount < 0:
                raise ValueError("Negative ledger amount")
            old_total += decode_pico(change.old["pico"])
            new_total += amount
            unpriced += new["unpriced"] == 1
            incomplete += new["incomplete"] == 1
        changed = tuple(c for c in changes if any(c.old[k] != c.new[k] for k in _FIELDS if k != "billed_at"))
        identity_fingerprint = hashlib.sha256(
            dumps(sorted((c.harness.value, c.dedupe_key) for c in changes)).encode()
        ).hexdigest()
        self.connection.execute(
            "INSERT INTO recalc_batch VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch.id,
                batch.harness.value,
                format_instant(batch.start),
                format_instant(batch.end),
                batch.group_id,
                batch.version_id,
                len(changes),
                encode_pico(old_total),
                encode_pico(new_total),
                unpriced,
                incomplete,
                batch.fingerprint,
                format_instant(at),
                batch.operation_id,
                len(changed),
                len(changes) - len(changed),
                identity_fingerprint,
            ),
        )
        for change in changed:
            self.connection.execute(
                "UPDATE ledger SET "
                + ",".join(field + "=?" for field in _FIELDS)
                + " WHERE harness=? AND dedupe_key=?",
                (
                    *[change.new[field] for field in _FIELDS],
                    change.harness.value,
                    key_bytes(change.dedupe_key),
                ),
            )
            self.connection.execute(
                "INSERT INTO recalc_item VALUES (?,?,?,?,?,?)",
                (
                    batch.id,
                    change.harness.value,
                    key_bytes(change.dedupe_key),
                    "",
                    "",
                    pack(change.old, change.new),
                ),
            )
            if on_updated is not None:
                on_updated(work, change)
        self.connection.execute(
            "INSERT INTO diagnostic_log VALUES(NULL,?,?,'recalc_committed',?)",
            (format_instant(at), batch.harness.value, f"重算 {len(changes)} 条账本"),
        )
        if record_receipt:
            operations.record(
                work, batch.operation_id, "maintenance.recalc", params, {"batch_id": batch.id}, at=at
            )
        if changed:
            work.touch("usage")
            if any(
                change.old["unpriced"] != change.new["unpriced"]
                or change.old["incomplete"] != change.new["incomplete"]
                for change in changed
            ):
                work.touch("quality")
        return batch.id
