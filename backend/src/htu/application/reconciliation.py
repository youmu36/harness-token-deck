"""Independent reconciliation query use case; UI never infers suppliers from money deltas."""

from typing import Protocol

from htu.domain.common.time import Clock, Instant


class ReconciliationStore(Protocol):
    def notices(self, at: Instant) -> list[dict[str, object]]: ...


class Reconciliation:
    def __init__(self, store: ReconciliationStore, clock: Clock) -> None:
        self.store, self.clock = store, clock

    def status(self) -> dict[str, object]:
        return {"notices": self.store.notices(self.clock.now_utc())}
