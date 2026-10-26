"""Independent reconciliation query use case; UI never infers suppliers from money deltas."""

from typing import Protocol

from htd.domain.common.time import Clock, Instant


class ReconciliationStore(Protocol):
    def preview(self, query: dict[str, object], at: Instant) -> dict[str, object]: ...

    def batches(self, query: dict[str, object], at: Instant) -> dict[str, object]: ...

    def notices(self, at: Instant) -> list[dict[str, object]]: ...

    def dismiss(self, vendor_id: str, observed_at: str) -> None: ...


class Reconciliation:
    def __init__(self, store: ReconciliationStore, clock: Clock) -> None:
        self.store, self.clock = store, clock

    def preview(self, query: dict[str, object]) -> dict[str, object]:
        return self.store.preview(query, self.clock.now_utc())

    def batches(self, query: dict[str, object]) -> dict[str, object]:
        return self.store.batches(query, self.clock.now_utc())

    def status(self) -> dict[str, object]:
        return {"notices": self.store.notices(self.clock.now_utc())}

    def dismiss(self, vendor_id: str, observed_at: str) -> dict[str, object]:
        self.store.dismiss(vendor_id, observed_at)
        return self.status()
