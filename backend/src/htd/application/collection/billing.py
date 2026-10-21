"""Price inbox requests and append wallet consumption in the same bounded transaction."""

from collections.abc import Callable
from dataclasses import dataclass

from htd.application.collection.cancellation import CollectionInterrupted, check_cancelled
from htd.application.ports import (
    BillBatchResult,
    BilledUsage,
    CollectionStore,
    UnitOfWork,
    UsageChanged,
    WalletChanged,
)
from htd.domain.billing.estimate import estimate_cost
from htd.domain.common.time import Clock, parse_instant
from htd.domain.usage.model import Harness, UsageEvent


@dataclass(frozen=True, slots=True)
class BillingResult:
    batch: BillBatchResult
    blocked: bool = False


class BillInbox:
    def __init__(self, store: CollectionStore, clock: Clock, *, batch_size: int = 500) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError("Billing batch size must be between 1 and 500")
        self.store, self.clock, self.batch_size = store, clock, batch_size

    def run_batch(self, *, should_stop: Callable[[], bool] = lambda: False) -> BillingResult:
        at = self.clock.now_utc()

        def transact(work: UnitOfWork) -> BillingResult:
            check_cancelled(should_stop)
            catalog = self.store.catalog(work)
            if not catalog.groups:
                return BillingResult(BillBatchResult(0, 0, 0, self.store.has_inbox(work), ()), True)

            def calculate(event: UsageEvent) -> BilledUsage:
                occurred_at = parse_instant(event.occurred_at)
                resolved = catalog.resolve(event.harness, occurred_at)
                rate = resolved.rate_for(event.model_id)
                return BilledUsage(
                    event,
                    estimate_cost(event.tokens, rate, occurred_at),
                    resolved.group.id if resolved.group else None,
                    resolved.version.id if resolved.version else None,
                    resolved.revision.id if resolved.revision else None,
                    rate.model_id if rate else None,
                )

            vendors: set[str] = set()

            def wallet(work: UnitOfWork, billed: BilledUsage) -> None:
                vendor = self.store.append_consumption(work, billed, at=at)
                if vendor is not None:
                    vendors.add(vendor)

            result = self.store.bill_batch(work, calculate, at=at, limit=self.batch_size, on_billed=wallet)
            if result.billed:
                work.emit(UsageChanged(result.harnesses, work.revision("usage")))
            if vendors:
                work.emit(WalletChanged(tuple(sorted(vendors)), work.revision("wallet")))
            return BillingResult(result)

        return self.store.execute(transact)

    def run(self, *, should_stop: Callable[[], bool] = lambda: False) -> BillingResult:
        billed = corrupt = duplicates = 0
        harnesses: set[Harness] = set()
        while True:
            try:
                check_cancelled(should_stop)
                result = self.run_batch(should_stop=should_stop)
            except CollectionInterrupted:
                return BillingResult(
                    BillBatchResult(
                        billed, corrupt, duplicates, True, tuple(h for h in Harness if h in harnesses)
                    )
                )
            batch = result.batch
            billed += batch.billed
            corrupt += batch.corrupt
            duplicates += batch.duplicates
            harnesses.update(batch.harnesses)
            if result.blocked or not batch.more or not batch.processed:
                return BillingResult(
                    BillBatchResult(
                        billed, corrupt, duplicates, batch.more, tuple(h for h in Harness if h in harnesses)
                    ),
                    result.blocked,
                )
