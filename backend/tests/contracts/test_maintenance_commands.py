"""Manual retention, file operations, revisions and partial-failure recovery through IPC."""

import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from htu.adapters.sqlite.history_repo import HistoryRepository
from htu.adapters.sqlite.key_codec import key_bytes
from htu.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htu.adapters.sqlite.writer import UnitOfWork
from htu.application.ports import PreferencesChanged, SourcesChanged, TaskDone, UsageChanged
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.statistics.model import LedgerRow
from htu.domain.usage.model import Harness
from htu.ipc.dto.base import Success
from htu.ipc.dto.maintenance import MaintenanceStatus, RetentionPreview, RetentionResponse
from htu.ipc.dto.preferences import PreferencesResponse
from htu.ipc.dto.tasks import TaskResponse

from backend.tests.integration.collection_helpers import usage

from .command_helpers import failure, invoke, seed_rows
from .maintenance_helpers import completed_task
from .management_helpers import group

AT = parse_instant("2026-10-01T00:00:00Z")


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Application]:
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as instance:
        yield instance


def seed(app: Application, count: int = 1) -> None:
    old = Instant(AT.value - timedelta(days=400))
    recent = Instant(AT.value - timedelta(minutes=5))
    seed_rows(
        app,
        [
            LedgerRow(Harness.CODEX, f"{i:064x}", "session", old, "Model", 1, 0, 0, 0, False, True)
            for i in range(1, count + 1)
        ]
        + [
            LedgerRow(Harness.CODEX, f"{count + 1:064x}", "session", recent, "Model", 1, 0, 0, 0, False, True)
        ],
    )
    roots = {s.harness: s.path for s in app.store.sources().sources}

    def insert(work: UnitOfWork) -> None:
        for i in range(1, count + 2):
            work.connection.execute(
                "INSERT INTO identity VALUES ('codex',?,?,1,?,'history')",
                (key_bytes(f"{i:064x}"), format_instant(old if i <= count else recent), format_instant(AT)),
            )
        HistoryRepository(work.connection).import_events(
            work, [usage(count + 2, at=recent)], roots=roots, at=AT
        )
        work.connection.execute("UPDATE inbox SET occurred_at=?", (format_instant(old),))
        work.connection.execute("UPDATE identity SET occurred_at=? WHERE billed=0", (format_instant(old),))

    app.writer.execute(insert)


def test_M05_M06_preview_and_cleanup_preserve_identity_and_prices(app: Application) -> None:
    group(app)
    seed(app)
    preview = invoke(app, "maintenance.previewRetention", {"retention": 360}, RetentionPreview)
    assert preview.ledgerCount == preview.inboxCount == 1 and preview.identityCount == 3
    status = invoke(app, "maintenance.status", {}, MaintenanceStatus)
    assert status.retention == "forever" and status.pendingExpired == 0 and status.databaseBytes > 0
    events: list[object] = []
    app.bus.subscribe(events.append)
    payload = {
        "retention": 90,
        "expectedRevision": app.revisions()["preferences"],
        "operationId": str(uuid4()),
    }
    result = invoke(app, "maintenance.setRetention", payload, RetentionResponse)
    assert result.complete and result.deletedLedger == result.expiredInbox == 1
    assert app.router.call("maintenance.setRetention", payload)["data"] == result.model_dump(mode="json")
    db = app.readers.get()
    assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM identity WHERE billed=1").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM price_group").fetchone()[0] == 1
    assert any(isinstance(e, PreferencesChanged) for e in events)
    assert any(isinstance(e, UsageChanged) for e in events)
    assert any(isinstance(e, SourcesChanged) for e in events)
    assert invoke(app, "maintenance.status", {}, MaintenanceStatus).lastRetentionAt == format_instant(AT)
    preferences = invoke(app, "preferences.get", {}, PreferencesResponse)
    changed = invoke(
        app,
        "preferences.update",
        {"patch": {"theme": "dark"}, "expectedRevision": preferences.revision, "operationId": str(uuid4())},
        PreferencesResponse,
    )
    assert changed.preferences.theme == "dark"
    assert invoke(app, "maintenance.status", {}, MaintenanceStatus).retention == 90


@pytest.mark.parametrize("table", ["preference", "operation"])
def test_setting_or_receipt_failure_does_not_delete(app: Application, table: str) -> None:
    seed(app)
    app.writer.execute(
        lambda work: work.connection.execute(
            f"CREATE TRIGGER injected BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'secret'); END"
        )
    )
    before = app.revisions()
    error = failure(
        app,
        "maintenance.setRetention",
        {"retention": 90, "expectedRevision": before["preferences"], "operationId": str(uuid4())},
    )
    assert error.code == "INTERNAL_ERROR" and "secret" not in error.message
    assert (
        app.revisions() == before
        and app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2
    )
    assert invoke(app, "maintenance.status", {}, MaintenanceStatus).retention == "forever"


