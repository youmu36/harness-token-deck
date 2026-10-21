"""Task query/cancellation use cases sharing the command receipt namespace."""

from collections.abc import Callable
from typing import cast

from htd.application.command import execute
from htd.application.ports import CommandStore, TaskContext, TaskControl, UnitOfWork
from htd.domain.common.time import Clock, Instant


def start_task(
    store: CommandStore,
    clock: Clock,
    tasks: TaskControl,
    command: str,
    parameters: dict[str, object],
    operation_id: str,
    action: Callable[[TaskContext], dict[str, object]],
) -> dict[str, object]:
    reserved: list[str] = []

    def reserve(work: UnitOfWork, at: Instant) -> dict[str, object]:
        task_id = tasks.reserve()
        reserved.append(task_id)
        return {"taskId": task_id}

    try:
        result = execute(store, clock, command, parameters, operation_id, reserve)
    except BaseException:
        for task_id in reserved:
            tasks.discard(task_id)
        raise
    if reserved:
        tasks.start(cast(str, result["taskId"]), action)
    return result


class Tasks:
    def __init__(self, tasks: TaskControl, store: CommandStore, clock: Clock) -> None:
        self.tasks, self.store, self.clock = tasks, store, clock

    def get(self, task_id: str) -> dict[str, object]:
        return self.tasks.get(task_id)

    def cancel(self, task_id: str, operation_id: str, *, command: str = "tasks.cancel") -> dict[str, object]:
        fresh: list[bool] = []

        def prepare(work: UnitOfWork, at: Instant) -> dict[str, object]:
            snapshot = self.tasks.get(task_id)
            if snapshot["status"] in ("pending", "running"):
                snapshot["cancelRequested"] = True
            fresh.append(True)
            return snapshot

        result = execute(self.store, self.clock, command, {"taskId": task_id}, operation_id, prepare)
        if fresh:
            self.tasks.cancel(task_id)
        return result
