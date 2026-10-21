"""Persist vendor commands, immutable association timelines, and atomic wallet receipts."""

from dataclasses import replace
from typing import cast
from uuid import uuid4

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.operation_repo import OperationRepository
from htd.adapters.sqlite.transaction import require_transaction
from htd.adapters.sqlite.wallet_store import WalletStore, decode_entry
from htd.application.ports import BilledUsage, UnitOfWork
from htd.domain.common.errors import OPERATION_ID_REUSED, VENDOR_NOT_FOUND, WALLET_INVALID, DomainError
from htd.domain.common.money import parse_usd_amount
from htd.domain.common.site_url import site_url
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.pricing.history import HISTORY_GROUP_ID
from htd.domain.wallet import ledger, vendors
from htd.domain.wallet.model import UsageRef, Vendor, VendorLink, VendorStatus, WalletEntry


class WalletRepository(WalletStore):
    def list_vendors(self, *, include_archived: bool = False) -> tuple[Vendor, ...]:
        return tuple(
            Vendor(
                row["id"],
                row["name"],
                cast(VendorStatus, row["status"]),
                parse_instant(row["created_at"]),
                row["url"],
                int(row["past_top_up"]),
                int(row["past_consumption"]),
            )
            for row in self.connection.execute(
                "SELECT v.*,COALESCE(s.url,r.base_url) url,COALESCE(a.past_top_up,'0') "
                "past_top_up,COALESCE(a.past_consumption,'0') past_consumption FROM vendor v "
                "LEFT JOIN vendor_accounting a ON a.vendor_id=v.id "
                "LEFT JOIN vendor_site s ON s.vendor_id=v.id LEFT JOIN remote_account r ON r.vendor_id=v.id "
                "WHERE ? OR status='active' ORDER BY seq",
                (int(include_archived),),
            )
        )

    def vendor(self, vendor_id: str) -> Vendor:
        row = self.connection.execute(
            "SELECT v.*,COALESCE(s.url,r.base_url) url,COALESCE(a.past_top_up,'0') past_top_up,"
            "COALESCE(a.past_consumption,'0') past_consumption FROM vendor v "
            "LEFT JOIN vendor_accounting a ON a.vendor_id=v.id LEFT JOIN vendor_site s ON s.vendor_id=v.id "
            "LEFT JOIN remote_account r ON r.vendor_id=v.id WHERE v.id=?",
            (vendor_id,),
        ).fetchone()
        if row is None:
            raise DomainError(VENDOR_NOT_FOUND, "供应商不存在", "vendor_id")
        return Vendor(
            row["id"],
            row["name"],
            cast(VendorStatus, row["status"]),
            parse_instant(row["created_at"]),
            row["url"],
            int(row["past_top_up"]),
            int(row["past_consumption"]),
        )

    def create_vendor(
        self,
        work: UnitOfWork,
        *,
        vendor_id: str,
        name: str,
        at: Instant,
        operation_id: str,
        initial_balance: str | None = None,
        url: str | None = None,
        past_top_up: str | None = None,
        past_consumption: str | None = None,
        record_operation: bool = True,
    ) -> Vendor:
        require_transaction(work, self.connection)
        normalized = vendors.validate_vendor_name((), name)
        initial = ledger.plan_initial_balance(initial_balance)
        params: dict[str, object] = {
            "vendor_id": vendor_id,
            "name": normalized,
            "initial_pico": str(initial.target_pico) if initial else None,
            "url": site_url(url),
            "past_top_up": str(parse_usd_amount(past_top_up or "0")),
            "past_consumption": str(parse_usd_amount(past_consumption or "0")),
        }
        operations = OperationRepository(self.connection)
        replay = operations.replay(operation_id, "wallet.create", params) if record_operation else None
        if replay is not None:
            return self.vendor(str(replay["id"]))
        new = vendors.create_vendor(
            self.list_vendors(include_archived=True), vendor_id=vendor_id, name=name, at=at
        )[-1]
        sequence = int(self.connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM vendor").fetchone()[0])
        self.connection.execute(
            "INSERT INTO vendor VALUES (?,?,?,?,?)",
            (new.id, new.name, new.status, format_instant(at), sequence),
        )
        self.connection.execute("INSERT INTO wallet_total VALUES (?,'0','0','0',0)", (new.id,))
        self.connection.execute(
            "INSERT INTO vendor_accounting(vendor_id,past_top_up,balance_initialized,past_consumption) "
            "VALUES(?,?,?,?)",
            (new.id, params["past_top_up"], int(initial is not None), params["past_consumption"]),
        )
        if self.connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='local_wallet_metadata'"
        ).fetchone():
            self.connection.execute(
                "INSERT INTO local_wallet_metadata VALUES(?,?,?)",
                (new.id, params["past_top_up"], params["past_consumption"]),
            )
        if url:
            self.set_site(work, new.id, url)
        if initial is not None:
            initial = replace(initial, watermark_seq=self.next_seq() - 1)
            entry, audit = initial.records(
                entry_id=str(uuid4()), vendor_id=new.id, at=at, seq=self.next_seq(), operation_id=operation_id
            )
            self.append(work, entry, calibration=audit)
        if record_operation:
            operations.record(
                work,
                operation_id,
                "wallet.create",
                params,
                {"id": new.id, "name": new.name, "created_at": format_instant(at)},
                at=at,
            )
        work.touch("wallet")
        return self.vendor(new.id)

    def set_past_top_up(self, work: UnitOfWork, vendor_id: str, amount: str) -> None:
        require_transaction(work, self.connection)
        vendors.require_active(self.vendor(vendor_id))
        pico = parse_usd_amount(amount)
        if self.vendor(vendor_id).past_top_up_pico != pico:
            self.connection.execute(
                "INSERT INTO vendor_accounting(vendor_id,past_top_up) VALUES(?,?) "
                "ON CONFLICT(vendor_id) DO UPDATE SET past_top_up=excluded.past_top_up",
                (vendor_id, str(pico)),
            )
            work.touch("wallet")

    def set_past_consumption(self, work: UnitOfWork, vendor_id: str, amount: str) -> None:
        require_transaction(work, self.connection)
        vendors.require_active(self.vendor(vendor_id))
        pico = parse_usd_amount(amount)
        if self.vendor(vendor_id).past_consumption_pico != pico:
            self.connection.execute(
                "INSERT INTO vendor_accounting(vendor_id,past_consumption) VALUES(?,?) "
                "ON CONFLICT(vendor_id) DO UPDATE SET past_consumption=excluded.past_consumption",
                (vendor_id, str(pico)),
            )
            work.touch("wallet")

    def set_site(self, work: UnitOfWork, vendor_id: str, value: str | None) -> None:
        require_transaction(work, self.connection)
        vendors.require_active(self.vendor(vendor_id))
        normalized = site_url(value)
        saved = self.connection.execute(
            "SELECT url FROM vendor_site WHERE vendor_id=?", (vendor_id,)
        ).fetchone()
        if normalized == (saved[0] if saved else None):
            return
        if normalized is None:
            self.connection.execute("DELETE FROM vendor_site WHERE vendor_id=?", (vendor_id,))
        else:
            self.connection.execute(
                "INSERT INTO vendor_site VALUES(?,?) ON CONFLICT(vendor_id) DO UPDATE SET url=excluded.url",
                (vendor_id, normalized),
            )
        work.touch("wallet")

    def rename_vendor(self, work: UnitOfWork, vendor_id: str, name: str) -> Vendor:
        require_transaction(work, self.connection)
        updated = vendors.rename_vendor(self.list_vendors(include_archived=True), vendor_id, name)
        new = next(v for v in updated if v.id == vendor_id)
        if new != self.vendor(vendor_id):
            self.connection.execute("UPDATE vendor SET name=? WHERE id=?", (new.name, new.id))
            work.touch("wallet")
        return new

    def restore_vendor(self, work: UnitOfWork, vendor_id: str) -> Vendor:
        require_transaction(work, self.connection)
        updated = vendors.restore_vendor(self.list_vendors(include_archived=True), vendor_id)
        new = next(v for v in updated if v.id == vendor_id)
        if new != self.vendor(vendor_id):
            self.connection.execute("UPDATE vendor SET status='active' WHERE id=?", (vendor_id,))
            work.touch("wallet")
        return new

    def links(self) -> tuple[VendorLink, ...]:
        return tuple(
            VendorLink(row["group_id"], row["vendor_id"], parse_instant(row["effective_at"]), row["seq"])
            for row in self.connection.execute("SELECT * FROM vendor_link ORDER BY seq")
        )

    def linked_groups(self, vendor_id: str, *, at: Instant) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self.connection.execute(
                "SELECT g.id FROM price_group g WHERE g.deleted_at IS NULL AND "
                "(SELECT vendor_id FROM vendor_link WHERE group_id=g.id AND effective_at<=? "
                "ORDER BY effective_at DESC,seq DESC LIMIT 1)=? ORDER BY g.id",
                (format_instant(at), vendor_id),
            )
        )

    def link_group(
        self, work: UnitOfWork, group_id: str, vendor_id: str | None, *, at: Instant
    ) -> VendorLink:
        require_transaction(work, self.connection)
        if group_id == HISTORY_GROUP_ID:
            raise DomainError(WALLET_INVALID, "历史价格组仅用于统计，不能关联供应商")
        if (
            self.connection.execute(
                "SELECT 1 FROM price_group WHERE id=? AND deleted_at IS NULL", (group_id,)
            ).fetchone()
            is None
        ):
            raise DomainError(WALLET_INVALID, "价格组不存在", "group_id")
        if vendor_id is not None:
            vendors.require_active(self.vendor(vendor_id))
        if any(link.group_id == group_id and link.effective_at > at for link in self.links()):
            raise DomainError(WALLET_INVALID, "关联时间不能早于最新关联", "effective_at")
        seq = self.connection.execute("INSERT INTO history_seq DEFAULT VALUES").lastrowid
        assert seq is not None
        link = VendorLink(group_id, vendor_id, at, seq)
        self._insert_link(link)
        work.touch("wallet")
        work.touch("pricing")
        return link

    def _insert_link(self, link: VendorLink) -> None:
        self.connection.execute("UPDATE revision SET value=value+1 WHERE scope='ownership'")
        self.connection.execute(
            "INSERT INTO vendor_link VALUES (?,?,?,?)",
            (link.seq, link.group_id, link.vendor_id, format_instant(link.effective_at)),
        )

    def delete_vendor(self, work: UnitOfWork, vendor_id: str, *, at: Instant) -> str:
        require_transaction(work, self.connection)
        entry = self.connection.execute(
            "SELECT * FROM wallet_entry WHERE vendor_id=? LIMIT 1", (vendor_id,)
        ).fetchone()
        # The pure plan only needs to establish the existence of a voucher.
        seq = int(self.connection.execute("SELECT COALESCE(MAX(id),0)+1 FROM history_seq").fetchone()[0])
        plan = vendors.plan_delete_vendor(
            self.vendor(vendor_id), (decode_entry(entry),) if entry else (), self.links(), at=at, next_seq=seq
        )
        for link in plan.unlinks:
            self.connection.execute("INSERT INTO history_seq(id) VALUES (?)", (link.seq,))
            self._insert_link(link)
        has_legacy = self.connection.execute(
            "SELECT 1 FROM vendor_legacy_record WHERE vendor_id=?", (vendor_id,)
        ).fetchone()
        has_rollup = self.connection.execute(
            "SELECT 1 FROM wallet_rollup WHERE vendor_id=? LIMIT 1", (vendor_id,)
        ).fetchone()
        has_financial = self.connection.execute(
            "SELECT 1 FROM wallet_archive WHERE vendor_id=? UNION ALL SELECT 1 FROM account_endpoint e "
            "JOIN account_segment s ON s.id=e.segment_id WHERE s.vendor_id=? UNION ALL "
            "SELECT 1 FROM account_posting WHERE vendor_id=? LIMIT 1",
            (vendor_id, vendor_id, vendor_id),
        ).fetchone()
        has_local = (
            self.connection.execute(
                "SELECT 1 FROM local_wallet_posting WHERE vendor_id=? LIMIT 1", (vendor_id,)
            ).fetchone()
            if self.connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='local_wallet_posting'"
            ).fetchone()
            else None
        )
        action = "archive" if has_legacy or has_rollup or has_financial or has_local else plan.action
        if action == "archive":
            self.connection.execute("UPDATE vendor SET status='archived' WHERE id=?", (vendor_id,))
        else:
            # Remote rows contain configuration only; credentials are removed by the later use case.
            self.connection.execute("DELETE FROM remote_account WHERE vendor_id=?", (vendor_id,))
            self.connection.execute("DELETE FROM account_segment WHERE vendor_id=?", (vendor_id,))
            self.connection.execute("DELETE FROM vendor WHERE id=?", (vendor_id,))
        work.touch("wallet")
        if plan.unlinks:
            work.touch("pricing")
        return action

    def top_up(
        self,
        work: UnitOfWork,
        vendor_id: str,
        amount: str,
        *,
        at: Instant,
        operation_id: str,
        record_operation: bool = True,
    ) -> WalletEntry:
        require_transaction(work, self.connection)
        pico = parse_usd_amount(amount)
        params: dict[str, object] = {"vendor_id": vendor_id, "amount_pico": str(pico)}
        operations = OperationRepository(self.connection)
        replay = operations.replay(operation_id, "wallet.top_up", params) if record_operation else None
        if replay is not None:
            prior = self.get_entry(str(replay["entry_id"]))
            assert prior is not None
            return prior
        prior = self._operation_entry(operation_id)
        if prior is not None:
            if (prior.kind, prior.vendor_id, prior.amount_pico) != ("top_up", vendor_id, pico):
                raise DomainError(OPERATION_ID_REUSED, "操作标识已用于其他资金操作", "operation_id")
            return prior
        entry = ledger.top_up(
            (),
            self.vendor(vendor_id),
            amount,
            entry_id=str(uuid4()),
            at=at,
            seq=self.next_seq(),
            operation_id=operation_id,
        )[-1]
        self.append(work, entry)
        if record_operation:
            operations.record(work, operation_id, "wallet.top_up", params, {"entry_id": entry.id}, at=at)
        return entry

    def calibrate(
        self,
        work: UnitOfWork,
        vendor_id: str,
        target: str,
        *,
        expected_revision: int,
        at: Instant,
        operation_id: str,
        record_operation: bool = True,
        target_pico: int | None = None,
    ) -> WalletEntry:
        require_transaction(work, self.connection)
        pico = parse_usd_amount(target, allow_negative=True) if target_pico is None else target_pico
        params: dict[str, object] = {
            "vendor_id": vendor_id,
            "target_pico": str(pico),
            "expected_revision": expected_revision,
        }
        operations = OperationRepository(self.connection)
        replay = operations.replay(operation_id, "wallet.calibrate", params) if record_operation else None
        if replay is not None:
            prior = self.get_entry(str(replay["entry_id"]))
            assert prior is not None
            return prior
        prior = self._operation_entry(operation_id)
        if prior is not None:
            audit = next((c for c in self.calibrations(prior.vendor_id) if c.entry_id == prior.id), None)
            if (
                prior.note != "calibration"
                or audit is None
                or (prior.vendor_id, audit.target_pico) != (vendor_id, pico)
            ):
                raise DomainError(OPERATION_ID_REUSED, "操作标识已用于其他资金操作", "operation_id")
            return prior
        vendors.require_active(self.vendor(vendor_id))
        plan = ledger.plan_calibration(self.totals(vendor_id), pico, expected_revision)
        entry, audit = plan.records(
            entry_id=str(uuid4()), vendor_id=vendor_id, at=at, seq=self.next_seq(), operation_id=operation_id
        )
        self.append(work, entry, calibration=audit)
        if record_operation:
            operations.record(work, operation_id, "wallet.calibrate", params, {"entry_id": entry.id}, at=at)
        return entry

    def _operation_entry(self, operation_id: str) -> WalletEntry | None:
        row = self.connection.execute(
            "SELECT * FROM wallet_entry WHERE operation_id=? AND "
            "(kind='top_up' OR (kind='adjustment' AND note='calibration'))",
            (operation_id,),
        ).fetchone()
        return decode_entry(row) if row else None

    def append_consumption(
        self,
        work: UnitOfWork,
        billed: BilledUsage,
        *,
        at: Instant,
        migration: bool = False,
        recalc_batch_id: str | None = None,
        ownership_at: Instant | None = None,
    ) -> WalletEntry | None:
        require_transaction(work, self.connection)
        event = billed.event
        ownership = self.connection.execute(
            "SELECT vendor_id FROM vendor_link WHERE group_id=? AND effective_at<=? "
            "ORDER BY effective_at DESC,seq DESC LIMIT 1",
            (billed.group_id, format_instant(ownership_at) if ownership_at else event.occurred_at),
        ).fetchone()
        vendor_id = ownership[0] if ownership else None
        if (
            vendor_id is None
            or self.connection.execute("SELECT 1 FROM vendor WHERE id=?", (vendor_id,)).fetchone() is None
        ):
            return None
        prior = self.connection.execute(
            "SELECT * FROM wallet_entry WHERE usage_harness=? AND usage_key=? AND kind='consumption'",
            (event.harness.value, key_bytes(event.dedupe_key)),
        ).fetchone()
        if prior is not None:
            return decode_entry(prior)
        entry = WalletEntry(
            str(uuid4()),
            vendor_id,
            "consumption",
            billed.cost.pico_usd,
            parse_instant(event.occurred_at),
            self.next_seq(),
            usage_ref=UsageRef(event.harness, event.dedupe_key),
            recalc_batch_id=recalc_batch_id,
            note="补价补记消费" if recalc_batch_id is not None else "",
            unpriced=billed.cost.unpriced,
            incomplete=billed.cost.incomplete,
        )
        self.append_with_offsets(work, entry, at=at, migration=migration)
        return entry

    def append_with_offsets(
        self, work: UnitOfWork, entry: WalletEntry, *, at: Instant, migration: bool = False
    ) -> WalletEntry:
        require_transaction(work, self.connection)
        prior = self.get_entry(entry.id)
        if prior is None and entry.kind in ("consumption", "consumption_delta"):
            from htd.adapters.sqlite.writer import UnitOfWork as SqlWork

            transaction = cast(SqlWork, work)
            stamp = format_instant(at)
            transaction.wallet_received[entry.vendor_id] = max(
                stamp, transaction.wallet_received.get(entry.vendor_id, stamp)
            )
        saved = self.append(work, entry)
        remote = self.connection.execute(
            "SELECT 1 FROM account_segment WHERE vendor_id=? AND kind='remote' AND ended_at IS NULL",
            (entry.vendor_id,),
        ).fetchone()
        if prior is None and remote is not None and entry.usage_ref is not None:
            from htd.adapters.sqlite.writer import UnitOfWork as SqlWork

            transaction = cast(SqlWork, work)
            key = entry.vendor_id, format_instant(entry.occurred_at)
            transaction.wallet_estimates[key] = transaction.wallet_estimates.get(key, 0) + entry.amount_pico
            # Only inbox members captured on restart can extend the sealed E snapshot.
            # Current-run tokens never enter an older batch, even with backdated usage.
            for batch in self.connection.execute(
                "SELECT b.id,b.estimate FROM account_pending_member m JOIN account_pending_batch b "
                "ON b.id=m.batch_id JOIN account_segment s ON s.id=b.segment_id "
                "WHERE m.harness=? AND m.dedupe_key=? AND s.vendor_id=?",
                (entry.usage_ref.harness.value, key_bytes(entry.usage_ref.dedupe_key), entry.vendor_id),
            ).fetchall():
                self.connection.execute(
                    "UPDATE account_pending_batch SET estimate=? WHERE id=?",
                    (str(int(batch["estimate"]) + entry.amount_pico), batch["id"]),
                )
            self.connection.execute(
                "UPDATE account_pending SET local_changed_at=?,reviewed_at=NULL WHERE segment_id IN "
                "(SELECT id FROM account_segment WHERE vendor_id=? AND ended_at IS NULL)",
                (format_instant(at), entry.vendor_id),
            )
        if prior is None and remote is None:
            from htd.domain.wallet.model import Calibration

            anchor = self.connection.execute(
                "SELECT c.*,e.seq FROM calibration c JOIN wallet_entry e ON e.id=c.entry_id "
                "WHERE e.vendor_id=? AND c.at>=? AND e.seq<? ORDER BY e.seq DESC LIMIT 1",
                (entry.vendor_id, format_instant(entry.occurred_at), entry.seq),
            ).fetchone()
            calibrations = (
                ()
                if anchor is None
                else (
                    Calibration(
                        anchor["entry_id"],
                        entry.vendor_id,
                        parse_instant(anchor["at"]),
                        anchor["seq"],
                        int(anchor["target_pico"]),
                        int(anchor["before_pico"]),
                        anchor["watermark_seq"],
                    ),
                )
            )
            for offset in ledger.late_offsets(calibrations, entry, migration=migration):
                self.append(work, offset.record(entry_id=str(uuid4()), at=at, seq=self.next_seq()))
        return saved
