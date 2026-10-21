"""Watcher root selection, failure recovery, and callback isolation."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from htd.adapters.collectors.discovery import source_key
from htd.adapters.watch.watcher import WakeHandler, Watcher, watch_roots
from htd.application.ports import SourceRecord
from htd.domain.usage.model import Harness


def source(harness: Harness, path: Path) -> SourceRecord:
    return SourceRecord(harness, str(path), None, None, "pending", ())


class FakeObserver:
    def __init__(self, *, fail: str = "") -> None:
        self.fail = fail
        self.alive = False
        self.scheduled: list[tuple[object, str, bool]] = []
        self.stopped = False
        self.joined = False

    def schedule(self, event_handler: object, path: str, *, recursive: bool) -> object:
        if self.fail == "schedule":
            raise OSError("injected scheduling failure")
        self.scheduled.append((event_handler, path, recursive))
        return object()

    def unschedule_all(self) -> None:
        self.scheduled.clear()

    def start(self) -> None:
        if self.fail == "start":
            raise OSError("injected startup failure")
        self.alive = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float) -> None:
        assert timeout <= 1
        self.alive = False
        self.joined = True

    def is_alive(self) -> bool:
        return self.alive


def test_sessions_archives_dsh_and_zcode_directory_roots(tmp_path: Path) -> None:
    codex, archive, dsh, db = (
        tmp_path / "codex" / "sessions",
        tmp_path / "codex" / "archived_sessions",
        tmp_path / "dsh" / "sessions",
        tmp_path / "zcode" / "db.sqlite",
    )
    for directory in (codex, archive, dsh, db.parent):
        directory.mkdir(parents=True)
    sources = (source(Harness.CODEX, codex), source(Harness.DSH, dsh), source(Harness.ZCODE, db))
    assert watch_roots(sources) == tuple(sorted(source_key(p) for p in (codex, archive, dsh, db.parent)))


def test_absent_archive_and_source_use_existing_parent(tmp_path: Path) -> None:
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    assert watch_roots((source(Harness.CODEX, sessions),)) == (source_key(sessions.parent),)
    assert watch_roots((source(Harness.DSH, tmp_path / "missing" / "sessions"),)) == (source_key(tmp_path),)


def test_symlink_roots_are_excluded(tmp_path: Path) -> None:
    actual, link = tmp_path / "actual", tmp_path / "link"
    actual.mkdir()
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink privileges unavailable")
    assert watch_roots((source(Harness.DSH, link),)) == ()


def test_callback_only_wakes_for_changes() -> None:
    wakeups: list[str] = []
    handler = WakeHandler(lambda: wakeups.append("wake"))
    for event_type in ("created", "modified", "moved", "deleted", "opened", "closed_no_write", "closed"):
        handler.dispatch(SimpleNamespace(event_type=event_type))
    assert wakeups == ["wake"] * 4


def test_database_wal_changes_forward_paths_and_unrelated_files_do_not_wake(tmp_path: Path) -> None:
    wakeups: list[str] = []
    changes: list[str] = []
    db = tmp_path / "db.sqlite"
    handler = WakeHandler(lambda: wakeups.append("wake"), (db,), changes.append)
    handler.dispatch(SimpleNamespace(event_type="modified", src_path=str(db) + "-wal", is_directory=False))
    assert changes == [str(db) + "-wal"] and wakeups == ["wake"]
    handler = WakeHandler(lambda: wakeups.append("wake"), (tmp_path,), changes.append)
    handler.dispatch(
        SimpleNamespace(event_type="modified", src_path=str(tmp_path / "notes.txt"), is_directory=False)
    )
    assert wakeups == ["wake"]


def test_parent_watch_filters_unrelated_files_and_accepts_moves(tmp_path: Path) -> None:
    wakeups: list[str] = []
    target = tmp_path / "missing" / "sessions"
    handler = WakeHandler(lambda: wakeups.append("wake"), (Path(source_key(target)),))
    handler.dispatch(SimpleNamespace(event_type="modified", src_path=str(tmp_path / "ledger.sqlite")))
    handler.dispatch(SimpleNamespace(event_type="created", src_path=str(target.parent)))
    handler.dispatch(
        SimpleNamespace(
            event_type="moved", src_path=str(tmp_path / "staged.jsonl"), dest_path=str(target / "one.jsonl")
        )
    )
    assert wakeups == ["wake", "wake"]


def test_changed_missing_root_reconfigures_filter_in_same_parent(tmp_path: Path) -> None:
    observer = FakeObserver()
    watcher = Watcher(factory=lambda: observer)
    first, second = tmp_path / "missing1", tmp_path / "missing2"
    calls: list[str] = []
    assert watcher.refresh((source(Harness.DSH, first),), lambda: calls.append("wake"))
    assert watcher.refresh((source(Harness.DSH, second),), lambda: calls.append("wake"))
    handler = observer.scheduled[0][0]
    assert isinstance(handler, WakeHandler)
    handler.dispatch(SimpleNamespace(event_type="created", src_path=str(first)))
    handler.dispatch(SimpleNamespace(event_type="created", src_path=str(second)))
    assert calls == ["wake"]
    watcher.close()


def test_refresh_reuses_observer_and_updates_changed_paths(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    observer = FakeObserver()
    watcher = Watcher(factory=lambda: observer)
    assert watcher.refresh((source(Harness.DSH, first),), lambda: None)
    assert watcher.refresh((source(Harness.DSH, first),), lambda: None)
    assert len(observer.scheduled) == 1 and observer.scheduled[0][2]
    assert watcher.refresh((source(Harness.DSH, second),), lambda: None)
    assert [item[1] for item in observer.scheduled] == [source_key(second)]
    watcher.close()
    watcher.close()
    assert observer.stopped and observer.joined


@pytest.mark.parametrize("failure", ["schedule", "start", "missing"])
def test_watchdog_failure_returns_polling_and_recovers(tmp_path: Path, failure: str) -> None:
    observer = FakeObserver(fail=failure)
    missing = failure == "missing"

    def factory() -> FakeObserver:
        if missing:
            raise ModuleNotFoundError("watchdog unavailable")
        return observer

    watcher = Watcher(factory=factory)
    assert not watcher.refresh((source(Harness.DSH, tmp_path),), lambda: None)
    if failure != "missing":
        assert observer.stopped
    missing = False
    observer.fail = ""
    assert watcher.refresh((source(Harness.DSH, tmp_path),), lambda: None)
    watcher.close()


def test_dead_observer_is_replaced(tmp_path: Path) -> None:
    first, second = FakeObserver(), FakeObserver()
    observers = iter((first, second))
    watcher = Watcher(factory=lambda: next(observers))
    records = (source(Harness.ZCODE, tmp_path / "db.sqlite"),)
    assert watcher.refresh(records, lambda: None)
    first.alive = False
    assert watcher.refresh(records, lambda: None)
    assert first.stopped and second.alive
    watcher.close()
