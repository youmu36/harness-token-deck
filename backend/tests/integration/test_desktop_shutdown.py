"""Shutdown waits for an in-flight collection transaction and drains queued writer work."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.window_state import WindowGeometry
from htd.application.ports import BilledUsage, UnitOfWork
from htd.bootstrap import Application
from htd.domain.common.time import Instant
from htd.domain.usage.model import Harness

from .collection_helpers import collect, usage
from .scan_helpers import seed_prices


def test_private_geometry_persists_without_changing_public_preferences(tmp_path: Path) -> None:
    geometry = WindowGeometry(-1200, 80, 1280, 820)
    with Application(tmp_path, start_scheduler=False) as app:
        before = app.bridge.call("preferences.get", {})
        app.window_state.save(geometry)
        assert app.window_state.load() == geometry
        assert app.bridge.call("preferences.get", {}) == before
    with Application(tmp_path, start_scheduler=False) as app:
        assert app.window_state.load() == geometry
        assert "desktopWindow" not in app.preferences.get().values


def test_shutdown_finishes_billing_transaction_and_drains_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = Application(tmp_path, start_scheduler=False)
    entered, release = threading.Event(), threading.Event()
    close_started = threading.Event()
    try:
        app.writer.execute(seed_prices)
        app.writer.execute(
            lambda w: SourcesRepository(w.connection).set_path(
                w,
                Harness.CODEX,
                str(tmp_path / "codex"),
                available=True,
            )
        )
        collect(app.writer, (usage(1),))
        append = app.scan.billing.store.append_consumption

        def hold(work: UnitOfWork, billed: BilledUsage, *, at: Instant) -> str | None:
            result = append(work, billed, at=at)
            entered.set()
            assert release.wait(2)
            return result

        monkeypatch.setattr(app.scan.billing.store, "append_consumption", hold)
        app.scheduler.start()
        assert entered.wait(2)
        close_scheduler = app.scheduler.close

        def closing(timeout: float = 10) -> None:
            close_started.set()
            close_scheduler(timeout)

        monkeypatch.setattr(app.scheduler, "close", closing)
        drained = app.writer.submit(
            lambda work: (
                work.connection.execute("INSERT INTO preference VALUES ('desktopWindow','invalid')").rowcount
            )
        )
        # Ledger insertion has executed but is not committed while shutdown begins.
        with ThreadPoolExecutor(max_workers=1) as pool:
            stopped = pool.submit(app.close)
            assert close_started.wait(2)
            assert not stopped.done()
            release.set()
            stopped.result(timeout=5)
        assert drained.result(timeout=2) == 1
        app.close()
        with sqlite3.connect(tmp_path / "htd.sqlite3") as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
            assert (
                db.execute("SELECT value_json FROM preference WHERE key='desktopWindow'").fetchone()[0]
                == "invalid"
            )
        assert app.bridge.call("preferences.get", {})["error"]["code"] == "APP_SHUTTING_DOWN"  # type: ignore[index]
    finally:
        release.set()
        app.close()


def test_corrupt_private_geometry_uses_default_without_breaking_preferences(tmp_path: Path) -> None:
    with Application(tmp_path, start_scheduler=False) as app:
        app.writer.execute(
            lambda work: work.connection.execute("INSERT INTO preference VALUES ('desktopWindow','invalid')")
        )
        assert app.window_state.load() is None
        assert app.bridge.call("preferences.get", {})["ok"] is True
