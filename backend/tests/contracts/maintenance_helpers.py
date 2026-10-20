"""Synthetic ledger and task completion helpers for P4.6 contracts."""

from collections.abc import Iterable
from threading import Event
from uuid import uuid4

from htu.adapters.sqlite.history_repo import HistoryRepository
from htu.adapters.sqlite.writer import UnitOfWork
from htu.application.ports import TaskDone
from htu.bootstrap import Application
from htu.domain.usage.model import UsageEvent
from htu.ipc.dto.tasks import TaskResponse, TaskStartedResponse

from .command_helpers import invoke


def add_usage(app: Application, events: Iterable[UsageEvent]) -> None:
    roots = {s.harness: s.path for s in app.store.sources().sources}

    def save(work: UnitOfWork) -> None:
        HistoryRepository(work.connection).import_events(work, events, roots=roots, at=app.clock.now_utc())

    app.writer.execute(save)
    app.scan.billing.run()


def completed_task(app: Application, command: str, **parameters: object) -> TaskResponse:
    done = Event()
    unsubscribe = app.bus.subscribe(lambda e: done.set() if isinstance(e, TaskDone) else None)
    try:
        started = invoke(app, command, {"operationId": str(uuid4()), **parameters}, TaskStartedResponse)
        assert done.wait(5)
        result = invoke(app, "tasks.get", {"taskId": started.taskId}, TaskResponse)
        assert result.status == "completed", result
        return result
    finally:
        unsubscribe()
