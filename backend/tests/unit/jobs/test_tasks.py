"""Task cancellation observes the action boundary and preserves completed side effects."""

from threading import Event

from htd.application.events import InProcessEventBus
from htd.application.ports import TaskContext, TaskDone
from htd.domain.common.testing import FakeClock
from htd.jobs.tasks import TaskPool


def test_cancel_after_action_commit_preserves_successful_result() -> None:
    bus = InProcessEventBus()
    done = Event()
    bus.subscribe(lambda event: done.set() if isinstance(event, TaskDone) else None)
    pool = TaskPool(FakeClock(), bus)
    task_id = pool.reserve()

    def action(context: TaskContext) -> dict[str, object]:
        context.progress(0, 1)
        pool.cancel(task_id)
        # A finished, noninterruptible operation reports the result it actually produced.
        return {"committed": True}

    try:
        pool.start(task_id, action)
        assert done.wait(5)
        result = pool.get(task_id)
        assert result["status"] == "completed" and result["result"] == {"committed": True}
        assert result["completed"] == result["total"] == 1
    finally:
        pool.close()
