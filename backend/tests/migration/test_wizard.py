"""Run application migration commands against synthetic old databases only."""

import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from htd.adapters.sqlite.connection import open_connection
from htd.application.ports import TaskContext
from htd.bootstrap import Application
from htd.domain.common.errors import DomainError
from htd.domain.common.testing import FakeClock
from htd.ipc.dto.migration import MigrationActivateResponse, MigrationDetectResponse, MigrationRunResponse
from htd.ipc.dto.tasks import TaskResponse
from htd.migration.legacy import reader

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.management_helpers import vendor

from .helpers import copy_legacy


def legacy_on(**_: object) -> bool:
    return True


def legacy_off(**_: object) -> bool:
    return False


@pytest.fixture(autouse=True)
def stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    def running(*, detector: Callable[[], bool] | None = None) -> bool:
        return detector() if detector else False

    monkeypatch.setattr(reader, "legacy_running", running)
    monkeypatch.setattr("htd.migration.engine.legacy_running", lambda: False)


def run(app: Application, operation_id: str | None = None) -> MigrationRunResponse:
    return invoke(
        app,
        "migration.run",
        {"operationId": operation_id or str(uuid4()), "dryRun": True},
        MigrationRunResponse,
    )


def finished(app: Application, started: MigrationRunResponse) -> TaskResponse:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = invoke(app, "tasks.get", {"taskId": started.taskId}, TaskResponse)
        if result.status not in ("pending", "running"):
            return result
        time.sleep(0.01)
    raise AssertionError("Migration task did not finish")


def activate(app: Application, started: MigrationRunResponse, operation_id: str) -> MigrationActivateResponse:
    return invoke(
        app,
        "migration.activate",
        {"migrationId": started.migrationId, "operationId": operation_id},
        MigrationActivateResponse,
    )


def test_detect_run_activate_replay_restart_and_open_connections(tmp_path: Path) -> None:
    source = copy_legacy(tmp_path)
    before = source.read_bytes()
    clock = FakeClock()
    with Application(tmp_path / "data", clock=clock, legacy_path=source, start_scheduler=False) as app:
        detected = invoke(app, "migration.detect", {}, MigrationDetectResponse)
        assert detected.canMigrate and detected.targetEmpty and not detected.legacyRunning
        run_id = str(uuid4())
        started = run(app, run_id)
        assert run(app, run_id) == started
        result = finished(app, started)
        assert result.status == "completed" and result.result is not None and result.result["ok"] is True
        assert Path(started.reportPath).is_file()
        assert app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        revisions = app.revisions()
        activation_id = str(uuid4())
        activated = activate(app, started, activation_id)
        assert run(app, run_id) == started
        assert activate(app, started, activation_id) == activated
        assert app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 11
        assert all(app.revisions()[scope] > revision for scope, revision in revisions.items())
        for command in ("preferences.get", "wallet.list", "pricing.catalog"):
            assert app.bridge.call(command, {})["ok"]
        assert not invoke(app, "migration.detect", {}, MigrationDetectResponse).targetEmpty
        assert source.read_bytes() == before
    with Application(tmp_path / "data", clock=clock, legacy_path=source, start_scheduler=False) as restarted:
        assert activate(restarted, started, activation_id) == activated
        reply = restarted.bridge.call(
            "migration.activate", {"migrationId": str(uuid4()), "operationId": activation_id}
        )
        assert (
            reply["ok"] is False
            and isinstance(reply["error"], dict)
            and reply["error"]["code"] == "OPERATION_ID_REUSED"
        )


