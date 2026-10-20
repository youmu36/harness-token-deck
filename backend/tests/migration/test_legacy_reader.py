"""Verify legacy recognition, merge boundaries, read-only snapshots, and loader validation."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from htu.domain.common.errors import DomainError
from htu.domain.common.time import parse_instant
from htu.migration.convert import prepare
from htu.migration.legacy import merge_legacy, open_legacy, read_legacy
from htu.migration.legacy.reader import legacy_running
from htu.migration.snapshot import snapshot, snapshot_time

from .helpers import AT, FIXTURES, change_state, copy_legacy


@pytest.mark.parametrize(
    "version,expected,user_version", [("v022", "0.2.2", 6), ("v023", "0.2.3", 7), ("v030", "0.2.3~0.3.0", 6)]
)
def test_identify_and_read_all_tables_without_writes(version: str, expected: str, user_version: int) -> None:
    path = FIXTURES / "legacy" / (version + ".sqlite3")
    before = path.read_bytes()
    result = read_legacy(path)
    assert (result.identity.version, result.identity.user_version) == (expected, user_version)
    with closing(open_legacy(path)) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM configuration")
        for table, rows in result.tables.items():
            assert len(rows) == db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert path.read_bytes() == before


@pytest.mark.parametrize("version", [5, 8, 0])
def test_unsupported_schema_is_rejected(tmp_path: Path, version: int) -> None:
    path = copy_legacy(tmp_path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(f"PRAGMA user_version={version}")
    with pytest.raises(DomainError) as error:
        read_legacy(path)
    assert error.value.code == "LEGACY_SCHEMA_UNSUPPORTED"


def test_merge_defaults_dsh_global_max_and_model_key_dedup(tmp_path: Path) -> None:
    path = copy_legacy(tmp_path, "v022")

    def modify(main: dict[str, Any]) -> None:
        groups = main["groups"]
        groups[0]["versions"][1]["revisions"][-1]["effectiveAt"] = "2026-09-08T00:00:00.000Z"
        groups[0]["versions"][0]["models"].append({"modelId": " Legacy-Model ", "displayName": "duplicate"})

    change_state(path, "configuration", modify)
    merged = merge_legacy(read_legacy(path))
    assert merged.selections[-1] == {
        "harness": "dsh",
        "groupId": "group-a",
        "effectiveAt": "2026-09-08T00:00:00.000Z",
    }
    assert merged.main["modelPool"] == ["legacy-model", "legacy-other"]
    assert merged.main["historyPrompted"] is True
    assert merged.main["settings"]["showScanCountdown"] is False
    assert merged.main["filters"]["dsh"] == {"preset": "today", "start": "", "end": ""}
    assert merged.vendors == () and merged.group_vendors == {}


def test_extension_merge_and_main_user_version_seven_dsh() -> None:
    merged = merge_legacy(read_legacy(FIXTURES / "legacy/v023.sqlite3"))
    assert [item["harness"] for item in merged.selections] == ["codex", "zcode", "codex", "dsh"]
    assert merged.group_vendors == {"group-a": "vendor-beta", "group-b": "vendor-alpha"}
    assert merged.main["activePage"] == "overview"
    assert merged.main["filters"]["dsh"]["preset"] == "30d"
    assert merged.vendors[0]["createdAt"] == "2026-09-01T00:00:00.000Z"
    assert merged.vendors[0]["balanceCalibrations"] == []


def test_stale_extension_page_and_missing_vendor_fallback(tmp_path: Path) -> None:
    path = copy_legacy(tmp_path)

    def modify(data: dict[str, Any]) -> None:
        data["configurationRevision"] = 0
        data["groupVendors"] = [{"groupId": "group-a", "vendorId": "missing"}]

    change_state(path, "configuration_extensions", modify)
    merged = merge_legacy(read_legacy(path))
    assert merged.main["activePage"] == "prices"
    assert merged.group_vendors["group-a"] == "vendor-beta"
    assert merged.main["filters"]["dsh"]["preset"] == "30d"


def test_reverse_single_harness_timeline_is_rejected_and_never_repaired(tmp_path: Path) -> None:
    path = copy_legacy(tmp_path)

    def modify(data: dict[str, Any]) -> None:
        data["selections"] = [
            {"harness": "codex", "groupId": "group-a", "effectiveAt": "2026-09-03T00:00:00.000Z"},
            {"harness": "codex", "groupId": "group-a", "effectiveAt": "2026-09-01T00:00:00.000Z"},
        ]

    change_state(path, "configuration", modify)
    before = path.read_bytes()
    with pytest.raises(DomainError) as error:
        merge_legacy(read_legacy(path))
    assert error.value.code == "LEGACY_SELECTION_ORDER_INVALID"
    assert error.value.field == "selections[1].effectiveAt"
    assert path.read_bytes() == before


def test_same_time_selection_is_stable_and_dsh_uses_activation_max(tmp_path: Path) -> None:
    path = copy_legacy(tmp_path, "v022")

    def modify(data: dict[str, Any]) -> None:
        data["selections"] = [
            {"harness": "codex", "groupId": "group-b", "effectiveAt": "2026-09-01T00:00:00.000Z"},
            {"harness": "codex", "groupId": "group-a", "effectiveAt": "2026-09-01T00:00:00.000Z"},
        ]
        data["groups"][0]["disabled"] = True
        data["groups"][1]["activations"] = [{"versionId": "b-v1", "effectiveAt": "2026-09-09T00:00:00.000Z"}]

    change_state(path, "configuration", modify)
    merged = merge_legacy(read_legacy(path))
    assert [row["groupId"] for row in merged.selections] == ["group-b", "group-a", "group-b"]
    assert merged.selections[-1]["effectiveAt"] == "2026-09-09T00:00:00.000Z"


@pytest.mark.parametrize("broken", ["rate", "revision", "models", "activation"])
def test_invalid_price_paths_are_reported(tmp_path: Path, broken: str) -> None:
    path = copy_legacy(tmp_path)

    def modify(data: dict[str, Any]) -> None:
        groups = data["groups"]
        version = groups[0]["versions"][1]
        if broken == "rate":
            version["models"][0]["input"] = "-1"
        elif broken == "revision":
            version["revisions"][0]["effectiveAt"] = "2026-09-02T00:00:00.000Z"
        elif broken == "models":
            version["revisions"][-1]["models"][0]["output"] = "99"
        else:
            groups[0]["activations"][0]["versionId"] = "b-v1"

    change_state(path, "configuration", modify)
    with pytest.raises(DomainError) as error:
        prepare(path)
    assert error.value.field is not None


def test_snapshot_includes_committed_wal_and_source_bytes_unchanged(tmp_path: Path) -> None:
    path = copy_legacy(tmp_path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE configuration SET revision=31")
        writer.commit()
        before = {suffix: path.with_name(path.name + suffix).read_bytes() for suffix in ("", "-wal")}
        result = snapshot(path, tmp_path / "snapshot.sqlite3", at=parse_instant(AT), detector=lambda: False)
        with closing(open_legacy(result.path)) as db:
            assert db.execute("SELECT revision FROM configuration").fetchone()[0] == 31
        assert result.sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()
        assert snapshot_time(result.path, "ignored") == parse_instant(AT)
        assert all(
            path.with_name(path.name + suffix).read_bytes() == payload for suffix, payload in before.items()
        )


def test_running_mutex_and_existing_snapshot_refuse_without_writing(tmp_path: Path) -> None:
    path = copy_legacy(tmp_path)
    destination = tmp_path / "snapshot.sqlite3"
    assert legacy_running(detector=lambda: True)
    with pytest.raises(DomainError) as error:
        snapshot(path, destination, detector=lambda: True)
    assert error.value.code == "LEGACY_RUNNING" and not destination.exists()
    destination.write_bytes(b"keep")
    with pytest.raises(DomainError):
        snapshot(path, destination, detector=lambda: False)
    assert destination.read_bytes() == b"keep"


def test_snapshot_digest_changes_are_rejected(tmp_path: Path) -> None:
    path = copy_legacy(tmp_path)
    result = snapshot(path, tmp_path / "snapshot.sqlite3", at=parse_instant(AT), detector=lambda: False)
    with closing(sqlite3.connect(result.path)) as db:
        db.execute("UPDATE configuration SET revision=0")
        db.commit()
    with pytest.raises(DomainError) as error:
        snapshot_time(result.path, AT)
    assert error.value.code == "MIGRATION_SNAPSHOT_INVALID"
