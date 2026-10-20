"""Exercise all synthetic versions, exact wallet oracles, corruption, and source continuation."""

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from htu.adapters.collectors.codex_reader import CodexReader
from htu.adapters.collectors.dsh_reader import DshReader
from htu.adapters.collectors.zcode_reader import ZcodeReader
from htu.adapters.sqlite.connection import open_connection
from htu.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htu.adapters.sqlite.migrate import MIGRATIONS, migrate
from htu.adapters.sqlite.pricing_load import load_catalog
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import WriteExecutor
from htu.application.ports import CheckpointRecord
from htu.bootstrap import Application
from htu.domain.common.errors import DomainError
from htu.domain.common.time import parse_instant
from htu.ipc.dto.preferences import PreferencesResponse
from htu.migration.convert import convert
from htu.migration.report import write_report
from htu.migration.verify import verify

from .helpers import FIXTURES, change_state, copy_legacy


@pytest.mark.parametrize("version", ["v022", "v023", "v030"])
def test_all_records_wallet_oracle_and_byte_repeatability(tmp_path: Path, version: str) -> None:
    source = FIXTURES / "legacy" / (version + ".sqlite3")
    before = source.read_bytes()
    expected = json.loads((FIXTURES / "legacy/expected.json").read_text(encoding="utf-8"))["databases"][
        source.name
    ]
    first, second = tmp_path / "first.sqlite3", tmp_path / "second.sqlite3"
    prepared = convert(source, first, detector=lambda: False)
    convert(source, second, detector=lambda: False)
    assert first.read_bytes() == second.read_bytes()
    assert source.read_bytes() == before
    result = verify(source, first)
    assert result.ok, result.errors
    assert all(result.checks.values()) and not result.price_mismatches and not result.dropped_checkpoints
    assert result.old_total == result.new_total == int(expected["ledgerPicoUsd"])
    assert {w.vendor_id: str(w.new) for w in result.wallets} == {
        key: value["balancePicoUsd"] for key, value in expected["vendors"].items()
    }
    assert {w.vendor_id: str(w.r) for w in result.wallets} == {
        key: value["topUpsPicoUsd"] for key, value in expected["vendors"].items()
    }
    with closing(open_connection(first, query_only=True)) as db:
        catalog = load_catalog(db)
        assert bool(catalog.activations) == (version == "v030")
        assert all(row[0] == "legacy" for row in db.execute("SELECT rule_applied FROM ledger"))
        assert all(row[0] == "legacy" for row in db.execute("SELECT origin FROM identity"))
        assert all(row[0] == "pending" for row in db.execute("SELECT status FROM source"))
        if version == "v030":
            wallet = WalletRepository(db)
            assert wallet.totals("vendor-alpha").last_calibrated_at == parse_instant(
                "2026-09-03T00:00:00.000Z"
            )
            assert wallet.totals("vendor-beta").incomplete_count == 1
            assert wallet.totals("vendor-alpha").unpriced_count == 1
            assert all(w.incomplete_history for w in result.wallets)
            assert prepared.catalog.hints[1].multiplier == "1.75"
    report = write_report(result, tmp_path / "report.md", elapsed_seconds=0.1)
    content = report.read_text(encoding="utf-8")
    assert result.sha256 in content and "B_old" in content and "按迁移时关联推定" in content


@pytest.mark.parametrize("occupied", ["ledger", "wallet"])
def test_existing_user_data_rejected_without_mutation(tmp_path: Path, occupied: str) -> None:
    source = FIXTURES / "legacy/v030.sqlite3"
    target = tmp_path / "target.sqlite3"
    convert(source, target, detector=lambda: False)
    if occupied == "wallet":
        with closing(open_connection(target)) as db:
            db.execute("DELETE FROM ledger")
    before = target.read_bytes()
    staging = tmp_path / "staging.sqlite3"
    with pytest.raises(DomainError) as error:
        convert(source, staging, target=target, detector=lambda: False)
    assert error.value.code == "MIGRATION_TARGET_NOT_EMPTY"
    assert target.read_bytes() == before and not staging.exists()


@pytest.mark.parametrize(
    "table,sql",
    [
        ("ledger", "UPDATE ledger SET pico='1' WHERE dedupe_key=(SELECT dedupe_key FROM ledger LIMIT 1)"),
        ("wallet_total", "UPDATE wallet_total SET a='0' WHERE vendor_id='vendor-alpha'"),
        ("preference", "UPDATE preference SET value_json='\"light\"' WHERE key='theme'"),
        ("price_version_label", "UPDATE price_version_label SET label='tampered'"),
        ("checkpoint", "DELETE FROM checkpoint WHERE harness='codex'"),
    ],
)
def test_corruption_cannot_pass_verification(tmp_path: Path, table: str, sql: str) -> None:
    source = FIXTURES / "legacy/v030.sqlite3"
    staging = tmp_path / "staging.sqlite3"
    convert(source, staging, detector=lambda: False)
    with closing(open_connection(staging)) as db:
        db.execute(sql)
    result = verify(source, staging)
    assert not result.ok and result.errors, table
    before = staging.read_bytes()
    report = write_report(result, tmp_path / "report.md")
    assert "失败，禁止激活" in report.read_text(encoding="utf-8")
    assert staging.read_bytes() == before


