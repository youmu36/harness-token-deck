"""Immutable recalculation audit and wallet callbacks share the ledger transaction."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from htu.adapters.sqlite.recalc_repo import RecalcBatch, RecalcChange, RecalcRepository
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.application.ports import UnitOfWork as WorkPort
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant
from htu.domain.usage.model import Harness
from htu.domain.wallet.model import WalletEntry

from backend.tests.integration.collection_helpers import AT, usage
from backend.tests.integration.test_maintenance_storage import RECENT, seed
from backend.tests.integration.test_wallet_storage import consumption, vendor

BATCH = RecalcBatch(
    "batch",
    Harness.CODEX,
    RECENT,
    Instant(AT.value + timedelta(minutes=1)),
    "g",
    "v",
    "fingerprint",
    "operation",
)


def plan(work: UnitOfWork) -> RecalcChange:
    repo = RecalcRepository(work.connection)
    old = repo.snapshot(Harness.CODEX, usage(1).dedupe_key)
    new = dict(old)
    new.update(
        price_group_id="g", price_version_id="v", price_revision_id="r", pico="100", display_name="New"
    )
    return RecalcChange(Harness.CODEX, usage(1).dedupe_key, old, new)


def test_recalc_audit_restart_idempotence(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(seed)
        change = writer.execute(plan)
        writer.execute(lambda w: RecalcRepository(w.connection).commit(w, BATCH, (change,), at=AT))
    with WriteExecutor(path) as writer:
        assert (
            writer.execute(
                lambda w: RecalcRepository(w.connection).commit(
                    w, replace(BATCH, id="unused"), (change,), at=AT
                )
            )
            == "batch"
        )
        assert (
            writer.execute(
                lambda w: RecalcRepository(w.connection).snapshot(Harness.CODEX, usage(1).dedupe_key)
            )
            == change.new
        )
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM recalc_item").fetchone()[0])
            == 1
        )
        with pytest.raises(DomainError) as conflict:
            writer.execute(
                lambda w: RecalcRepository(w.connection).commit(
                    w, replace(BATCH, operation_id="another"), (change,), at=AT
                )
            )
        assert conflict.value.code == "RECALC_PLAN_CHANGED"


@pytest.mark.parametrize("table", ["recalc_item", "wallet_total", "operation", "diagnostic_log"])
def test_recalc_wallet_and_audit_rollback(tmp_path: Path, table: str) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(seed)
        writer.execute(vendor)
        original = writer.execute(lambda w: consumption(w, at=RECENT))
        change = writer.execute(plan)
        before = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))

        def callback(work: WorkPort, updated: RecalcChange) -> None:
            assert isinstance(work, UnitOfWork)
            repo = WalletRepository(work.connection)
            record = WalletEntry(
                "delta",
                "vendor",
                "consumption_delta",
                1,
                RECENT,
                repo.next_seq(),
                usage_ref=original.usage_ref,
                related_entry_id=original.id,
                recalc_batch_id=BATCH.id,
            )
            repo.append_with_offsets(work, record, at=AT)

        verb = "UPDATE" if table == "wallet_total" else "INSERT"
        writer.execute(
            lambda w: w.connection.execute(
                f"CREATE TRIGGER fail BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            writer.execute(
                lambda w: RecalcRepository(w.connection).commit(
                    w, BATCH, (change,), at=AT, on_updated=callback
                )
            )
        assert (
            writer.execute(
                lambda w: RecalcRepository(w.connection).snapshot(Harness.CODEX, usage(1).dedupe_key)
            )
            == change.old
        )
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")) == before
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM recalc_batch").fetchone()[0])
            == 0
        )


@pytest.mark.parametrize("table", ["recalc_batch", "recalc_item"])
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_recalc_audit_append_only(tmp_path: Path, table: str, verb: str) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(seed)
        change = writer.execute(plan)
        writer.execute(lambda w: RecalcRepository(w.connection).commit(w, BATCH, (change,), at=AT))
        sql = f"DELETE FROM {table}" if verb == "DELETE" else f"UPDATE {table} SET harness=harness"
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            writer.execute(lambda w: w.connection.execute(sql))
