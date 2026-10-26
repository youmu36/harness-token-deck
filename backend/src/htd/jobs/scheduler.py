"""Coordinate collection on one thread using injected time and condition waits."""

import logging
from collections.abc import Callable
from concurrent.futures import Future, InvalidStateError
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from threading import Condition, Event, Lock, Thread, current_thread
from typing import Literal, Protocol

from htd.application.collection.scan import ScanResult
from htd.application.ports import ScanTrigger, SchedulerStatus, SchedulerStore, SourceWatcher
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant

type Wait = Callable[[Condition, float], None]
_LOG = logging.getLogger(__name__)


class ScanRunner(Protocol):
    def run(self, *, should_stop: Callable[[], bool]) -> ScanResult: ...


class MaintenanceHook(Protocol):
    def run(self, *, more: bool, paused: bool, should_stop: Callable[[], bool]) -> object: ...


def condition_wait(condition: Condition, timeout: float) -> None:
    condition.wait(timeout)


@dataclass(frozen=True, slots=True)
class ScanCompletion:
    trigger: ScanTrigger
    revision: int
    completed_at: Instant
    next_periodic_at: Instant
    result: ScanResult


class Scheduler:
    def __init__(
        self,
        scan: ScanRunner,
        store: SchedulerStore,
        clock: Clock,
        *,
        watcher: SourceWatcher | None = None,
        maintenance: MaintenanceHook | None = None,
        wait: Wait = condition_wait,
        on_completed: Callable[[ScanCompletion], None] | None = None,
        before_scan: Callable[[ScanTrigger], None] | None = None,
        drain_inbox: Callable[[Callable[[], bool]], object] | None = None,
        on_maintenance: Callable[[], None] | None = None,
    ) -> None:
        self.scan, self.store, self.clock = scan, store, clock
        self.watcher, self._wait = watcher, wait
        self.maintenance = maintenance
        self._on_completed = on_completed
        self._before_scan = before_scan
        self._drain_inbox = drain_inbox
        self._on_maintenance = on_maintenance
        self._condition = Condition()
        self._control = Lock()
        self._stop = Event()
        self._cancel = Event()
        self._paused = store.paused()
        self._changing_pause = False
        self._state: Literal["idle", "scanning", "paused", "stopping"] = "paused" if self._paused else "idle"
        self._pending: list[Future[ScanCompletion]] = []
        self._active: list[Future[ScanCompletion]] = []
        self._periodic_due = clock.monotonic() + 30
        self._maintenance_due = clock.monotonic()
        self._notification_due: float | None = None
        self._baseline_due: float | None = None
        self._startup = True
        self._resume = False
        self._last_mono = clock.monotonic()
        self._last_wall = clock.now_utc()
        self._completed_at: Instant | None = None
        self._trigger: ScanTrigger | None = None
        self._error: str | None = None
        self._watcher_available = False
        self._thread: Thread | None = None

    def start(self) -> None:
        with self._condition:
            if self._stop.is_set():
                raise DomainError("COLLECTION_STOPPED", "采集调度器已停止")
            if self._thread is None:
                self._thread = Thread(target=self._run, name="htd-collection", daemon=True)
                self._thread.start()

    def _next_periodic_at(self) -> Instant:
        remaining = max(0, self._periodic_due - self.clock.monotonic())
        return Instant(self.clock.now_utc().value + timedelta(seconds=remaining))

    def status(self) -> SchedulerStatus:
        with self._condition:
            return SchedulerStatus(
                self._state, self._next_periodic_at(), self._completed_at, self._trigger, self._error
            )

    def notify(self) -> None:
        """A watcher callback only records a wakeup; it never performs I/O or collection."""
        with self._condition:
            if not self._paused and not self._stop.is_set() and self._notification_due is None:
                self._notification_due = self.clock.monotonic() + 2
                self._condition.notify_all()

    def scan_now(self) -> Future[ScanCompletion]:
        future: Future[ScanCompletion] = Future()
        with self._condition:
            error = (
                DomainError("COLLECTION_STOPPED", "采集调度器已停止")
                if self._stop.is_set()
                else DomainError("COLLECTION_PAUSED", "采集已暂停")
                if self._paused
                else None
            )
            if error is None:
                self._pending.append(future)
                self._condition.notify_all()
        if error is not None:
            future.set_exception(error)
        return future

    @staticmethod
    def _fail(futures: list[Future[ScanCompletion]], error: Exception) -> None:
        for future in futures:
            # A caller may have cancelled a queued Future concurrently.
            with suppress(InvalidStateError):
                future.set_exception(error)

    def set_paused(self, paused: bool, *, persist: Callable[[], None] | None = None) -> None:
        if type(paused) is not bool:
            raise ValueError("Paused must be a boolean")
        with self._control:
            with self._condition:
                if self._stop.is_set():
                    raise DomainError("COLLECTION_STOPPED", "采集调度器已停止")
                old = self._paused
                self._paused = paused
                self._changing_pause = True
                if paused:
                    self._cancel.set()
                self._condition.notify_all()
            try:
                if persist is None:
                    self.store.set_paused(paused)
                else:
                    persist()
            except Exception:
                with self._condition:
                    self._paused = old
                    self._changing_pause = False
                    self._condition.notify_all()
                raise
            with self._condition:
                self._changing_pause = False
                pending = self._pending if paused else []
                if paused:
                    self._pending = []
                    self._notification_due = self._baseline_due = None
                elif old:
                    self._resume = True
                if self._stop.is_set():
                    self._state = "stopping"
                elif self._state != "scanning":
                    self._state = "paused" if paused else "idle"
                self._condition.notify_all()
            self._fail(pending, DomainError("COLLECTION_PAUSED", "采集已暂停"))

    def _detect_resume(self, now: float) -> None:
        wall = self.clock.now_utc()
        elapsed = now - self._last_mono
        wall_elapsed = (wall.value - self._last_wall.value).total_seconds()
        if elapsed > 60 or abs(wall_elapsed) > 60 or abs(wall_elapsed - elapsed) > 60:
            self._resume = True
        self._last_mono, self._last_wall = now, wall

    def _choose(self, now: float) -> ScanTrigger | None:
        if self._stop.is_set() or self._paused or self._changing_pause:
            return None
        if self._pending:
            return "manual"
        if self._resume:
            return "resume"
        if self._startup:
            return "startup"
        if now >= self._periodic_due:
            return "periodic"
        if self._baseline_due is not None and now >= self._baseline_due:
            return "baseline"
        if self._notification_due is not None and now >= self._notification_due:
            return "notification"
        return None

    def _refresh_watch(self) -> None:
        try:
            available = self.watcher is not None and self.watcher.refresh(self.store.sources(), self.notify)
        except Exception:
            _LOG.exception("Source watcher failed; using periodic collection")
            available = False
        self._watcher_available = available

    def refresh_sources(self) -> None:
        if self._thread is None or self._stop.is_set():
            return
        self._refresh_watch()
        self.notify()

    def _run(self) -> None:
        try:
            self._refresh_watch()
            self.store.watcher_status(self._watcher_available)
            while not self._stop.is_set():
                with self._condition:
                    if self._stop.is_set():
                        break
                    now = self.clock.monotonic()
                    self._detect_resume(now)
                    trigger = self._choose(now)
                    paused_maintenance = (
                        trigger is None
                        and self._paused
                        and not self._changing_pause
                        and self.maintenance is not None
                        and now >= self._maintenance_due
                    )
                    if trigger is None and not paused_maintenance:
                        deadlines = [now + 15]
                        if self._paused and not self._changing_pause and self.maintenance is not None:
                            deadlines.append(self._maintenance_due)
                        if not self._paused and not self._changing_pause:
                            deadlines.append(self._periodic_due)
                            deadlines.extend(
                                d for d in (self._notification_due, self._baseline_due) if d is not None
                            )
                        self._wait(self._condition, min(15, max(0, min(deadlines) - now)))
                        continue
                    if not paused_maintenance:
                        assert trigger is not None
                        self._state, self._trigger = "scanning", trigger
                        self._active = [f for f in self._pending if f.set_running_or_notify_cancel()]
                        self._pending = []
                        self._cancel = Event()
                        self._startup = self._resume = False
                        self._notification_due = self._baseline_due = None
                if paused_maintenance:
                    self._maintain(more=False, paused=True, should_stop=self._stop_paused_maintenance)
                else:
                    assert trigger is not None
                    self._run_round(trigger)
        except Exception as error:
            _LOG.exception("Collection coordinator stopped unexpectedly")
            self._stop.set()
            with self._condition:
                futures = self._active + self._pending
                self._active, self._pending = [], []
            self._fail(futures, error)
        finally:
            with self._condition:
                self._state = "stopping"
                self._condition.notify_all()
            if self.watcher is not None:
                try:
                    self.watcher.close()
                except Exception:
                    _LOG.exception("Source watcher shutdown failed")

    def _stop_paused_maintenance(self) -> bool:
        return self._stop.is_set() or not self._paused

    def _maintain(self, *, more: bool, paused: bool, should_stop: Callable[[], bool]) -> None:
        try:
            if paused and self._drain_inbox is not None and not should_stop():
                self._drain_inbox(should_stop)
            if self.maintenance is not None:
                self.maintenance.run(more=more, paused=paused, should_stop=should_stop)
            if self._on_maintenance is not None:
                self._on_maintenance()
        except Exception:
            # Billing already committed; a failed hook must not turn it into a failed scan.
            _LOG.exception("Automatic collection maintenance failed")
        finally:
            with self._condition:
                self._maintenance_due = self.clock.monotonic() + 30

    def _run_round(self, trigger: ScanTrigger) -> None:
        result: ScanResult | None = None
        error: Exception | None = None
        revision = 0
        try:
            if self._before_scan is not None:
                self._before_scan(trigger)
            result = self.scan.run(should_stop=self._cancel.is_set)
            if result.interrupted or self._cancel.is_set():
                code = "COLLECTION_STOPPED" if self._stop.is_set() else "COLLECTION_PAUSED"
                raise DomainError(code, "采集已停止" if self._stop.is_set() else "采集已暂停")
            self._maintain(more=result.more, paused=False, should_stop=self._cancel.is_set)
            if self._cancel.is_set():
                code = "COLLECTION_STOPPED" if self._stop.is_set() else "COLLECTION_PAUSED"
                raise DomainError(code, "采集已停止" if self._stop.is_set() else "采集已暂停")
            self._refresh_watch()
            revision = self.store.completed(trigger, watcher_available=self._watcher_available)
        except Exception as failed:
            error = failed
            if not isinstance(failed, DomainError) or failed.code not in (
                "COLLECTION_PAUSED",
                "COLLECTION_STOPPED",
            ):
                _LOG.exception("Collection round failed")
        with self._condition:
            now = self.clock.monotonic()
            at = self.clock.now_utc()
            if trigger in ("startup", "periodic", "manual") or (
                trigger == "resume" and self._periodic_due <= now
            ):
                self._periodic_due = now + 30
            self._detect_resume(now)
            if error is None:
                self._completed_at = at
                if result is not None and result.more:
                    self._baseline_due = now + 0.1
            self._error = (
                error.code if isinstance(error, DomainError) else "COLLECTION_FAILED" if error else None
            )
            self._state = "stopping" if self._stop.is_set() else "paused" if self._paused else "idle"
            active, self._active = self._active, []
            completion = (
                ScanCompletion(trigger, revision, at, self._next_periodic_at(), result)
                if error is None and result is not None
                else None
            )
            self._condition.notify_all()
        # Future callbacks can reenter the scheduler, so resolve outside its condition lock.
        if error is not None:
            self._fail(active, error)
        elif completion is not None:
            if self._on_completed is not None:
                try:
                    self._on_completed(completion)
                except Exception:
                    _LOG.exception("Collection completion snapshot failed")
            for future in active:
                if not future.done():
                    future.set_result(completion)

    def close(self, timeout: float = 10) -> None:
        if not 0 <= timeout <= 10:
            raise ValueError("Shutdown timeout must be between 0 and 10 seconds")
        if current_thread() is self._thread:
            raise RuntimeError("Cannot join collection coordinator from its own thread")
        with self._condition:
            self._stop.set()
            self._cancel.set()
            self._state = "stopping"
            pending, self._pending = self._pending, []
            self._condition.notify_all()
            thread = self._thread
        self._fail(pending, DomainError("COLLECTION_STOPPED", "采集调度器已停止"))
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise DomainError("COLLECTION_CLOSE_TIMEOUT", "采集仍在完成当前事务")
