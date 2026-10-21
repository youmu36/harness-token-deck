"""Wallet queries, optimistic calibration and full atomic IPC receipts."""

from collections.abc import Callable
from typing import cast

from htd.application.command import execute, expected
from htd.application.ports import (
    PricingChanged,
    UnitOfWork,
    VendorSnapshot,
    WalletChanged,
    WalletCommandStore,
)
from htd.domain.common.time import Clock, Instant, format_instant


def vendor_dict(snapshot: VendorSnapshot) -> dict[str, object]:
    vendor, totals = snapshot.vendor, snapshot.totals
    return {
        "id": vendor.id,
        "name": vendor.name,
        "url": vendor.url,
        "status": vendor.status,
        "createdAt": format_instant(vendor.created_at),
        "R": str(totals.r),
        "pastTopUpPico": str(vendor.past_top_up_pico),
        "pastConsumptionPico": str(vendor.past_consumption_pico),
        "C": str(totals.c),
        "A": str(totals.a),
        "B": str(totals.b),
        "lastCalibratedAt": format_instant(totals.last_calibrated_at) if totals.last_calibrated_at else None,
        "linkedGroups": list(snapshot.linked_groups),
        "coverageIncomplete": totals.coverage_incomplete,
        "unpricedCount": totals.unpriced_count,
        "incompleteCount": totals.incomplete_count,
        "historyCoverageStart": format_instant(snapshot.history_coverage_start)
        if snapshot.history_coverage_start
        else None,
        "walletRevision": totals.revision,
        "account": snapshot.account,
        "localWallet": snapshot.local_wallet,
        "remoteWallet": snapshot.remote_wallet,
        "walletVisibility": snapshot.wallet_visibility,
    }


class Wallet:
    def __init__(self, store: WalletCommandStore, clock: Clock) -> None:
        self.store, self.clock = store, clock

    def list(self, include_archived: bool) -> dict[str, object]:
        snapshots, revision = self.store.wallets(at=self.clock.now_utc(), include_archived=include_archived)
        return {"vendors": [vendor_dict(s) for s in snapshots], "revision": revision}

    def entries(
        self, vendor_id: str, cursor: int | None, limit: int, kind: str = "all", offset_minutes: int = 0
    ) -> dict[str, object]:
        return self.store.wallet_entries(
            vendor_id, cursor, limit, kind, offset_minutes, at=self.clock.now_utc()
        )

    def preview_interval(self, vendor_id: str, interval_id: str) -> dict[str, object]:
        return self.store.preview_interval(vendor_id, interval_id)

    def account_entries(
        self,
        vendor_id: str,
        cursor: str | None,
        limit: int,
        scope: str = "legacy",
        kind: str = "all",
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> dict[str, object]:
        return self.store.account_entries(
            vendor_id,
            cursor,
            limit,
            scope,
            at=self.clock.now_utc(),
            kind=kind,
            start_day=start_day,
            end_day=end_day,
        )

    def daily_book(self, vendor_id: str, scope: str, month: str) -> dict[str, object]:
        return self.store.daily_book(vendor_id, scope, month, self.clock.now_utc())

    def change(
        self,
        command: str,
        parameters: dict[str, object],
        operation_id: str,
        *,
        prepare: Callable[[], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        def save(work: UnitOfWork, at: Instant) -> dict[str, object]:
            before, pricing_before = work.revision("wallet"), work.revision("pricing")
            if command == "wallet.calibrate":
                expected(
                    work,
                    "wallet",
                    cast(int, parameters["expectedWalletRevision"]),
                    field="expected_wallet_revision",
                )
            if command == "wallet.linkGroups":
                expected(work, "wallet", cast(int, parameters["expectedRevision"]))
            vendor_id, action, affected = self.store.wallet_command(
                work, command, prepare() if prepare else parameters, operation_id, at=at
            )
            snapshots, revision = self.store.wallets(at=at, include_archived=True, work=work)
            vendor = next((s for s in snapshots if s.vendor.id == vendor_id), None)
            if before != revision:
                work.emit(WalletChanged(affected, revision))
            if pricing_before != work.revision("pricing"):
                work.emit(PricingChanged(work.revision("pricing")))
            return {
                "vendor": vendor_dict(vendor) if vendor else None,
                "vendorId": vendor_id,
                "action": action,
                "changed": before != revision,
                "revision": revision,
                "pricingRevision": work.revision("pricing"),
            }

        return execute(self.store, self.clock, command, parameters, operation_id, save)