def test_independently_reproducible_recorded_price_is_preserved_without_changing_old_amount(
    tmp_path: Path,
) -> None:
    source = copy_legacy(tmp_path)

    def modify(data: dict[str, object]) -> None:
        data["selections"] = [
            {"harness": "codex", "groupId": "group-b", "effectiveAt": "2026-09-01T00:00:00.000Z"}
        ]

    change_state(source, "configuration", modify)
    staging = tmp_path / "staging.sqlite3"
    convert(source, staging, detector=lambda: False)
    result = verify(source, staging)
    assert result.ok and not result.price_mismatches
    assert any("逐条重算" in warning for warning in result.warnings)
    assert result.old_total == result.new_total


def test_derived_prices_only_warn_and_json_remains_authoritative(tmp_path: Path) -> None:
    source = copy_legacy(tmp_path)
    with closing(sqlite3.connect(source)) as db:
        db.execute("UPDATE price_groups SET name='wrong' WHERE id='group-a'")
        db.execute("UPDATE price_model_rates SET input='999' WHERE model_id='legacy-model'")
        db.commit()
    staging = tmp_path / "staging.sqlite3"
    convert(source, staging, detector=lambda: False)
    result = verify(source, staging)
    assert result.ok and len(result.warnings) == 2
    assert "price_groups" in result.warnings[0] and "price_model_rates" in result.warnings[1]
    assert any(row["originalModels"][0].get("input") == "4.000000" for row in result.price_mapping)


def test_foreign_key_integrity_fails_even_when_counts_and_amounts_match(tmp_path: Path) -> None:
    source = FIXTURES / "legacy/v030.sqlite3"
    staging = tmp_path / "staging.sqlite3"
    convert(source, staging, detector=lambda: False)
    with closing(sqlite3.connect(staging)) as db:
        db.execute("UPDATE inbox SET dedupe_key=? WHERE harness='codex'", (bytes.fromhex("f" * 64),))
        db.commit()
    result = verify(source, staging)
    assert not result.ok and result.checks["foreign_key_check"] is False


@pytest.mark.parametrize("amount", ["-1", "01", "1.0"])
def test_invalid_legacy_ledger_amount_rejected_before_staging(tmp_path: Path, amount: str) -> None:
    source = copy_legacy(tmp_path)
    with closing(sqlite3.connect(source)) as db:
        db.execute("UPDATE usage_ledger SET pico_usd=?", (amount,))
        db.commit()
    staging = tmp_path / "staging.sqlite3"
    with pytest.raises(DomainError):
        convert(source, staging, detector=lambda: False)
    assert not staging.exists()


def test_failed_write_preserves_empty_staging_and_active_target(tmp_path: Path) -> None:
    source = copy_legacy(tmp_path)
    with closing(sqlite3.connect(source)) as db:
        db.execute("UPDATE usage_identities SET billed=2")
        db.commit()
    target, staging = tmp_path / "active.sqlite3", tmp_path / "staging.sqlite3"
    migrate(target)
    before = target.read_bytes()
    with pytest.raises(sqlite3.IntegrityError):
        convert(source, staging, target=target, detector=lambda: False)
    assert staging.exists() and target.read_bytes() == before
    with closing(open_connection(staging, query_only=True)) as db:
        assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM price_group").fetchone()[0] == 0


def test_unparseable_checkpoint_is_dropped_and_reported(tmp_path: Path) -> None:
    source = copy_legacy(tmp_path)
    with closing(sqlite3.connect(source)) as db:
        db.execute("UPDATE collection_checkpoints SET state_json='invalid' WHERE harness='codex'")
        db.commit()
    staging = tmp_path / "staging.sqlite3"
    prepared = convert(source, staging, detector=lambda: False)
    assert len(prepared.records.dropped_checkpoints) == 1
    result = verify(source, staging)
    assert result.ok and len(result.dropped_checkpoints) == 1 and result.warnings
    assert result.counts["checkpoint"] == (2, 2)