def test_partial_cleanup_receipt_resumes_same_operation_and_restarts(
    app: Application, tmp_path: Path
) -> None:
    seed(app, 501)
    app.writer.execute(
        lambda work: work.connection.execute(
            "CREATE TRIGGER injected BEFORE DELETE ON ledger WHEN OLD.dedupe_key=unhex(printf('%064x',501)) "
            "BEGIN SELECT RAISE(ABORT,'secret'); END"
        )
    )
    payload = {"retention": 90, "expectedRevision": 0, "operationId": str(uuid4())}
    partial = invoke(app, "maintenance.setRetention", payload, RetentionResponse)
    assert not partial.complete and partial.errorCode == "RETENTION_FAILED"
    assert partial.deletedLedger == 500 and partial.expiredInbox == 1
    assert invoke(app, "maintenance.status", {}, MaintenanceStatus).retention == 90
    assert app.readers.get().execute("SELECT COUNT(*) FROM retention_run").fetchone()[0] == 0
    app.writer.execute(lambda work: work.connection.execute("DROP TRIGGER injected"))
    app.close()
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as restarted:
        result = invoke(restarted, "maintenance.setRetention", payload, RetentionResponse)
        assert result.complete and result.deletedLedger == 501 and result.expiredInbox == 1
        assert invoke(restarted, "maintenance.setRetention", payload, RetentionResponse) == result


@pytest.mark.parametrize("retention", [90, 180, 360, "forever"])
def test_retention_options_and_revision_conflict(app: Application, retention: int | str) -> None:
    seed(app)
    payload = {"retention": retention, "expectedRevision": 0, "operationId": str(uuid4())}
    result = invoke(app, "maintenance.setRetention", payload, RetentionResponse)
    assert result.complete and result.deletedLedger == (0 if retention == "forever" else 1)
    if retention != 90:
        assert (
            failure(app, "maintenance.setRetention", payload | {"retention": 90}).code
            == "OPERATION_ID_REUSED"
        )
    assert (
        failure(
            app,
            "maintenance.setRetention",
            {"retention": 90, "expectedRevision": 99, "operationId": str(uuid4())},
        ).code
        == "PREFERENCES_REVISION_CONFLICT"
    )


def test_backup_integrity_vacuum_and_replay(app: Application) -> None:
    group(app)
    backed = completed_task(app, "maintenance.backup")
    assert backed.result and isinstance(backed.result["path"], str)
    with sqlite3.connect(backed.result["path"]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM price_group").fetchone()[0] == 1
    result = completed_task(app, "maintenance.vacuum")
    assert result.completed == result.total == 1
    assert invoke(app, "maintenance.status", {}, MaintenanceStatus).lastVacuumAt == format_instant(AT)


def test_queued_file_task_can_cancel_before_side_effect(app: Application) -> None:
    entered, release, done = Event(), Event(), Event()

    def block(work: UnitOfWork) -> None:
        entered.set()
        assert release.wait(5)

    # Reserve without a receipt so the file action can queue behind the blocked writer.
    blocker = app.writer.submit(block)
    assert entered.wait(5)
    task_id = app.task_pool.reserve()
    app.bus.subscribe(lambda e: done.set() if isinstance(e, TaskDone) and e.task_id == task_id else None)
    app.task_pool.start(
        task_id,
        lambda context: app.maintenance.store.file_action(
            "maintenance.vacuum", at=AT, check_cancelled=context.check_cancelled
        ),
    )
    try:
        app.task_pool.cancel(task_id)
    finally:
        release.set()
    blocker.result(5)
    assert done.wait(5)
    assert invoke(app, "tasks.get", {"taskId": task_id}, TaskResponse).status == "cancelled"
    assert invoke(app, "maintenance.status", {}, MaintenanceStatus).lastVacuumAt is None


def test_batch_receipt_update_failure_rolls_back_expiration_and_resumes(app: Application) -> None:
    seed(app)
    app.writer.execute(
        lambda work: work.connection.execute(
            "CREATE TRIGGER injected BEFORE UPDATE ON operation BEGIN SELECT RAISE(ABORT,'secret'); END"
        )
    )
    payload = {"retention": 90, "expectedRevision": 0, "operationId": str(uuid4())}
    partial = invoke(app, "maintenance.setRetention", payload, RetentionResponse)
    assert not partial.complete and partial.deletedLedger == partial.expiredInbox == 0
    assert invoke(app, "maintenance.status", {}, MaintenanceStatus).retention == 90
    assert app.readers.get().execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
    assert app.readers.get().execute("SELECT COUNT(*) FROM identity WHERE billed=0").fetchone()[0] == 1
    app.writer.execute(lambda work: work.connection.execute("DROP TRIGGER injected"))
    assert invoke(app, "maintenance.setRetention", payload, RetentionResponse).complete


def test_current_retention_change_stops_old_plan_between_batches(app: Application) -> None:
    seed(app)
    scheduled = Event()

    def changed(event: object) -> None:
        if isinstance(event, SourcesChanged) and not scheduled.is_set():
            scheduled.set()
            app.writer.submit(
                lambda work: MaintenanceRepository(work.connection).save_retention(work, "forever")
            )

    app.bus.subscribe(changed)
    result = invoke(
        app,
        "maintenance.setRetention",
        {"retention": 90, "expectedRevision": 0, "operationId": str(uuid4())},
        RetentionResponse,
    )
    assert not result.complete and result.errorCode == "RETENTION_CHANGED"
    assert result.expiredInbox == 1 and result.deletedLedger == 0
    assert invoke(app, "maintenance.status", {}, MaintenanceStatus).retention == "forever"


def test_registered_p46_contracts_export_json_schema(app: Application) -> None:
    assert {"maintenance.backup", "maintenance.setRetention", "maintenance.previewRecalc"} <= set(
        app.router.commands
    )
    for name, command in app.router.commands.items():
        if name.startswith(("maintenance.", "history.")):
            assert command.request.model_json_schema()
            assert Success[command.response].model_json_schema()
