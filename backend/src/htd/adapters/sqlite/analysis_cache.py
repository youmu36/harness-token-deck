"""Bounded immutable projections and single-flight metadata, without holding DB locks."""

import sys
from collections import OrderedDict
from concurrent.futures import Future
from threading import Lock
from time import monotonic
from typing import Any

from htd.adapters.sqlite.projection_snapshot import ProjectionSnapshot
from htd.application.ports.query_control import ReadControl, SharedReadControl


class AnalysisCache:
    def __init__(self, max_bytes: int = 8 * 1024**2, capacity: int = 8, *, compress: bool = True) -> None:
        self.lock = Lock()
        self.max_bytes, self.capacity = max_bytes, capacity
        self.compress = compress
        self.bytes = 0
        self.last_used = monotonic()
        self.entries: OrderedDict[str, tuple[ProjectionSnapshot, int]] = OrderedDict()
        self.pending: dict[str, Future[ProjectionSnapshot]] = {}
        self.controls: dict[Future[ProjectionSnapshot], SharedReadControl] = {}
        self.expires: dict[str, float] = {}
        self.generation = 0

    def clear(self) -> None:
        with self.lock:
            self.generation += 1
            self.entries.clear()
            self.expires.clear()
            self.bytes = 0

    def expire_idle(self) -> None:
        with self.lock:
            if self.entries and monotonic() - self.last_used >= 60:
                self.generation += 1
                self.entries.clear()
                self.expires.clear()
                self.bytes = 0

    def claim(
        self,
        key: str,
        reader: ReadControl | None = None,
        *,
        at: float | None = None,
    ) -> tuple[Future[ProjectionSnapshot], bool]:
        with self.lock:
            if monotonic() - self.last_used >= 60:
                self.entries.clear()
                self.expires.clear()
                self.bytes = 0
            self.last_used = monotonic()
            if at is not None and at >= self.expires.get(key, float("inf")):
                previous = self.entries.pop(key, None)
                if previous is not None:
                    self.bytes -= previous[1]
                self.expires.pop(key, None)
            if key in self.entries:
                self.entries.move_to_end(key)
                completed: Future[ProjectionSnapshot] = Future()
                completed.set_result(self.entries[key][0])
                return completed, False
            if key in self.pending:
                future = self.pending[key]
                if reader:
                    self.controls[future].join(reader)
                return future, False
            future: Future[ProjectionSnapshot] = Future()
            self.pending[key] = future
            self.controls[future] = SharedReadControl()
            if reader:
                self.controls[future].join(reader)
            return future, True

    def shared(self, future: Future[ProjectionSnapshot]) -> SharedReadControl:
        with self.lock:
            return self.controls[future]

    def release(self, future: Future[ProjectionSnapshot], reader: ReadControl) -> None:
        with self.lock:
            control = self.controls.get(future)
            if control:
                control.leave(reader)
                if future.done() and not control.reader_count():
                    self.controls.pop(future, None)

    def finish(
        self,
        key: str,
        future: Future[ProjectionSnapshot],
        result: dict[str, Any],
        generation: int,
        *,
        expires_at: float | None = None,
    ) -> None:
        projection = ProjectionSnapshot.build(result, compress=self.compress, expires_at=expires_at)
        # Includes owned encoded blocks and key metadata; process memory is measured separately.
        size = projection.retained_bytes + sys.getsizeof(key) + 256
        with self.lock:
            if generation == self.generation and size <= self.max_bytes:
                previous = self.entries.pop(key, None)
                if previous is not None:
                    self.bytes -= previous[1]
                self.entries[key] = projection, size
                if expires_at is not None:
                    self.expires[key] = expires_at
                else:
                    self.expires.pop(key, None)
                self.bytes += size
                while self.bytes > self.max_bytes or len(self.entries) > self.capacity:
                    removed_key, (_, removed) = self.entries.popitem(last=False)
                    self.expires.pop(removed_key, None)
                    self.bytes -= removed
            self.pending.pop(key, None)
        future.set_result(projection)
        self._completed(future)

    def fail(self, key: str, future: Future[ProjectionSnapshot], error: BaseException) -> None:
        with self.lock:
            self.pending.pop(key, None)
        future.set_exception(error)
        self._completed(future)

    def _completed(self, future: Future[ProjectionSnapshot]) -> None:
        with self.lock:
            control = self.controls.get(future)
            if control is not None and not control.reader_count():
                self.controls.pop(future, None)
