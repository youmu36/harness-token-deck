"""Serialize prioritized transactions and publish their events only after successful commits."""

import logging
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from queue import Empty, PriorityQueue
from threading import Lock, Thread, current_thread
from types import TracebackType
from typing import cast
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.migrate import migrate
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant

USER = 0
BILLING = 10
MAINTENANCE = 20
_LOG = logging.getLogger(__name__)


class UnitOfWork:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.events: list[object] = []
        self._touched: set[str] = set()
        self.wallet_flows: dict[
            tuple[str, str, str, str], tuple[str, str, str, int, int, int, str, int, int]
        ] = {}
        self.wallet_received: dict[str, str] = {}
        self.wallet_zone: ZoneInfo | None = None
        self.wallet_estimates: dict[tuple[str, str], int] = {}

    def revision(self, scope: str) -> int:
        row = (
            self.connection.execute("SELECT value FROM calibration_revision WHERE id=1").fetchone()
            if scope == "cost"
            else self.connection.execute("SELECT value FROM revision WHERE scope=?", (scope,)).fetchone()
        )
        if row is None:
            raise ValueError(f"Unknown revision scope: {scope}")
        return int(row[0])

    def touch(self, scope: str) -> int:
        if scope not in self._touched:
            self.revision(scope)
            if scope == "cost":
                self.connection.execute("UPDATE calibration_revision SET value=value+1 WHERE id=1")
            else:
                self.connection.execute("UPDATE revision SET value=value+1 WHERE scope=?", (scope,))
                if scope == "usage":
                    self.connection.execute(
                        "INSERT INTO runtime_state SELECT 'session_writer_revision',CAST(value AS TEXT) "
                        "FROM revision WHERE scope='usage' ON CONFLICT(key) "
                        "DO UPDATE SET value=excluded.value"
                    )
                    self.connection.execute(
                        "INSERT INTO runtime_state SELECT 'session_recalc_cursor',"
                        "CAST(COALESCE(MAX(rowid),0) AS TEXT) FROM recalc_item "
                        "HAVING NOT EXISTS(SELECT 1 FROM runtime_state WHERE key='session_recalc_until') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                    )
            self._touched.add(scope)
        return self.revision(scope)

    def emit(self, event: object) -> None:
        self.events.append(event)


@dataclass(order=True)
class _Task:
    priority: int
    sequence: int
    function: Callable[[UnitOfWork], object] | None = field(compare=False)
    future: Future[object] = field(compare=False)
    transactional: bool = field(default=True, compare=False)


class WriteExecutor:
    def __init__(self, path: Path, *, publish: Callable[[object], None] | None = None) -> None:
        self.path = path
        self._publish = publish
        self._queue: PriorityQueue[_Task] = PriorityQueue()
        self._counter = count()
        self._lock = Lock()
        self._closed = False
        self._ready: Future[None] = Future()
        self._thread = Thread(target=self._run, name="htd-writer", daemon=True)
        self._thread.start()
        self._ready.result()

    def submit[T](self, function: Callable[[UnitOfWork], T], priority: int = USER) -> Future[T]:
        return self._submit(function, priority, transactional=True)

    def submit_idle[T](self, function: Callable[[UnitOfWork], T]) -> Future[T]:
        """Run exclusive SQLite maintenance on the writer thread outside a transaction."""
        return self._submit(function, MAINTENANCE, transactional=False)

    def _submit[T](
        self, function: Callable[[UnitOfWork], T], priority: int, *, transactional: bool
    ) -> Future[T]:
        if type(priority) is not int or not 0 <= priority <= 2**31:
            raise ValueError("Priority must be an integer between 0 and 2**31")
        with self._lock:
            if self._closed:
                raise RuntimeError("Write executor is closed")
            future: Future[T] = Future()
            self._queue.put(
                _Task(priority, next(self._counter), function, cast(Future[object], future), transactional)
            )
            return future

    def execute[T](
        self, function: Callable[[UnitOfWork], T], *, priority: int = USER, timeout: float = 30
    ) -> T:
        if current_thread() is self._thread:
            raise RuntimeError("Cannot wait for a nested write task")
        future = self.submit(function, priority)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as error:
            if future.done():
                return future.result()
            future.cancel()
            raise DomainError("WRITE_TIMEOUT", "写入等待超时，请查询操作结果后重试") from error

    def _run(self) -> None:
        try:
            migrate(self.path)
            connection = open_connection(self.path)
            # Imported here because repository transaction guards depend on this executor.
            from htd.adapters.sqlite.wallet_store import WalletStore

            connection.execute("BEGIN IMMEDIATE")
            try:
                WalletStore(connection).validate_if_due(
                    UnitOfWork(connection), at=Instant(datetime.now(UTC)), force=True
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                connection.close()
                raise
        except BaseException as error:
            self._ready.set_exception(error)
            return
        self._ready.set_result(None)
        last_write = last_checkpoint = time.monotonic()
        try:
            while True:
                try:
                    task = self._queue.get(timeout=30)
                except Empty:
                    now = time.monotonic()
                    if now - last_checkpoint >= 300:
                        from htd.adapters.sqlite.maintenance_files import idle_checkpoint

                        try:
                            idle_checkpoint(connection, idle_seconds=now - last_write)
                        except sqlite3.Error:
                            _LOG.debug("空闲 WAL 回写暂不可用", exc_info=True)
                        last_checkpoint = now
                    continue
                try:
                    if task.function is None:
                        return
                    if not task.future.set_running_or_notify_cancel():
                        continue
                    if self._execute_task(connection, task):
                        last_write = time.monotonic()
                finally:
                    self._queue.task_done()
                    # A waiting worker must not own the last task's closure or future.
                    del task
        finally:
            connection.close()

    def _execute_task(self, connection: sqlite3.Connection, task: _Task) -> bool:
        """Per-task locals expire before the long-lived worker waits for more work."""
        assert task.function is not None
        work = UnitOfWork(connection)
        try:
            if task.transactional:
                connection.execute("BEGIN IMMEDIATE")
            result = task.function(work)
            if work.wallet_flows or work.wallet_estimates:
                from htd.adapters.sqlite.wallet_flows import commit_staged

                commit_staged(work)
            connection.commit()
        except BaseException as error:
            connection.rollback()
            task.future.set_exception(error)
            return False
        if self._publish is not None:
            for event in work.events:
                try:
                    self._publish(event)
                except Exception:
                    _LOG.exception("Post-commit event delivery failed")
        task.future.set_result(result)
        return True

    def close(self, timeout: float = 10) -> None:
        if current_thread() is self._thread:
            raise RuntimeError("Cannot join write executor from its own thread")
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(_Task(2**63, next(self._counter), None, Future()))
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise DomainError("WRITE_CLOSE_TIMEOUT", "写执行器仍在完成当前事务")

    def reopen(self) -> None:
        if self._thread.is_alive():
            raise RuntimeError("Close the writer before reopening")
        self.__init__(self.path, publish=self._publish)

    def __enter__(self) -> "WriteExecutor":
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