def test_converted_checkpoints_resume_all_three_sources(tmp_path: Path) -> None:
    source = FIXTURES / "legacy/v030.sqlite3"
    staging = tmp_path / "staging.sqlite3"
    convert(source, staging, detector=lambda: False)
    with closing(open_connection(staging, query_only=True)) as db:
        checkpoints = {
            row["harness"]: json.loads(row["state_json"]) for row in db.execute("SELECT * FROM checkpoint")
        }
    codex = tmp_path / "desktop.jsonl"
    shutil.copyfile(FIXTURES / "codex/desktop.jsonl", codex)
    started = parse_instant("2026-09-01T00:00:00.000Z")
    batch = CodexReader().read(str(codex), CheckpointRecord(checkpoints["codex"]), started)
    # The newer parser rereads old checkpoints to recover provider metadata.
    # The original start and the migrated identity table still prevent rebilling.
    assert batch.available and len(batch.events) == 1
    assert not {"source_rewritten", "invalid_checkpoint"}.intersection(batch.diagnostics)
    assert batch.checkpoint["offset"] == len(codex.read_bytes())
    resumed = CodexReader().read(str(codex), CheckpointRecord(batch.checkpoint), started)
    assert resumed.available and not resumed.events
    zcode = tmp_path / "usage.sqlite3"
    with closing(sqlite3.connect(zcode)) as db:
        db.executescript((FIXTURES / "zcode/schema.sql").read_text())
        for index in range(1, 5):
            db.execute(
                "INSERT INTO model_usage (id,session_id,logical_request_id,status) VALUES (?,?,?,'running')",
                (str(index), "session", "request-" + str(index)),
            )
        db.commit()
    batch = ZcodeReader().read(str(zcode), CheckpointRecord(checkpoints["zcode"]), started)
    assert batch.available and not batch.events
    assert not {"source_rewritten", "invalid_checkpoint"}.intersection(batch.diagnostics)
    assert batch.checkpoint["rowId"] == 4 and batch.checkpoint["pending"] == [4]
    batch = DshReader().read(
        str(FIXTURES / "dsh/session.jsonl"), CheckpointRecord(checkpoints["dsh"]), started
    )
    assert batch.available and not {"source_rewritten", "invalid_checkpoint"}.intersection(batch.diagnostics)


def test_imported_database_opens_with_normal_application_writer(tmp_path: Path) -> None:
    target = tmp_path / "htu.sqlite3"
    convert(FIXTURES / "legacy/v030.sqlite3", target, detector=lambda: False)
    with WriteExecutor(target) as writer:
        assert (
            writer.execute(lambda work: WalletRepository(work.connection).totals("vendor-alpha").b)
            == 62_749_994_000_000
        )


def test_imported_preferences_and_maintenance_are_usable_through_ipc(tmp_path: Path) -> None:
    target = tmp_path / "htu.sqlite3"
    convert(FIXTURES / "legacy/v030.sqlite3", target, detector=lambda: False)
    with Application(tmp_path, start_scheduler=False) as app:
        response = app.bridge.call("preferences.get", {})
        assert response["ok"], response
        prefs = PreferencesResponse.model_validate(response["data"]).preferences
        assert prefs.filters["codex"].range == "custom"
        assert prefs.filters["codex"].start == "2026-09-01T08:00:00.000Z"
        assert prefs.filters["dsh"].range == "30days"
        assert app.writer.execute(lambda work: MaintenanceRepository(work.connection).retention()) == 90
        legacy = app.writer.execute(
            lambda work: json.loads(
                work.connection.execute(
                    "SELECT value_json FROM preference WHERE key='legacyFilters'"
                ).fetchone()[0]
            )
        )
        assert legacy["codex"]["start"] == "2026-09-01T08:00"


def test_invalid_custom_filter_preserves_original_text_and_reports_default(tmp_path: Path) -> None:
    source = copy_legacy(tmp_path)

    def modify(data: dict[str, object]) -> None:
        data["filters"] = {"codex": {"preset": "custom", "start": "invalid", "end": ""}}

    change_state(source, "configuration", modify)
    staging = tmp_path / "staging.sqlite3"
    convert(source, staging, detector=lambda: False)
    result = verify(source, staging)
    assert result.ok and any("legacyFilters" in warning for warning in result.warnings)
    with closing(open_connection(staging, query_only=True)) as db:
        filters = json.loads(
            db.execute("SELECT value_json FROM preference WHERE key='filters'").fetchone()[0]
        )
        legacy = json.loads(
            db.execute("SELECT value_json FROM preference WHERE key='legacyFilters'").fetchone()[0]
        )
        assert filters["codex"] == {"range": "today"} and legacy["codex"]["start"] == "invalid"


def test_legacy_marker_upgrade_preserves_existing_rows_and_indexes(tmp_path: Path) -> None:
    source = FIXTURES / "legacy/v022.sqlite3"
    latest = tmp_path / "latest.sqlite3"
    convert(source, latest, detector=lambda: False)
    with closing(open_connection(latest)) as db:
        row = dict(db.execute("SELECT * FROM ledger LIMIT 1").fetchone())
    older = tmp_path / "old-schema"
    older.mkdir()
    for script in MIGRATIONS.glob("*.sql"):
        if script.name < "0007":
            shutil.copyfile(script, older / script.name)
    target = tmp_path / "existing.sqlite3"
    migrate(target, migrations=older)
    row["rule_applied"] = "base"
    with closing(open_connection(target)) as db:
        keys = tuple(row)
        db.execute(
            f"INSERT INTO ledger ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
            tuple(row.values()),
        )
    migrate(target)
    with closing(open_connection(target)) as db:
        assert dict(db.execute("SELECT * FROM ledger").fetchone()) == row
        assert {r["name"] for r in db.execute("PRAGMA index_list(ledger)")} >= {
            "ledger_time",
            "ledger_session",
            "ledger_group",
            "ledger_statistics",
        }
