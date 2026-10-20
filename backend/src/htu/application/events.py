"""Thread-safe post-commit delivery, isolating subscriber failures from producers."""

import logging
from collections.abc import Callable
from threading import Lock

_LOG = logging.getLogger(__name__)


class InProcessEventBus:
    def __init__(self) -> None:
        self._lock = Lock()
        self._subscribers: list[Callable[[object], None]] = []

    def subscribe(self, callback: Callable[[object], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def publish(self, event: object) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                _LOG.exception("Event subscriber failed")
