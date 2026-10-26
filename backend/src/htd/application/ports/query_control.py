"""Cooperative cancellation for reads; write transactions never enter this context."""

from collections import OrderedDict
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Event, Lock
from time import monotonic

from htd.domain.common.errors import DomainError


@dataclass
class ReadControl:
    deadline: float
    stopped: Event = field(default_factory=Event)

    def cancelled(self) -> bool:
        return self.stopped.is_set() or monotonic() >= self.deadline

    def check(self) -> None:
        if self.cancelled():
            raise DomainError("READ_CANCELLED", "读取已取消，请按当前条件重试", retryable=True)


class SharedReadControl(ReadControl):
    """The executing reader may leave while another reader still needs the same result."""

    def __init__(self) -> None:
        super().__init__(monotonic() + 30)
        self._lock = Lock()
        self._readers: dict[int, ReadControl] = {}

    def join(self, reader: ReadControl) -> None:
        with self._lock:
            self._readers[id(reader)] = reader

    def leave(self, reader: ReadControl) -> None:
        with self._lock:
            self._readers.pop(id(reader), None)

    def reader_count(self) -> int:
        with self._lock:
            return len(self._readers)

    def cancelled(self) -> bool:
        with self._lock:
            return super().cancelled() or not any(not reader.cancelled() for reader in self._readers.values())


class QuerySlots:
    """One bounded limiter is injected into all heavy read paths; followers do not acquire it."""

    def __init__(self) -> None:
        self._slots = BoundedSemaphore(2)

    @contextmanager
    def acquire(self) -> Generator[None, None, None]:
        while not self._slots.acquire(timeout=0.05):
            check_read()
        try:
            check_read()
            yield
        finally:
            self._slots.release()


current_read: ContextVar[ReadControl | None] = ContextVar("read_control", default=None)
_lock = Lock()
_analyses: dict[str, ReadControl] = {}
_cancelled: OrderedDict[str, float] = OrderedDict()


def check_read() -> None:
    control = current_read.get()
    if control:
        control.check()


def cancel_analysis(identity: str) -> None:
    with _lock:
        control = _analyses.get(identity)
        if control:
            control.stopped.set()
        else:
            _cancelled[identity] = monotonic()
            while len(_cancelled) > 128:
                _cancelled.popitem(last=False)


@contextmanager
def analysis_read(identity: str | None) -> Generator[None, None, None]:
    control = current_read.get() or ReadControl(monotonic() + 30)
    token = current_read.set(control)
    if identity:
        with _lock:
            cancelled_at = _cancelled.pop(identity, None)
            if cancelled_at is not None and monotonic() - cancelled_at <= 30:
                control.stopped.set()
            previous = _analyses.get(identity)
            if previous and previous is not control:
                previous.stopped.set()
            _analyses[identity] = control
    try:
        control.check()
        yield
    finally:
        if identity:
            with _lock:
                if _analyses.get(identity) is control:
                    del _analyses[identity]
        current_read.reset(token)
