"""Atomic remote calibration and read-only, selection-aware mismatch notices."""

from datetime import timedelta

from htu.adapters.sqlite.command_store import read_snapshot
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.usage_archive import archived_rows
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.usage.model import Harness
from htu.domain.wallet.ledger import attribute_consumption


class SqliteReconciliationStore:
    def __init__(self, readers: ReadPool) -> None:
        self.readers = readers

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
                    if owner == row["vendor_id"]:
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
