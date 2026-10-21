"""Synthetic old-format inputs and their independent wallet oracle must stay reproducible."""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from scripts.make_legacy_fixtures import SNAPSHOT_AT, VERSIONS, generate, normalize_writer_version

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/legacy"


def read_state(db: sqlite3.Connection, table: str) -> dict[str, Any]:
    return json.loads(db.execute(f"SELECT state_json FROM {table} WHERE id=1").fetchone()[0])


def test_repeat_generation_matches_committed_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "legacy"
    first = generate(destination)
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    assert generate(destination) == first
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    assert before == {name: (FIXTURES / name).read_bytes() for name in before}


@pytest.mark.parametrize("version", VERSIONS)
def test_sqlite_writer_version_does_not_change_fixture_bytes(tmp_path: Path, version: str) -> None:
    original = (FIXTURES / f"{version}.sqlite3").read_bytes()
    path = tmp_path / f"{version}.sqlite3"
    # Python 3.12.10 on the CI runner writes SQLite 3.49.1 into this header field.
    path.write_bytes(original[:96] + (3_049_001).to_bytes(4, "big") + original[100:])
    normalize_writer_version(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "version,user_version,ledger_count,inbox_count",
    [
        ("v022", 6, 9, 0),
        ("v023", 7, 10, 0),
        ("v030", 6, 11, 3),
    ],
)
def test_manifest_matches_old_database(
    version: str, user_version: int, ledger_count: int, inbox_count: int
) -> None:
    path = FIXTURES / f"{version}.sqlite3"
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))["databases"][path.name]
    assert expected["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == user_version
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, count in expected["rowCounts"].items():
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
        assert expected["rowCounts"]["usage_ledger"] == ledger_count
        assert expected["rowCounts"]["collection_inbox"] == inbox_count
        amounts = db.execute("SELECT harness, occurred_at, pico_usd FROM usage_ledger").fetchall()
        assert str(sum(int(row[2]) for row in amounts)) == expected["ledgerPicoUsd"]
        for harness in ("codex", "zcode", "dsh"):
            assert (
                str(sum(int(row[2]) for row in amounts if row[0] == harness))
                == expected["harnessPicoUsd"][harness]
            )
        for day, total in expected["harnessDatePicoUsd"].items():
            harness, date = day.split(":")
            assert (
                str(sum(int(row[2]) for row in amounts if row[0] == harness and row[1][:10] == date)) == total
            )
        for harness, dedupe, occurred, payload in db.execute(
            "SELECT harness, dedupe_key, occurred_at, event_json FROM collection_inbox"
        ):
            usage = json.loads(payload)
            assert (usage["harness"], usage["dedupeKey"], usage["occurredAt"]) == (harness, dedupe, occurred)
            assert set(usage["tokens"]) == {"input", "cacheRead", "cacheWrite", "output", "reasoning"}
            assert db.execute(
                "SELECT billed FROM usage_identities WHERE harness=? AND dedupe_key=?", (harness, dedupe)
            ).fetchone() == (0,)


def test_version_shapes_and_checkpoint_anchor() -> None:
    for version in VERSIONS:
        path = FIXTURES / f"{version}.sqlite3"
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
            state = read_state(db, "configuration")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            main_dsh = db.execute("SELECT COUNT(*) FROM price_selections WHERE harness='dsh'").fetchone()[0]
            if version == "v022":
                assert "schema_metadata" not in tables and "configuration_extensions" not in tables
                assert "dsh_price_selections" not in tables and main_dsh == 0
                assert "historyPrompted" not in state and "modelPool" not in state
            else:
                ext = read_state(db, "configuration_extensions")
                assert db.execute("SELECT storage_generation FROM schema_metadata").fetchone() == (6,)
                assert main_dsh == (1 if version == "v023" else 0)
                assert bool(ext.get("groupVendors")) == (version == "v030")
                assert bool(ext.get("modelMultipliers")) == (version == "v030")
                assert bool(ext["dshSelections"]) == (version == "v030")
            checkpoints = {
                h: json.loads(payload)
                for h, payload in db.execute("SELECT harness, state_json FROM collection_checkpoints")
            }
            payload = (FIXTURES.parent / "codex/desktop.jsonl").read_bytes()
            assert checkpoints["codex"]["offset"] == len(payload)
            assert checkpoints["codex"]["anchor"] == hashlib.sha256(payload[-512:]).hexdigest()
            assert checkpoints["zcode"] == {"row_id": 4, "tail_id": "4", "pending": [4]}
            if version == "v030":
                assert set(checkpoints) == {"codex", "zcode", "dsh"}
                assert db.execute("SELECT old_pico_usd, new_pico_usd FROM usage_recalc_items").fetchone() == (
                    "3000000000000",
                    "4000000000000",
                )
                # Retention keeps an already-billed identity without its former ledger row.
                assert db.execute(
                    "SELECT COUNT(*) FROM usage_identities i LEFT JOIN usage_ledger l "
                    "USING(harness,dedupe_key) WHERE i.billed=1 AND l.dedupe_key IS NULL"
                ).fetchone() == (1,)


def test_hand_calculated_wallet_boundaries_and_exact_amounts() -> None:
    databases = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))["databases"]
    assert databases["v022.sqlite3"]["vendors"] == {}
    older = databases["v023.sqlite3"]["vendors"]
    # The last associated version owns the whole group in the old balance algorithm.
    assert older["vendor-alpha"]["groupIds"] == ["group-b"]
    assert older["vendor-alpha"]["balancePicoUsd"] == "88000000000000"  # 100 - 12
    assert older["vendor-beta"]["balancePicoUsd"] == "36000000000000"  # 50 - 14
    vendors = databases["v030.sqlite3"]["vendors"]
    alpha, beta = vendors["vendor-alpha"], vendors["vendor-beta"]
    assert alpha["groupIds"] == ["group-a"]  # groupVendors overrides versionVendors.
    assert alpha["anchorInclusive"] is False
    assert alpha["topUpsAfterAnchorPicoUsd"] == "10250000000000"  # Equal-time 5 excluded.
    assert alpha["consumptionAfterAnchorPicoUsd"] == "8000006000000"  # Equal-time 3 excluded.
    assert alpha["balancePicoUsd"] == "62749994000000"  # 60.5 + 10.25 - 8.000006
    assert beta["createdAt"] == "2026-09-02T00:00:00.000Z" and beta["anchorInclusive"] is True
    assert beta["topUpsPicoUsd"] == "10000500001000000"  # Above JS safe integer, exact.
    assert beta["balancePicoUsd"] == "9993500001000000"  # 10000.500001 - (2 + 5)
    assert vendors["vendor-gamma"]["balancePicoUsd"] == "-500000000000"
    assert vendors["vendor-empty"]["createdAt"] == SNAPSHOT_AT
    assert vendors["vendor-empty"]["balancePicoUsd"] == "0"
    assert [databases[f"{version}.sqlite3"]["ledgerPicoUsd"] for version in VERSIONS] == [
        "22000000000000",
        "26000000000000",
        "26000006000000",
    ]
