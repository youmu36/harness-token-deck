"""Synthetic regressions for SQL shapes and audited overrides found during P9.4."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from htu.migration.convert import convert
from htu.migration.verify import verify

from .helpers import copy_legacy


def test_real_derived_columns_and_retention_audit_are_preserved(tmp_path: Path) -> None:
    source = copy_legacy(tmp_path)
    with closing(sqlite3.connect(source)) as db:
        db.execute("ALTER TABLE price_versions DROP COLUMN label")
        for name in ("input", "cache", "output"):
            db.execute(f"ALTER TABLE price_model_rates RENAME COLUMN {name} TO {name}_rate")
        db.execute("ALTER TABLE dsh_price_selections DROP COLUMN harness")
        db.execute("ALTER TABLE retention_runs RENAME COLUMN cutoff TO cutoff_at")
        db.execute("ALTER TABLE retention_runs RENAME COLUMN deleted_ledger_rows TO deleted_ledger")
        db.execute("ALTER TABLE retention_runs DROP COLUMN expired_inbox_rows")
        db.execute("ALTER TABLE retention_runs ADD COLUMN vacuumed INTEGER NOT NULL DEFAULT 1")
        db.execute("ALTER TABLE retention_runs ADD COLUMN retention TEXT NOT NULL DEFAULT '90'")
        db.commit()
    staging = tmp_path / "staging.sqlite3"
    convert(source, staging, detector=lambda: False)
    result = verify(source, staging)
    assert result.ok and all(wallet.incomplete_history for wallet in result.wallets)
    assert not any("派生" in warning for warning in result.warnings)
    with closing(sqlite3.connect(staging)) as db:
        audit = json.loads(
            db.execute("SELECT value FROM meta WHERE key='legacy_retention_runs'").fetchone()[0]
        )
        assert audit[0]["vacuumed"] == 1
        assert db.execute("SELECT expired_inbox FROM retention_run").fetchone()[0] == 0


@pytest.mark.parametrize("tamper", [None, "amount", "range", "latest"])
def test_recalculated_price_override_requires_matching_latest_audit(
    tmp_path: Path, tamper: str | None
) -> None:
    source = copy_legacy(tmp_path)
    with closing(sqlite3.connect(source)) as db:
        item = db.execute("SELECT batch_id,harness,dedupe_key FROM usage_recalc_items LIMIT 1").fetchone()
        assert item is not None
        batch_id, harness, key = item
        db.execute(
            "UPDATE usage_ledger SET price_group_id='group-b',price_version_id='b-v1' "
            "WHERE harness=? AND dedupe_key=?",
            (harness, key),
        )
        db.execute(
            "UPDATE usage_recalc_batches SET group_id='group-b',version_id='b-v1',"
            "start_at='2026-09-01T00:00:00.000Z',end_at='2026-09-10T00:00:00.000Z' WHERE id=?",
            (batch_id,),
        )
        db.execute(
            "UPDATE usage_recalc_items SET new_price_group_id='group-b',new_price_version_id='b-v1' "
            "WHERE batch_id=? AND harness=? AND dedupe_key=?",
            item,
        )
        if tamper == "amount":
            db.execute("UPDATE usage_recalc_items SET new_pico_usd='1'")
        elif tamper == "range":
            db.execute("UPDATE usage_recalc_batches SET start_at='2026-09-09T00:00:00.000Z'")
        elif tamper == "latest":
            columns = [row[1] for row in db.execute("PRAGMA table_info(usage_recalc_batches)")]
            row = list(db.execute("SELECT * FROM usage_recalc_batches WHERE id=?", (batch_id,)).fetchone())
            row[columns.index("id")] = "later"
            row[columns.index("committed_at")] = "2026-09-11T00:00:00.000Z"
            db.execute("INSERT INTO usage_recalc_batches VALUES (" + ",".join("?" for _ in row) + ")", row)
            columns = [row[1] for row in db.execute("PRAGMA table_info(usage_recalc_items)")]
            row = list(
                db.execute(
                    "SELECT * FROM usage_recalc_items WHERE batch_id=? AND harness=? AND dedupe_key=?", item
                ).fetchone()
            )
            row[columns.index("batch_id")] = "later"
            row[columns.index("new_pico_usd")] = "1"
            db.execute("INSERT INTO usage_recalc_items VALUES (" + ",".join("?" for _ in row) + ")", row)
        db.commit()
    staging = tmp_path / "staging.sqlite3"
    convert(source, staging, detector=lambda: False)
    result = verify(source, staging)
    assert result.ok is (tamper is None), result.errors
    assert bool(result.price_mismatches) is (tamper is not None)
    if tamper is None:
        assert any("手动重算" in warning for warning in result.warnings)


@pytest.mark.parametrize("tamper", [False, True])
def test_legacy_routing_override_requires_reproducible_exact_cost(tmp_path: Path, tamper: bool) -> None:
    from argparse import Namespace

    from htu.migration.__main__ import run

    source = copy_legacy(tmp_path)
    # Remove one timeline selection while preserving the actual recorded price.
    # This models legacy supplier routing, without weakening audit validation.
    from .helpers import change_state

    change_state(
        source,
        "configuration",
        lambda state: state.update(selections=[s for s in state["selections"] if s["harness"] != "codex"]),
    )
    if tamper:
        with closing(sqlite3.connect(source)) as db:
            db.execute("UPDATE usage_ledger SET pico_usd='123456789' WHERE harness='codex'")
            db.commit()
    result = run(
        Namespace(
            command="import",
            source=source,
            target=tmp_path / "unused.sqlite3",
            output_dir=tmp_path / "dry-run",
            dry_run=True,
        )
    )
    assert result["ok"] is not tamper
    assert result["activated"] is False
