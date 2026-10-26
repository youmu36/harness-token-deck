"""Independent fixed title charges, atomically attributed and deduplicated without usage rows."""

import re
import sqlite3
from uuid import uuid4

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.transaction import require_transaction
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.application.ports import AuxiliaryChanged, TitleRequest, UnitOfWork, WalletChanged
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.model import Harness
from htd.domain.wallet.ledger import attribute_consumption
from htd.domain.wallet.model import UsageRef, WalletEntry

TITLE_PICO = 1_000_000_000  # Exactly 0.001 USD; independent of model prices and multipliers.


class TitleRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def collect(self, work: UnitOfWork, requests: tuple[TitleRequest, ...], *, at: Instant) -> None:
        require_transaction(work, self.connection)
        before = work.revision("auxiliary")
        for request in requests:
            if re.fullmatch(r"[0-9a-f]{64}", request.dedupe_key) is None:
                raise ValueError("Invalid title identity")
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO title_request(dedupe_key,occurred_at,model_id) VALUES(?,?,?)",
                (key_bytes(request.dedupe_key), format_instant(request.occurred_at), request.model_id),
            ).rowcount
            if inserted:
                work.touch("auxiliary")
        if work.revision("auxiliary") != before:
            work.emit(AuxiliaryChanged(work.revision("auxiliary")))
        if (
            self.connection.execute(
                "SELECT 1 FROM title_request WHERE wallet_entry_id IS NULL AND "
                "attribution_revision<>? LIMIT 1",
                (work.revision("ownership"),),
            ).fetchone()
            is None
        ):
            return
        catalog = PricingRepository(self.connection).load()
        wallet = WalletRepository(self.connection)
        links = wallet.links()
        vendor_ids = {v.id for v in wallet.list_vendors(include_archived=True)}
        changed: set[str] = set()
        ownership = work.revision("ownership")
        for row in self.connection.execute(
            "SELECT * FROM title_request WHERE wallet_entry_id IS NULL AND attribution_revision<>? "
            "ORDER BY occurred_at LIMIT 500",
            (ownership,),
        ).fetchall():
            self.connection.execute(
                "UPDATE title_request SET attribution_revision=? WHERE dedupe_key=?",
                (ownership, key_bytes(row["dedupe_key"])),
            )
            occurred = parse_instant(row["occurred_at"])
            resolved = catalog.resolve(Harness.CODEX, occurred)
            group_id = resolved.group.id if resolved.group else None
            vendor_id = attribute_consumption(links, group_id, occurred)
            if vendor_id is None or vendor_id not in vendor_ids:
                continue
            entry = WalletEntry(
                str(uuid4()),
                vendor_id,
                "consumption",
                TITLE_PICO,
                occurred,
                wallet.next_seq(),
                usage_ref=UsageRef(Harness.CODEX, row["dedupe_key"]),
                note="title_generation",
            )
            wallet.append_with_offsets(work, entry, at=at)
            self.connection.execute(
                "UPDATE title_request SET price_group_id=?,vendor_id=?,wallet_entry_id=? WHERE dedupe_key=?",
                (group_id, vendor_id, entry.id, key_bytes(row["dedupe_key"])),
            )
            changed.add(vendor_id)
        if changed:
            self.connection.execute(
                "UPDATE collection_activity SET generation=generation+1,last_at=? WHERE id=1",
                (format_instant(at),),
            )
            work.emit(WalletChanged(tuple(sorted(changed)), work.revision("wallet")))
