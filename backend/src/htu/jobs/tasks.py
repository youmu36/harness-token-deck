"""Bounded background pool, cooperative cancellation and thirty-minute result retention."""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Event, RLock
from uuid import uuid4

from htu.application.ports import EventBus, TaskContext, TaskDone, TaskProgress
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Clock

_LOG = logging.getLogger(__name__)


@dataclass
class _Task:
    id: str
    status: str = "pending"
    completed: int = 0
    total: int = 0
    result: dict[str, object] | None = None
    error: dict[str, object] | None = None
    finished: float | None = None
    cancelled: Event = field(default_factory=Event)


class _Context:
    def __init__(self, pool: "TaskPool", task: _Task) -> None:
        self.pool, self.task = pool, task

    def check_cancelled(self) -> None:
        if self.task.cancelled.is_set():
            raise DomainError("TASK_CANCELLED", "任务已取消")

    def progress(self, completed: int, total: int) -> None:
        self.check_cancelled()
        with self.pool.lock:
            self.task.completed, self.task.total = completed, total
        self.pool.bus.publish(TaskProgress(self.task.id, completed, total))


class TaskPool:
    def __init__(self, clock: Clock, bus: EventBus) -> None:
        self.clock, self.bus = clock, bus
        self.lock = RLock()
        self._tasks: dict[str, _Task] = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="htu-task")
        self._closed = False

    def _prune(self) -> None:
        now = self.clock.monotonic()
        self._tasks = {
            key: task
            for key, task in self._tasks.items()
            if task.finished is None or now - task.finished < 1800
        }

    def reserve(self) -> str:
        with self.lock:
            self._prune()
            if self._closed:
                raise DomainError("APP_SHUTTING_DOWN", "应用正在关闭")
            if len(self._tasks) >= 64:
                raise DomainError("SERVER_BUSY", "后台任务已满，请稍后重试", retryable=True)
            task_id = str(uuid4())
            self._tasks[task_id] = _Task(task_id)
            return task_id

    def discard(self, task_id: str) -> None:
        with self.lock:
            self._tasks.pop(task_id, None)

    def start(self, task_id: str, action: Callable[[TaskContext], dict[str, object]]) -> None:
        with self.lock:
            task = self._find(task_id)
            if task.status != "pending":
                return
            if self._closed:
                raise DomainError("APP_SHUTTING_DOWN", "应用正在关闭")
            task.status = "running"
            self._executor.submit(self._run, task, action)

    def _run(self, task: _Task, action: Callable[[TaskContext], dict[str, object]]) -> None:
        context = _Context(self, task)
        result: dict[str, object] | None = None
        error: dict[str, object] | None = None
        try:
            context.check_cancelled()
            result = action(context)
        except DomainError as failure:
            error = {"code": failure.code, "message": failure.message, "retryable": failure.retryable}
        except Exception:
            log_id = uuid4().hex
            _LOG.exception("Background task failure logId=%s", log_id)
            error = {
                "code": "INTERNAL_ERROR",
                "message": f"发生内部错误（日志编号 {log_id}）",  # noqa: RUF001
                "retryable": False,
                "logId": log_id,
            }
        with self.lock:
            task.result, task.error = result if error is None else None, error
            if error is None:
                task.completed = task.total
            task.status = (
                "completed"
                if error is None
                else "cancelled"
                if error["code"] == "TASK_CANCELLED"
                else "failed"
            )
            task.finished = self.clock.monotonic()
        self.bus.publish(TaskDone(task.id, error is None, task.result, error))

    def _find(self, task_id: str) -> _Task:
        self._prune()
        task = self._tasks.get(task_id)
        if task is None:
            raise DomainError("TASK_NOT_FOUND", "任务不存在或结果已过期", "task_id")
        return task

    def get(self, task_id: str) -> dict[str, object]:
        with self.lock:
            task = self._find(task_id)
            return deepcopy(
                {
                    "taskId": task.id,
                    "status": task.status,
                    "cancelRequested": task.cancelled.is_set(),
                    "completed": task.completed,
                    "total": task.total,
                    "result": task.result,
                    "error": task.error,
                }
            )

    def cancel(self, task_id: str) -> dict[str, object]:
        with self.lock:
            task = self._find(task_id)
            if task.finished is None:
                task.cancelled.set()
            return self.get(task_id)

    def close(self) -> None:
        with self.lock:
            self._closed = True
            for task in self._tasks.values():
                if task.finished is None:
                    task.cancelled.set()
        self._executor.shutdown(wait=True)

    def require_idle(self) -> None:
        with self.lock:
            if any(task.finished is None for task in self._tasks.values()):
                raise DomainError("MIGRATION_TARGET_BUSY", "请等待其他后台任务完成后再激活")