def test_custom_source_is_bound_to_receipt_snapshot_and_activation_after_restart(tmp_path: Path) -> None:
    folder = tmp_path / "用户备份"
    folder.mkdir()
    source = copy_legacy(folder)
    original = source.read_bytes()
    default = tmp_path / "not-the-selected-database.sqlite3"
    parameters = {"sourcePath": str(source), "dryRun": True, "operationId": str(uuid4())}
    with Application(tmp_path / "data", legacy_path=default, start_scheduler=False) as app:
        assert not invoke(app, "migration.detect", {}, MigrationDetectResponse).detected
        selected = invoke(app, "migration.detect", {"sourcePath": str(source)}, MigrationDetectResponse)
        assert selected.canMigrate and selected.sourcePath == str(source)
        started = invoke(app, "migration.run", parameters, MigrationRunResponse)
        assert invoke(app, "migration.run", parameters, MigrationRunResponse) == started
        changed = app.bridge.call("migration.run", {**parameters, "sourcePath": str(default)})
        assert not changed["ok"] and isinstance(changed["error"], dict)
        assert changed["error"]["code"] == "OPERATION_ID_REUSED"
        result = finished(app, started)
        assert result.result is not None and result.result["ok"] is True
        assert result.result["sourcePath"] == str(source)
        assert not invoke(app, "migration.detect", {}, MigrationDetectResponse).detected
    with Application(tmp_path / "data", legacy_path=default, start_scheduler=False) as restarted:
        activated = activate(restarted, started, str(uuid4()))
        assert activated.sourcePath == str(source)
        assert restarted.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 11
    assert source.read_bytes() == original


def test_custom_source_missing_relative_and_invalid_database_keep_target_unchanged(tmp_path: Path) -> None:
    invalid = tmp_path / "bad.sqlite3"
    invalid.write_bytes(b"not a database")
    with Application(tmp_path / "data", start_scheduler=False) as app:
        missing = invoke(
            app, "migration.detect", {"sourcePath": str(tmp_path / "missing")}, MigrationDetectResponse
        )
        assert not missing.detected and not missing.canMigrate
        relative = app.bridge.call("migration.detect", {"sourcePath": "relative.sqlite3"})
        assert not relative["ok"] and isinstance(relative["error"], dict)
        assert relative["error"]["code"] == "MIGRATION_SOURCE_PATH"
        started = invoke(
            app,
            "migration.run",
            {"dryRun": True, "sourcePath": str(invalid), "operationId": str(uuid4())},
            MigrationRunResponse,
        )
        result = finished(app, started)
        assert result.result is not None and result.result["ok"] is False
        assert Path(started.reportPath).is_file()
        assert app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        assert invalid.read_bytes() == b"not a database"


@pytest.mark.parametrize("cause", ["occupied", "legacy", "tampered", "rename"])
def test_failed_activation_preserves_active_database_and_reopens_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    source = copy_legacy(tmp_path)
    with Application(tmp_path / "data", legacy_path=source, start_scheduler=False) as app:
        started = run(app)
        assert finished(app, started).result is not None
        staging = Path(started.reportPath).with_name("staging.sqlite3")
        expected = {
            "occupied": "MIGRATION_TARGET_NOT_EMPTY",
            "legacy": "LEGACY_RUNNING",
            "tampered": "MIGRATION_VERIFICATION_FAILED",
            "rename": "INTERNAL_ERROR",
        }[cause]
        if cause == "occupied":
            vendor(app, initial="1")
        elif cause == "legacy":
            monkeypatch.setattr(reader, "legacy_running", legacy_on)
        elif cause == "tampered":
            db = open_connection(staging)
            try:
                db.execute("UPDATE ledger SET pico='1'")
            finally:
                db.close()
        else:

            def fail(*_: object) -> None:
                raise OSError("rename failed")

            monkeypatch.setattr("htd.migration.engine.activate", fail)
        reply = app.bridge.call(
            "migration.activate", {"migrationId": started.migrationId, "operationId": str(uuid4())}
        )
        assert (
            reply["ok"] is False and isinstance(reply["error"], dict) and reply["error"]["code"] == expected
        )
        assert staging.is_file() and Path(started.reportPath).is_file()
        assert app.bridge.call("preferences.get", {})["ok"]
        assert app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_failure_report_and_cancelled_task_cannot_activate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_legacy(tmp_path)
    with Application(tmp_path / "data", legacy_path=source, start_scheduler=False) as app:
        monkeypatch.setattr(reader, "legacy_running", legacy_on)
        started = run(app)
        result = finished(app, started)
        assert result.result is not None and result.result["ok"] is False
        assert result.result["reportPath"] == started.reportPath
        assert "LEGACY_RUNNING" in Path(started.reportPath).read_text(encoding="utf-8")
        monkeypatch.setattr(reader, "legacy_running", legacy_off)
        entered, release = Event(), Event()
        original = app.migration.engine.run

        def delayed(
            migration_id: str, context: TaskContext, source_path: str | None = None
        ) -> dict[str, object]:
            entered.set()
            assert release.wait(5)
            return original(migration_id, context, source_path)

        monkeypatch.setattr(app.migration.engine, "run", delayed)
        cancelled = run(app)
        assert entered.wait(5)
        invoke(app, "tasks.cancel", {"taskId": cancelled.taskId, "operationId": str(uuid4())}, TaskResponse)
        release.set()
        assert finished(app, cancelled).status == "cancelled"
        reply = app.bridge.call(
            "migration.activate", {"migrationId": cancelled.migrationId, "operationId": str(uuid4())}
        )
        assert reply["ok"] is False


