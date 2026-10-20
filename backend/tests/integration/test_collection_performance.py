"""Bound repeated I/O while retaining notification, polling and log-reset correctness."""

import os
from contextlib import closing
from pathlib import Path
from typing import Any

import htu.adapters.collectors.codex_logs as logs_adapter
import htu.adapters.collectors.discovery as discovery_module
import pytest
from htu.adapters.collectors.codex_logs import CodexLogs
from htu.adapters.collectors.discovery import Discovery, source_key
from htu.adapters.sqlite.auto_maintenance_store import SqliteMaintenanceStore
from htu.adapters.sqlite.writer import WriteExecutor
from htu.application.collection.maintenance import AutoMaintenance
from htu.application.ports import UnitOfWork
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import parse_instant
from htu.domain.usage.model import Harness

from .collection_helpers import AT
from .test_codex_logs import END, START, add_log, create_logs


def test_notification_reuses_unchanged_files_and_periodic_scan_recovers_missed_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    for index in range(20):
        (root / f"{index}.jsonl").write_bytes(b"a\n")
    discovery = Discovery(environ={})
    discovery.prepare("startup")
    files = discovery.discover(Harness.CODEX, str(root)).paths
    original = {path: discovery.signature(Harness.CODEX, path) for path in files}
    stat_calls: list[str] = []
    scans: list[str] = []
    path_stat, scandir = Path.stat, os.scandir

    def counted_stat(path: Path, *args: Any, **kwargs: Any):
        stat_calls.append(str(path))
        return path_stat(path, *args, **kwargs)

    def counted_scan(path: Any):
        scans.append(str(path))
        return scandir(path)

    monkeypatch.setattr(Path, "stat", counted_stat)
    monkeypatch.setattr(discovery_module.os, "scandir", counted_scan)
    changed = root / "0.jsonl"
    changed.write_bytes(b"longer\n")
    discovery.invalidate(str(changed))
    discovery.prepare("notification")
    assert discovery.discover(Harness.CODEX, str(root)).paths == files
    current = {path: discovery.signature(Harness.CODEX, path) for path in files}
    assert current[source_key(changed)] != original[source_key(changed)]
    assert len([path for path in stat_calls if path.endswith(".jsonl")]) == 1
    # File notifications invalidate the parent listing, but other transcripts avoid stat.
    assert len(scans) == 1
    missing = root / "1.jsonl"
    missing.write_bytes(b"missed notification\n")
    stat_calls.clear()
    scans.clear()
    discovery.prepare("periodic")
    assert discovery.discover(Harness.CODEX, str(root)).paths == files
    current = {path: discovery.signature(Harness.CODEX, path) for path in files}
    assert current[source_key(missing)] != original[source_key(missing)]
    assert len([path for path in stat_calls if path.endswith(".jsonl")]) == 20
    assert scans == []


def test_incremental_discovery_tracks_new_nested_files_deletion_and_archive(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    discovery = Discovery(environ={})
    discovery.prepare("startup")
    assert discovery.discover(Harness.CODEX, str(root)).paths == ()
    nested = root / "2026" / "10"
    nested.mkdir(parents=True)
    path = nested / "new.jsonl"
    path.touch()
    discovery.invalidate(str(root / "2026"))
    discovery.prepare("notification")
    assert discovery.discover(Harness.CODEX, str(root)).paths == (source_key(path),)
    archive = root.parent / "archived_sessions"
    archive.mkdir()
    moved = archive / "archived.jsonl"
    path.rename(moved)
    discovery.invalidate(str(path))
    discovery.invalidate(str(moved))
    discovery.prepare("notification")
    assert discovery.discover(Harness.CODEX, str(root)).paths == (source_key(moved),)


def test_log_cache_shares_reads_only_queries_appended_ids_and_accepts_late_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    reader = CodexLogs()
    calls: list[object] = []
    original = logs_adapter.read_title_update

    def counted(*args: Any):
        calls.append(args[2])
        return original(*args)

    monkeypatch.setattr(logs_adapter, "read_title_update", counted)
    with closing(create_logs(root.parent / "logs_2.sqlite")) as db:
        add_log(db)
        add_log(db, END)
        assert sum(row.count for row in reader.read(str(root), START, END) or ()) == 1
        assert len(reader.read_titles(str(root), parse_instant("2026-09-01T00:00:00Z"), END) or ()) == 1
        assert len(calls) == 1 and calls[0] is None
        assert len(reader.read_titles(str(root), START, parse_instant("2026-09-03T00:00:00Z")) or ()) == 2
        assert len(calls) == 2 and calls[1] is not None
        add_log(db, START)
        assert len(reader.read_titles(str(root), START, END) or ()) == 2
        assert len(calls) == 3 and calls[2] is not None
        db.execute("DELETE FROM logs")
        db.commit()
        add_log(db, START)
        result = reader.read(str(root), START, END)
        assert result is not None and len(result) == 1 and result[0].count == 1


def test_maintenance_skips_unchanged_models_and_minute_but_rechecks_new_revision(tmp_path: Path) -> None:
    class CountingStore(SqliteMaintenanceStore):
        models_read = 0
        compactions = 0

        def observed_models(self, work: UnitOfWork):
            self.models_read += 1
            return super().observed_models(work)

        def compact_history(self, work: UnitOfWork, *, at: Any) -> int:
            self.compactions += 1
            return super().compact_history(work, at=at)

    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer:
        clock = FakeClock(AT)
        store = CountingStore(writer)
        maintenance = AutoMaintenance(store, clock)
        assert not maintenance.run(more=False).failed
        for _ in range(5):
            assert not maintenance.run(more=False).failed
        assert store.models_read == store.compactions == 1
        writer.execute(lambda work: work.touch("pricing"))
        maintenance.run(more=False)
        assert store.models_read == 1 and store.compactions == 1
        writer.execute(lambda work: work.touch("models"))
        maintenance.run(more=False)
        assert store.models_read == 2
        clock.advance(60_000)
        maintenance.run(more=False)
        assert store.compactions == 2


def test_large_title_snapshot_is_lossless_and_rebuilds_after_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from htu.adapters.sqlite.codex_title_cache import TitleLogBatch
    from htu.application.ports import TitleRequest
    from htu.domain.common.time import from_epoch_ms

    root = tmp_path / "sessions"
    root.mkdir()
    (tmp_path / "logs_2.sqlite").touch()
    requests = tuple(TitleRequest(f"{i:064x}", from_epoch_ms(1_788_220_800_000 + i)) for i in range(5000))
    calls: list[int] = []

    def update(*args: object) -> TitleLogBatch:
        calls.append(1)
        return TitleLogBatch(None, requests, False)

    monkeypatch.setattr(logs_adapter, "read_title_update", update)
    reader = CodexLogs()
    start, end = from_epoch_ms(1_788_220_800_000), from_epoch_ms(1_788_220_810_000)
    assert reader.read_titles(str(root), start, end) == requests
    assert reader.read_titles(str(root), start, end) == requests
    assert sum(row.count for row in reader.read(str(root), start, end) or ()) == 5000
    assert len(calls) == 1
    reader.release_cache()
    assert reader.read_titles(str(root), start, end) == requests and len(calls) == 2
