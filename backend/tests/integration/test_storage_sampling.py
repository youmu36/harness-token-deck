"""Disk samples are lazy, bounded, owned-only and refreshed after explicit maintenance."""

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from htd.adapters.data_directory import MARKER
from htd.adapters.sqlite import storage_size
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.manual_maintenance_store import SqliteManualMaintenanceStore
from htd.adapters.sqlite.writer import WriteExecutor
from htd.domain.common.time import Instant, parse_instant
from htd.ipc.dto.maintenance import MaintenanceStatus, StorageFootprint

AT = parse_instant("2026-10-25T10:00:00Z")


def sampled(store: SqliteManualMaintenanceStore) -> StorageFootprint:
    result = MaintenanceStatus.model_validate(store.status(at=AT, app_version="0.5.0"))
    assert result.storage is not None
    return result.storage


def test_disk_summary_counts_only_owned_data_and_reuses_a_fifteen_minute_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, size in (
        ("backups/a.sqlite3", 7),
        ("migration/run/a.sqlite3", 11),
        ("logs/log.txt", 5),
        ("webview/Default/Cache/item", 3),
        (MARKER, 2),
        (".htd-directory-move.json", 1),
        ("unrelated.bin", 10000),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    calls: list[Instant] = []
    now = [0.0]
    original = storage_size.footprint

    def footprint(db: sqlite3.Connection, *, at: Instant) -> dict[str, object]:
        calls.append(at)
        return original(db, at=at)

    monkeypatch.setattr(storage_size, "footprint", footprint)
    monkeypatch.setattr(storage_size, "monotonic", lambda: now[0])
    with WriteExecutor(tmp_path / "htd.sqlite3") as writer, ReadPool(writer.path) as readers:
        store = SqliteManualMaintenanceStore(writer, readers)
        first = sampled(store)
        assert first.totalBytes is not None
        assert first.backupBytes == 7
        assert first.totalBytes == first.mainFileBytes + first.walBytes + first.shmBytes + 29
        (tmp_path / "webview/new-cache").write_bytes(b"x" * 17)
        now[0] = 899
        assert sampled(store) == first and len(calls) == 1
        now[0] = 900
        updated = sampled(store)
        assert updated.totalBytes == first.totalBytes + 17
        assert len(calls) == 2


def test_completed_backup_invalidates_the_disk_sample(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "htd.sqlite3") as writer, ReadPool(writer.path) as readers:
        store = SqliteManualMaintenanceStore(writer, readers)
        assert sampled(store).backupBytes == 0
        result = store.file_action("maintenance.backup", at=AT, check_cancelled=lambda: None)
        assert result["path"]
        assert sampled(store).backupBytes > 0


def test_owned_tree_sampler_does_not_follow_links_or_junctions(tmp_path: Path) -> None:
    outside, link = tmp_path / "outside", tmp_path / "webview"
    outside.mkdir()
    (outside / "large").write_bytes(b"x" * 10000)
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    assert storage_size.tree_bytes(link) == 0