def test_legacy_detection_keeps_collection_running_and_activation_resumes_scheduler(tmp_path: Path) -> None:
    source = copy_legacy(tmp_path, "v022")
    with Application(tmp_path / "data", legacy_path=source) as app:
        assert app.scheduler.status().state != "paused"
        assert app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        started = run(app)
        assert finished(app, started).result is not None
        activate(app, started, str(uuid4()))
        reply = app.bridge.call("collection.scanNow", {"operationId": str(uuid4())})
        assert reply["ok"], reply


def test_activation_preserves_requests_collected_while_the_legacy_report_is_open(tmp_path: Path) -> None:
    from htd.adapters.sqlite.ingest_repo import IngestRepository
    from htd.adapters.sqlite.key_codec import key_bytes
    from htd.domain.usage.model import Harness

    from backend.tests.integration.collection_helpers import usage

    source = copy_legacy(tmp_path, "v022")
    with Application(tmp_path / "data", legacy_path=source, start_scheduler=False) as app:
        started = run(app)
        assert finished(app, started).result is not None
        at = app.clock.now_utc()
        event = usage(999999, at=at)
        root = next(s.path for s in app.store.sources().sources if s.harness == Harness.CODEX)
        app.writer.execute(
            lambda w: IngestRepository(w.connection).commit_batch(
                w, Harness.CODEX, root, root + "/pending.jsonl", {}, (event,), at=at
            )
        )
        activate(app, started, str(uuid4()))
        with app.readers.connection() as db:
            assert db.execute(
                "SELECT 1 FROM identity WHERE harness='codex' AND dedupe_key=?",
                (key_bytes(event.dedupe_key),),
            ).fetchone()
            assert db.execute(
                "SELECT 1 FROM inbox WHERE harness='codex' AND dedupe_key=?", (key_bytes(event.dedupe_key),)
            ).fetchone()
            assert (
                db.execute("SELECT value FROM meta WHERE key='legacy_pending_retained'").fetchone()[0] == "1"
            )


def test_cancellation_after_summary_write_removes_activation_qualification(tmp_path: Path) -> None:
    class CancelAtCompletion:
        def check_cancelled(self) -> None:
            pass

        def progress(self, completed: int, total: int) -> None:
            if completed == total:
                raise DomainError("TASK_CANCELLED", "任务已取消")

    with Application(tmp_path / "data", legacy_path=copy_legacy(tmp_path), start_scheduler=False) as app:
        migration_id = str(uuid4())
        with pytest.raises(DomainError, match="任务已取消"):
            app.migration.engine.run(migration_id, CancelAtCompletion())
        directory = Path(app.migration.engine.report_path(migration_id)).parent
        assert not (directory / "result.json").exists()
        assert "失败，禁止激活" in (directory / "report.md").read_text(encoding="utf-8")
