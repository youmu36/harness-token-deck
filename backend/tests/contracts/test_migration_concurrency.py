"""Exclusive database replacement drains calls and publishes refresh events after reopening."""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from htd.bootstrap import Application
from htd.ipc.dto.base import EmptyRequest, StrictModel
from htd.ipc.router import Router

from backend.tests.migration.helpers import copy_legacy
from backend.tests.migration.test_wizard import finished, legacy_off, run


def test_exclusive_command_drains_readers_and_releases_gate_after_failure() -> None:
    reading, release_read, replacing, release_replace, signalled = (Event() for _ in range(5))
    with Router() as router, ThreadPoolExecutor(max_workers=2) as calls:

        @router.register("test.read", request=EmptyRequest, response=StrictModel, kind="read")
        def read(request: EmptyRequest) -> StrictModel:
            reading.set()
            assert release_read.wait(5)
            return StrictModel()

        @router.register(
            "test.replace", request=EmptyRequest, response=StrictModel, kind="write", exclusive=True
        )
        def replace(request: EmptyRequest) -> StrictModel:
            replacing.set()
            assert release_replace.wait(5)
            raise ValueError("replace failed")

        @router.register("test.ping", request=EmptyRequest, response=StrictModel, kind="read")
        def ping(request: EmptyRequest) -> StrictModel:
            signalled.set()
            return StrictModel()

        try:
            read_call = calls.submit(router.call, "test.read", {})
            assert reading.wait(5)
            replace_call = calls.submit(router.call, "test.replace", {})
            deadline = time.monotonic() + 5
            while router.call("test.ping", {})["ok"]:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            signalled.clear()
            assert not replacing.is_set()
            assert router.call("test.ping", {})["ok"] is False
            assert not signalled.is_set()
            release_read.set()
            assert read_call.result(timeout=5)["ok"]
            assert replacing.wait(5)
            release_replace.set()
            assert replace_call.result(timeout=5)["ok"] is False
            assert router.call("test.ping", {})["ok"]
        finally:
            release_read.set()
            release_replace.set()


def test_activation_waits_for_other_tasks_and_event_queries_see_new_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("htd.migration.legacy.reader.legacy_running", legacy_off)
    monkeypatch.setattr("htd.migration.engine.legacy_running", lambda: False)
    with Application(tmp_path / "data", legacy_path=copy_legacy(tmp_path), start_scheduler=False) as app:
        started = run(app)
        assert finished(app, started).result is not None
        pending = app.task_pool.reserve()
        activation = {"migrationId": started.migrationId, "operationId": str(uuid4())}
        result = app.bridge.call("migration.activate", activation)
        assert result["ok"] is False and isinstance(result["error"], dict)
        assert result["error"]["code"] == "MIGRATION_TARGET_BUSY"
        app.task_pool.discard(pending)
        seen: list[object] = []

        def refreshed(event: object) -> None:
            if type(event).__name__ == "UsageChanged":
                seen.append(app.bridge.call("preferences.get", {}))

        app.bus.subscribe(refreshed)
        assert app.bridge.call("migration.activate", activation)["ok"]
        assert len(seen) == 1 and isinstance(seen[0], dict) and seen[0]["ok"] is True
