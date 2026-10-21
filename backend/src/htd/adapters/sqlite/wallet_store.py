"""Wallet row codecs, append validation, and reconstructible materialized totals."""

import sqlite3
from dataclasses import dataclass
from typing import cast

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.pico_codec import decode_pico, encode_pico
from htd.adapters.sqlite.transaction import require_transaction
from htd.adapters.sqlite.wallet_activity import append_activity, initialize_activity
from htd.adapters.sqlite.wallet_archive import rollup_totals
from htd.application.ports import UnitOfWork
from htd.domain.common.errors import WALLET_INVALID, DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.model import Harness
from htd.domain.wallet.model import Calibration, EntryKind, Totals, UsageRef, WalletEntry


def decode_entry(row: sqlite3.Row) -> WalletEntry:
    return WalletEntry(
        row["id"],
        row["vendor_id"],
        cast(EntryKind, row["kind"]),
        decode_pico(row["amount_pico"]),
        parse_instant(row["occurred_at"]),
        row["seq"],
        row["operation_id"],
        UsageRef(Harness(row["usage_harness"]), row["usage_key"]) if row["usage_key"] is not None else None,
        row["related_entry_id"],
        row["recalc_batch_id"],
        row["note"],
        bool(row["unpriced"]),
        bool(row["incomplete"]),
    )


@dataclass(frozen=True, slots=True)
class EntryPage:
    entries: tuple[WalletEntry, ...]
    next_cursor: int | None


class WalletStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def next_seq(self) -> int:
        return int(self.connection.execute("SELECT value+1 FROM wallet_sequence WHERE id=1").fetchone()[0])

    def get_entry(self, entry_id: str) -> WalletEntry | None:
        row = self.connection.execute("SELECT * FROM wallet_entry WHERE id=?", (entry_id,)).fetchone()
        return None if row is None else decode_entry(row)

    def entries(self, vendor_id: str, *, cursor: int | None = None, limit: int = 100) -> EntryPage:
        if (
            type(limit) is not int
            or not 1 <= limit <= 500
            or (cursor is not None and (type(cursor) is not int or cursor < 1))
        ):
            raise ValueError("Invalid wallet page limit or cursor")
        rows = self.connection.execute(
            "SELECT * FROM wallet_entry WHERE vendor_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
            (vendor_id, cursor if cursor is not None else 2**63 - 1, limit + 1),
        ).fetchall()
        records = tuple(decode_entry(row) for row in rows[:limit])
        return EntryPage(records, records[-1].seq if len(rows) > limit else None)

    def calibrations(self, vendor_id: str) -> tuple[Calibration, ...]:
        return tuple(
            Calibration(
                row["entry_id"],
                vendor_id,
                parse_instant(row["at"]),
                row["seq"],
                decode_pico(row["target_pico"]),
                decode_pico(row["before_pico"]),
                row["watermark_seq"],
            )
            for row in self.connection.execute(
                "SELECT c.*,e.seq FROM calibration c JOIN wallet_entry e ON e.id=c.entry_id "
                "WHERE e.vendor_id=? ORDER BY e.seq",
                (vendor_id,),
            )
        )

    def totals(self, vendor_id: str) -> Totals:
        own_transaction = not self.connection.in_transaction
        if own_transaction:
            self.connection.execute("BEGIN")
        try:
            return self._totals(vendor_id)
        finally:
            if own_transaction:
                self.connection.rollback()

    def _totals(self, vendor_id: str) -> Totals:
        row = self.connection.execute("SELECT * FROM wallet_total WHERE vendor_id=?", (vendor_id,)).fetchone()
        if row is None:
            raise DomainError(WALLET_INVALID, "钱包累计缺失，请校验重建", "vendor_id")
        summary = self.connection.execute(
            "SELECT * FROM wallet_summary WHERE vendor_id=?",
            (vendor_id,),
        ).fetchone()
        if summary is None:
            raise DomainError(WALLET_INVALID, "钱包质量汇总缺失，请校验重建")
        latest = summary["calibrated_at"]
        revision = int(
            self.connection.execute("SELECT value FROM revision WHERE scope='wallet'").fetchone()[0]
        )
        return Totals(
            decode_pico(row["r"]),
            decode_pico(row["c"]),
            decode_pico(row["a"]),
            parse_instant(latest) if latest else None,
            revision,
            self.next_seq() - 1,
            summary["unpriced"],
            summary["incomplete"],
        )

    def append(
        self, work: UnitOfWork, entry: WalletEntry, *, calibration: Calibration | None = None
    ) -> WalletEntry:
        require_transaction(work, self.connection)
        if type(entry) is not WalletEntry:
            raise ValueError("Expected a validated WalletEntry")
        prior = self.get_entry(entry.id)
        if prior is not None:
            if prior != entry or (
                calibration is not None and calibration not in self.calibrations(entry.vendor_id)
            ):
                raise DomainError(WALLET_INVALID, "凭证标识已存在且内容不同")
            return prior
        if entry.seq != self.next_seq():
            raise DomainError(WALLET_INVALID, "钱包凭证序号须连续递增", "seq")
        if entry.note == "calibration":
            if calibration is None or (
                calibration.entry_id,
                calibration.vendor_id,
                calibration.at,
                calibration.seq,
                calibration.amount_pico,
            ) != (entry.id, entry.vendor_id, entry.occurred_at, entry.seq, entry.amount_pico):
                raise DomainError(WALLET_INVALID, "校准审计与凭证不一致")
            totals = self.totals(entry.vendor_id)
            if (calibration.observed_balance_pico, calibration.watermark_seq) != (
                totals.b,
                totals.watermark_seq,
            ):
                raise DomainError(WALLET_INVALID, "校准余额或水位已变化")
        elif calibration is not None:
            raise DomainError(WALLET_INVALID, "只有校准凭证可以写审计")
        if entry.related_entry_id is not None:
            related = self.get_entry(entry.related_entry_id)
            if related is None or related.vendor_id != entry.vendor_id:
                raise DomainError(WALLET_INVALID, "关联凭证缺失或供应商不同")
            if related.seq >= entry.seq or related.kind not in ("consumption", "consumption_delta"):
                raise DomainError(WALLET_INVALID, "关联消费顺序或类型无效")
            if entry.kind == "consumption_delta" and (
                related.kind != "consumption"
                or related.usage_ref != entry.usage_ref
                or related.occurred_at != entry.occurred_at
            ):
                raise DomainError(WALLET_INVALID, "重算差额须关联同一请求的原消费")
            if entry.note == "late_offset" and related.amount_pico != entry.amount_pico:
                raise DomainError(WALLET_INVALID, "抵消金额须与关联消费相等")
        # Rebuild can repair totals, but append must never compute from malformed materialization.
        total = self.connection.execute(
            "SELECT * FROM wallet_total WHERE vendor_id=?", (entry.vendor_id,)
        ).fetchone()
        if total is None:
            raise DomainError(WALLET_INVALID, "钱包累计缺失")
        r, c, a = (decode_pico(total[key]) for key in ("r", "c", "a"))
        if entry.kind == "top_up":
            r += entry.amount_pico
        elif entry.kind in ("consumption", "consumption_delta"):
            c += entry.amount_pico
        else:
            a += entry.amount_pico
        initialize_activity(self.connection)
        from htd.adapters.sqlite.wallet_closing import mark

        mark(self.connection, entry.vendor_id, format_instant(entry.occurred_at))
        ref = entry.usage_ref
        self.connection.execute(
            "INSERT INTO wallet_entry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry.id,
                entry.vendor_id,
                entry.kind,
                encode_pico(entry.amount_pico),
                format_instant(entry.occurred_at),
                entry.seq,
                entry.operation_id,
                ref.harness.value if ref else None,
                key_bytes(ref.dedupe_key) if ref else None,
                entry.related_entry_id,
                entry.recalc_batch_id,
                entry.note,
                int(entry.unpriced),
                int(entry.incomplete),
            ),
        )
        if calibration is not None:
            self.connection.execute(
                "UPDATE vendor_accounting SET balance_initialized=1 WHERE vendor_id=?", (entry.vendor_id,)
            )
            self.connection.execute(
                "INSERT INTO calibration VALUES (?,?,?,?,?)",
                (
                    calibration.entry_id,
                    encode_pico(calibration.target_pico),
                    encode_pico(calibration.observed_balance_pico),
                    calibration.watermark_seq,
                    format_instant(calibration.at),
                ),
            )
        self.connection.execute(
            "UPDATE wallet_total SET r=?,c=?,a=?,last_seq=? WHERE vendor_id=?",
            (encode_pico(r), encode_pico(c), encode_pico(a), entry.seq, entry.vendor_id),
        )
        append_activity(
            self.connection,
            self.connection.execute("SELECT * FROM wallet_entry WHERE id=?", (entry.id,)).fetchone(),
        )
        if (
            self.connection.execute(
                "SELECT 1 FROM account_segment WHERE vendor_id=? AND kind='remote' AND ended_at IS NULL",
                (entry.vendor_id,),
            ).fetchone()
            is None
        ):
            work.touch("account")
        from htd.adapters.sqlite.wallet_flows import stage

        absorbed = False
        delta_kind = "adjustment"
        source_seq = entry.seq if entry.kind in ("top_up", "consumption") else 0
        if entry.kind == "consumption_delta":
            from htd.adapters.sqlite.wallet_flows import reprice_local

            source = self.connection.execute(
                "SELECT seq FROM wallet_entry WHERE id=?", (entry.related_entry_id,)
            ).fetchone()
            state = reprice_local(
                self.connection,
                work,
                entry.vendor_id,
                source[0],
                format_instant(entry.occurred_at),
                entry.amount_pico,
                entry.id,
            )
            absorbed = state == "absorbed"
            delta_kind = state if state != "absorbed" else "adjustment"
            source_seq = source[0] if state == "consumption" else 0
        if entry.note == "late_offset":
            # Legacy vouchers retain their rollback semantics. The independent local
            # wallet always follows actual estimated consumption, including repricing.
            self.connection.execute(
                "INSERT INTO local_wallet_total VALUES(?,'0','0',?) ON CONFLICT(vendor_id) "
                "DO UPDATE SET a=htd_pico_add(a,excluded.a)",
                (entry.vendor_id, str(-entry.amount_pico)),
            )
        elif not absorbed:
            stage(
                self.connection,
                entry.vendor_id,
                "local",
                "top_up"
                if entry.kind == "top_up"
                else "consumption"
                if entry.kind == "consumption"
                else delta_kind,
                format_instant(entry.occurred_at),
                format_instant(entry.occurred_at),
                r=entry.amount_pico if entry.kind == "top_up" else 0,
                c=entry.amount_pico if entry.kind in ("consumption", "consumption_delta") else 0,
                a=entry.amount_pico if entry.kind == "adjustment" else 0,
                note=entry.note
                or (
                    "历史重算消费校准"
                    if entry.kind == "consumption_delta"
                    else "本地估算消费"
                    if ref
                    else "人工充值"
                ),
                work=work,
                source_seq=source_seq,
            )
        work.touch("wallet")
        return entry

    def validate_rebuild(self, work: UnitOfWork, *, at: Instant) -> tuple[str, ...]:
        require_transaction(work, self.connection)
        initialize_activity(self.connection)
        repaired: list[str] = []
        for vendor in self.connection.execute("SELECT id FROM vendor ORDER BY seq").fetchall():
            vendor_id = vendor[0]
            r = c = a = last_seq = unpriced = incomplete = 0
            calibrated_at = None
            for record in self.connection.execute(
                "SELECT e.*,NOT EXISTS(SELECT 1 FROM wallet_entry newer WHERE "
                "newer.usage_harness=e.usage_harness "
                "AND newer.usage_key=e.usage_key AND newer.seq>e.seq) latest FROM wallet_entry e "
                "WHERE e.vendor_id=? ORDER BY e.seq",
                (vendor_id,),
            ):
                entry = decode_entry(record)
                if entry.kind == "top_up":
                    r += entry.amount_pico
                elif entry.kind in ("consumption", "consumption_delta"):
                    c += entry.amount_pico
                    if record["latest"]:
                        unpriced += entry.unpriced
                        incomplete += entry.incomplete
                else:
                    a += entry.amount_pico
                    if entry.note == "calibration":
                        calibrated_at = max(calibrated_at or entry.occurred_at, entry.occurred_at)
                last_seq = max(last_seq, entry.seq)
            totals = Totals(
                r,
                c,
                a,
                calibrated_at,
                watermark_seq=last_seq,
                unpriced_count=unpriced,
                incomplete_count=incomplete,
            )
            cold_c = cold_a = cold_unpriced = cold_incomplete = cold_last = 0
            for counter in rollup_totals(self.connection, vendor_id):
                cold_c += int(counter["consumption"])
                cold_a += int(counter["adjustment"])
                cold_unpriced += counter["unpriced_count"]
                cold_incomplete += counter["incomplete_count"]
                cold_last = max(cold_last, counter["last_seq"])
            expected = (
                encode_pico(totals.r),
                encode_pico(totals.c + cold_c),
                encode_pico(totals.a + cold_a),
                max(totals.watermark_seq, cold_last),
            )
            row = self.connection.execute(
                "SELECT r,c,a,last_seq FROM wallet_total WHERE vendor_id=?", (vendor_id,)
            ).fetchone()
            legacy_at = self.connection.execute(
                "SELECT MAX(at) FROM vendor_legacy_record WHERE vendor_id=?", (vendor_id,)
            ).fetchone()[0]
            latest = max(
                filter(
                    None,
                    [
                        format_instant(totals.last_calibrated_at) if totals.last_calibrated_at else None,
                        legacy_at,
                    ],
                ),
                default=None,
            )
            summary_expected = (
                totals.unpriced_count + cold_unpriced,
                totals.incomplete_count + cold_incomplete,
                latest,
            )
            summary = self.connection.execute(
                "SELECT unpriced,incomplete,calibrated_at FROM wallet_summary WHERE vendor_id=?", (vendor_id,)
            ).fetchone()
            if (
                row is not None
                and tuple(row) == expected
                and summary is not None
                and tuple(summary) == summary_expected
            ):
                continue
            self.connection.execute(
                "INSERT INTO wallet_total VALUES (?,?,?,?,?) ON CONFLICT(vendor_id) "
                "DO UPDATE SET r=excluded.r,c=excluded.c,a=excluded.a,last_seq=excluded.last_seq",
                (vendor_id, *expected),
            )
            self.connection.execute(
                "INSERT INTO wallet_summary VALUES(?,?,?,?) ON CONFLICT(vendor_id) DO "
                "UPDATE SET unpriced=excluded.unpriced,incomplete=excluded.incomplete,"
                "calibrated_at=excluded.calibrated_at",
                (vendor_id, *summary_expected),
            )
            self.connection.execute(
                "INSERT INTO diagnostic_log VALUES(NULL,?,NULL,'wallet_totals_rebuilt',?)",
                (format_instant(at), "供应商 " + vendor_id[:100] + ": 累计与凭证不一致，已重建"),
            )
            repaired.append(vendor_id)
        if repaired:
            work.touch("wallet")
        self.connection.execute(
            "UPDATE wallet_sequence SET value=MAX(value,COALESCE((SELECT MAX(n) "
            "FROM (SELECT seq n FROM wallet_entry UNION ALL SELECT last_seq FROM "
            "wallet_total UNION ALL SELECT last_seq FROM wallet_rollup UNION ALL "
            "SELECT last_seq FROM wallet_archive UNION ALL SELECT watermark_seq "
            "FROM calibration UNION ALL SELECT seq FROM wallet_activity UNION ALL "
            "SELECT related_seq FROM wallet_activity)),0)) WHERE id=1"
        )
        self.connection.execute(
            "INSERT INTO runtime_state VALUES ('last_wallet_check_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (format_instant(at),),
        )
        return tuple(repaired)

    def validate_if_due(self, work: UnitOfWork, *, at: Instant, force: bool = False) -> tuple[str, ...]:
        """Check voucher totals at most once every 24 hours, including paused maintenance."""
        require_transaction(work, self.connection)
        from htd.adapters.sqlite.account_validation import step as account_step
        from htd.adapters.sqlite.wallet_validation import step

        return tuple(
            sorted(
                set(step(self.connection, work, at=at, force=force))
                | set(account_step(self.connection, work, at=at, force=force))
            )
        )
