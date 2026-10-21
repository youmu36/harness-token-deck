"""Atomic remote calibration and read-only, selection-aware mismatch notices."""

from datetime import timedelta

from htd.adapters.sqlite.command_store import read_snapshot
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.usage_archive import archived_rows
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.model import Harness
from htd.domain.wallet.ledger import attribute_consumption


class SqliteReconciliationStore:
    def __init__(self, readers: ReadPool, writer: WriteExecutor | None = None) -> None:
        self.readers, self.writer = readers, writer

    def dismiss(self, vendor_id: str, observed_at: str) -> None:
        if self.writer is None:
            raise RuntimeError("Dismissing a notice requires the writer")
        # Idempotent and tied to the displayed observation: a delayed click
        # must never dismiss a newer remote decrease.
        self.writer.execute(
            lambda work: (
                work.connection.execute(
                    "DELETE FROM reconciliation_notice WHERE vendor_id=? AND observed_at=?",
                    (vendor_id, observed_at),
                ).rowcount
            )
        )

    def notices(self, at: Instant) -> list[dict[str, object]]:
        with read_snapshot(self.readers) as db:
            rows = db.execute(
                "SELECT n.*,v.name FROM reconciliation_notice n JOIN vendor v ON v.id=n.vendor_id "
                "WHERE v.status='active' ORDER BY n.observed_at DESC,n.vendor_id"
            ).fetchall()
            if not rows:
                return []
            catalog = PricingRepository(db).load()
            wallet = WalletRepository(db)
            links = wallet.links()
            notices: list[dict[str, object]] = []
            for row in rows:
                # Only actual usage supplies evidence of which Harness needs attention.
                activity = {
                    item[0]
                    for item in db.execute(
                        "SELECT DISTINCT harness FROM (SELECT harness,occurred_at FROM ledger UNION ALL "
                        "SELECT harness,bucket FROM usage_rollup) WHERE occurred_at>=? AND occurred_at<=?",
                        (row["from_at"], row["observed_at"]),
                    )
                }
                activity.update(
                    item[0]
                    for item in db.execute(
                        "SELECT DISTINCT harness FROM usage_daily WHERE end_at>? AND start_at<=?",
                        (row["from_at"], row["observed_at"]),
                    )
                )
                activity.update(
                    item["harness"]
                    for item in archived_rows(
                        db,
                        row["from_at"],
                        format_instant(
                            Instant(parse_instant(row["observed_at"]).value + timedelta(milliseconds=1))
                        ),
                    )
                )
                for name in sorted(activity):
                    harness = Harness(name)
                    selected = catalog.resolve(harness, at).group
                    owner = attribute_consumption(links, selected.id if selected else None, at)
                    selections = sorted(
                        (
                            item
                            for item in catalog.selections
                            if item.harness == harness and item.effective_at <= at
                        ),
                        key=lambda item: (item.effective_at, item.seq),
                    )
                    # Re-selecting a version in the same group is not a supplier switch.
                    index = len(selections) - 1
                    while index > 0 and selections[index - 1].group_id == selections[index].group_id:
                        index -= 1
                    selection = selections[index] if selections else None
                    if selected is None or selection is None or owner == row["vendor_id"]:
                        continue
                    switched_at = format_instant(selection.effective_at)
                    # A decrease entirely before the current selection belongs
                    # to an earlier supplier choice, even if settlement is late.
                    if switched_at > row["observed_at"]:
                        continue
                    previous = selections[index - 1] if index > 0 else None
                    if (
                        previous is not None
                        and previous.group_id != selected.id
                        and switched_at > row["from_at"]
                    ):
                        edge = format_instant(Instant(selection.effective_at.value - timedelta(minutes=5)))
                        # Only precise, recent facts can establish continuity
                        # across a supplier change. Daily/archive totals cannot.
                        active_before_switch = db.execute(
                            "SELECT 1 FROM ledger WHERE harness=? AND price_group_id=? "
                            "AND occurred_at>=? AND occurred_at<=? "
                            "AND (input>0 OR output>0) LIMIT 1",
                            (name, previous.group_id, edge, switched_at),
                        ).fetchone()
                        if active_before_switch is None:
                            continue
                    notices.append(
                        {
                            "vendorId": row["vendor_id"],
                            "vendorName": row["name"],
                            "harness": harness.value,
                            "selectedGroupId": selected.id if selected else None,
                            "selectedGroupName": selected.name if selected else None,
                            "observedAt": row["observed_at"],
                            "remoteDecreasePico": row["amount_pico"],
                        }
                    )
            return notices
