"""Separate account contributions, immutable endpoints and balance-neutral corrections."""

import hashlib
import json
import sqlite3
from typing import TypedDict
from uuid import uuid4

from htd.adapters.sqlite.token_activity import QUIET, account_last, account_state
from htd.adapters.sqlite.wallet_archive import unpack
from htd.application.ports import UnitOfWork
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant
from htd.domain.remote.accounting import CENT, settle
from htd.domain.remote.model import BalanceObservation, RemoteAccount


class AccountSummary(TypedDict):
    R: str
    C: str
    A: str
    B: str
    localEstimatePico: str
    observedAt: str | None
    coverageStart: str | None
    methods: list[str]
    remote: bool
    revision: int


def _id(*values: str) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()


class AccountBook:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def local(self, vendor: str) -> tuple[int, int, int]:
        row = self.db.execute("SELECT r,c,a FROM wallet_total WHERE vendor_id=?", (vendor,)).fetchone()
        return tuple(int(row[key]) for key in ("r", "c", "a"))  # type: ignore[return-value]

    def active(self, vendor: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM account_segment WHERE vendor_id=? AND ended_at IS NULL", (vendor,)
        ).fetchone()

    def ensure_local(self, vendor: str, stamp: str) -> sqlite3.Row:
        segment = self.active(vendor)
        if segment is None:
            self.db.execute(
                "INSERT INTO account_segment(id,vendor_id,kind,identity,started_at,anchor_r,anchor_c,"
                "anchor_a,initial_balance) VALUES(?,?,'local','local-v1',?,'0','0','0','0')",
                (str(uuid4()), vendor, stamp),
            )
            segment = self.active(vendor)
        assert segment is not None
        return segment

    def close(self, vendor: str, stamp: str) -> int:
        segment = self.active(vendor)
        if segment is None:
            return 0
        if segment["kind"] != "remote":
            delta = tuple(
                value - int(segment["anchor_" + key])
                for key, value in zip("rca", self.local(vendor), strict=True)
            )
            self.db.execute(
                "UPDATE account_segment SET r=?,c=?,a=?,ended_at=? WHERE id=?",
                (*map(str, delta), stamp, segment["id"]),
            )
            return int(segment["initial_balance"]) + delta[0] - delta[1] + delta[2]
        endpoint = self.latest(segment["id"])
        balance = int(self.summary(vendor)["B"])
        self.begin_run(segment["id"])
        self.db.execute("UPDATE account_segment SET ended_at=? WHERE id=?", (stamp, segment["id"]))
        self.db.execute("DELETE FROM remote_wallet_pending_estimate WHERE segment_id=?", (segment["id"],))
        return balance if endpoint else int(segment["initial_balance"])

    def detach(self, vendor: str, stamp: str) -> None:
        segment = self.active(vendor)
        if segment is None or segment["kind"] != "remote":
            return
        balance = self.close(vendor, stamp)
        self.db.execute(
            "INSERT INTO account_segment(id,vendor_id,kind,identity,started_at,anchor_r,anchor_c,anchor_a,"
            "initial_balance) VALUES(?,?,'local','local-v1',?,?,?,?,?)",
            (str(uuid4()), vendor, stamp, *map(str, self.local(vendor)), str(balance)),
        )

    def latest(self, segment: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM account_endpoint WHERE segment_id=? ORDER BY observed_at DESC LIMIT 1", (segment,)
        ).fetchone()

    def endpoint(self, segment: str, observation: BalanceObservation, unit: str) -> sqlite3.Row:
        stamp = format_instant(observation.fetched_at)
        identity = _id(segment, stamp)
        self.db.execute(
            "INSERT OR IGNORE INTO account_endpoint VALUES(?,?,?,?,?,?,?)",
            (
                identity,
                segment,
                stamp,
                str(observation.amount_pico),
                str(observation.cumulative_used_pico)
                if observation.cumulative_used_pico is not None
                else None,
                unit,
                observation.source,
            ),
        )
        return self.db.execute("SELECT * FROM account_endpoint WHERE id=?", (identity,)).fetchone()

    def estimate(self, vendor: str, start: str, end: str) -> int:
        # Recent endpoint intervals use indexed hot vouchers. Cold minute evidence is
        # read only for the intersecting archived days, one <=500-row block at a time.
        total = sum(
            int(row[0])
            for row in self.db.execute(
                "SELECT amount_pico FROM wallet_entry WHERE vendor_id=? AND occurred_at>? AND occurred_at<=? "
                "AND kind IN ('consumption','consumption_delta')",
                (vendor, start, end),
            )
        )
        for row in self.db.execute(
            "SELECT consumption FROM wallet_rollup WHERE vendor_id=? AND bucket>? AND bucket<=?",
            (vendor, start, end),
        ):
            total += int(row[0])
        for block in self.db.execute(
            "SELECT * FROM wallet_archive WHERE vendor_id=? AND end_at>? AND start_at<=? AND rollup_count>0",
            (vendor, start, end),
        ):
            total += sum(
                int(row["consumption"]) for row in unpack(block)["rollup"] if start < row["bucket"] <= end
            )
        return total

    def post(
        self,
        work: UnitOfWork,
        *,
        vendor: str,
        segment: str | None,
        interval: str | None,
        at: str,
        kind: str,
        r: int = 0,
        c: int = 0,
        a: int = 0,
        note: str,
        operation: str,
        original: str | None = None,
        received: Instant | None = None,
    ) -> str:
        identity = _id(operation)
        if not (r or c or a):
            return identity
        self.db.execute(
            "INSERT INTO account_posting(id,vendor_id,segment_id,interval_id,o"
            "ccurred_at,posted_at,kind,r,c,a,"
            "note,original_id,operation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                vendor,
                segment,
                interval,
                at,
                at,
                kind,
                str(r),
                str(c),
                str(a),
                note,
                original,
                operation,
            ),
        )
        remote = (
            segment is not None
            and self.db.execute(
                "SELECT 1 FROM account_segment WHERE id=? AND kind='remote'",
                (segment,),
            ).fetchone()
            is not None
        )
        if kind in ("correction", "consumption_correction", "balance_correction"):
            from htd.adapters.sqlite.wallet_flows import correction_total

            correction_total(self.db, vendor, r, c, a, remote=remote)
        if remote:
            from htd.adapters.sqlite.wallet_flows import restate_source, stage

            received_stamp = format_instant(received) if received else at
            if interval and kind in ("top_up", "consumption"):
                # Settlement already checked local quietness. Remote polling must
                # not introduce another ten-minute wait for the journal projection.
                pending = self.db.execute(
                    "SELECT local_changed_at FROM account_pending WHERE segment_id=?", (segment,)
                ).fetchone()
                received_stamp = (
                    pending[0]
                    if pending
                    else self.db.execute(
                        "SELECT e.observed_at FROM account_interval i JOIN account_endpoint e "
                        "ON e.id=i.start_endpoint WHERE i.id=?",
                        (interval,),
                    ).fetchone()[0]
                )
            flow_r, flow_c = r, c
            if kind == "interval_revision" and interval:
                for source_kind, prefix, delta in (
                    ("top_up", "interval-recharge:", r),
                    ("consumption", "interval-consumption:", c),
                ):
                    source = self.db.execute(
                        "SELECT rowid,occurred_at FROM account_posting WHERE id=?", (_id(prefix + interval),)
                    ).fetchone()
                    if (
                        source
                        and restate_source(
                            self.db,
                            vendor,
                            "remote",
                            source_kind,
                            source["rowid"],
                            source["occurred_at"],
                            delta,
                        )
                        == "pending"
                    ):
                        if source_kind == "top_up":
                            flow_r = 0
                        else:
                            flow_c = 0
            stage(
                self.db,
                vendor,
                "remote",
                "top_up"
                if kind in ("top_up", "manual_top_up")
                else "consumption"
                if kind == "consumption"
                else "adjustment",
                at,
                received_stamp,
                r=flow_r,
                c=flow_c,
                a=a,
                note=note,
                source_seq=self.db.execute(
                    "SELECT rowid FROM account_posting WHERE id=?", (identity,)
                ).fetchone()[0]
                if kind in ("top_up", "manual_top_up", "consumption")
                else 0,
            )
            if received:
                self.db.execute(
                    "UPDATE account_posting SET posted_at=? WHERE id=?", (format_instant(received), identity)
                )
        work.touch("account")
        work.touch("wallet")  # compatibility event; model statistics never depend on this scope.
        if kind == "manual_top_up" and segment is not None:
            self.db.execute("UPDATE account_pending SET reviewed_at=NULL WHERE segment_id=?", (segment,))
        return identity

    def observe(self, work: UnitOfWork, account: RemoteAccount, value: BalanceObservation) -> None:
        if (
            not value.can_calibrate
            or self.db.execute("SELECT status FROM vendor WHERE id=?", (account.vendor_id,)).fetchone()[0]
            != "active"
        ):
            return
        stamp = format_instant(value.fetched_at)
        unit = json.dumps(
            [
                value.currency,
                value.quota_per_unit,
                value.usage_scope,
                value.original_currency,
                value.conversion_rule,
            ],
            separators=(",", ":"),
        )
        identity = _id(
            account.id, account.base_url, account.site_type, account.auth_mode, account.user_id or "", unit
        )
        segment = self.ensure_local(account.vendor_id, stamp)
        previous = self.latest(segment["id"]) if segment["kind"] == "remote" else None
        reset = (
            previous is not None
            and previous["u"] is not None
            and value.cumulative_used_pico is not None
            and value.cumulative_used_pico < int(previous["u"])
        )
        if segment["kind"] != "remote" or segment["identity"] != identity or reset:
            self.close(account.vendor_id, stamp)
            segment_id = str(uuid4())
            self.db.execute(
                "INSERT INTO account_segment(id,vendor_id,kind,identity,started_at"
                ",anchor_r,anchor_c,anchor_a,"
                "initial_balance) VALUES(?,?,'remote',?,?,'0','0','0',?)",
                (segment_id, account.vendor_id, identity, stamp, str(value.amount_pico)),
            )
            baseline = self.endpoint(segment_id, value, unit)
            self.db.execute(
                "INSERT INTO account_pending VALUES(?,?,?,?,?,NULL)",
                (segment_id, baseline["id"], baseline["id"], stamp, stamp),
            )
            work.touch("account")
            work.touch("wallet")
            return
        assert previous is not None
        if stamp <= previous["observed_at"]:
            return
        current = self.endpoint(segment["id"], value, unit)
        changed = (previous["b"], previous["u"]) != (current["b"], current["u"])
        self.db.execute(
            "INSERT INTO account_pending VALUES(?,?,?,?,?,NULL) ON CONFLICT(segment_id) DO UPDATE SET "
            "end_endpoint=excluded.end_endpoint,changed_at=CASE WHEN ? THEN excluded.changed_at "
            "ELSE account_pending.changed_at END,reviewed_at=CASE WHEN ? THEN NULL "
            "ELSE account_pending.reviewed_at END",
            (segment["id"], previous["id"], current["id"], stamp, stamp, int(changed), int(changed)),
        )
        work.touch("account")
        work.touch("wallet")

        from htd.adapters.sqlite.wallet_pending import sync

        sync(self.db, segment["id"], work)

    def flush_due(self, work: UnitOfWork, at: Instant) -> None:
        for batch in self.db.execute(
            "SELECT * FROM account_pending_batch b WHERE ready_at<=? AND NOT EXISTS "
            "(SELECT 1 FROM account_pending_member m WHERE m.batch_id=b.id) ORDER BY ready_at,id LIMIT 200",
            (format_instant(at),),
        ).fetchall():
            segment = self.db.execute(
                "SELECT * FROM account_segment WHERE id=?", (batch["segment_id"],)
            ).fetchone()
            previous = self.db.execute(
                "SELECT * FROM account_endpoint WHERE id=?", (batch["start_endpoint"],)
            ).fetchone()
            current = self.db.execute(
                "SELECT * FROM account_endpoint WHERE id=?", (batch["end_endpoint"],)
            ).fetchone()
            self._settle(
                work,
                segment,
                previous,
                current,
                batch["account_id"],
                estimate_override=int(batch["estimate"]),
                watermark_override=batch["e_watermark"],
            )
            self.db.execute("DELETE FROM account_pending_batch WHERE id=?", (batch["id"],))
        cutoff = format_instant(Instant(at.value - QUIET))
        rows = self.db.execute(
            "SELECT p.*,s.vendor_id FROM account_pending p JOIN account_segment s ON s.id=p.segment_id "
            "JOIN vendor v ON v.id=s.vendor_id WHERE s.ended_at IS NULL AND v.status='active' "
            "AND p.local_changed_at<=? AND p.start_endpoint<>p.end_endpoint "
            "AND p.reviewed_at IS NULL",
            (cutoff,),
        ).fetchall()
        for pending in rows:
            last_token, busy = account_state(self.db, pending["vendor_id"])
            if busy or (last_token is not None and last_token > cutoff):
                continue
            previous = self.db.execute(
                "SELECT * FROM account_endpoint WHERE id=?", (pending["start_endpoint"],)
            ).fetchone()
            current = self.db.execute(
                "SELECT * FROM account_endpoint WHERE id=?", (pending["end_endpoint"],)
            ).fetchone()
            # Keep the original waterline while the aggregate movement is only noise.
            used = (
                int(current["u"]) - int(previous["u"])
                if current["u"] is not None and previous["u"] is not None
                else 0
            )
            manual = self.db.execute(
                "SELECT 1 FROM account_posting WHERE segment_id=? AND interval_id IS NULL "
                "AND kind='manual_top_up' AND occurred_at>? AND occurred_at<=? LIMIT 1",
                (pending["segment_id"], previous["observed_at"], current["observed_at"]),
            ).fetchone()
            if abs(int(current["b"]) - int(previous["b"])) < CENT and abs(used) < CENT and manual is None:
                self.db.execute(
                    "UPDATE account_pending SET reviewed_at=? WHERE segment_id=?",
                    (format_instant(at), pending["segment_id"]),
                )
                continue
            segment = self.db.execute(
                "SELECT * FROM account_segment WHERE id=?", (pending["segment_id"],)
            ).fetchone()
            account = self.db.execute(
                "SELECT id FROM remote_account WHERE vendor_id=?", (pending["vendor_id"],)
            ).fetchone()
            if account is None:
                continue
            self._settle(work, segment, previous, current, account["id"])
            self.db.execute(
                "UPDATE account_pending SET start_endpoint=end_endpoint,reviewed_at=? WHERE segment_id=?",
                (format_instant(at), segment["id"]),
            )
            from htd.adapters.sqlite.wallet_pending import sync

            sync(self.db, segment["id"], work)

    def _settle(
        self,
        work: UnitOfWork,
        segment: sqlite3.Row,
        previous: sqlite3.Row,
        current: sqlite3.Row,
        account_id: str,
        *,
        estimate_override: int | None = None,
        watermark_override: int | None = None,
    ) -> None:
        stamp = current["observed_at"]
        vendor_id = segment["vendor_id"]
        interval_id = _id(segment["id"], previous["id"], current["id"])
        estimate = (
            self.estimate(vendor_id, previous["observed_at"], stamp)
            if estimate_override is None
            else estimate_override
        )
        manual = list(
            self.db.execute(
                "SELECT * FROM account_posting WHERE segment_id=? AND interval_id IS NULL "
                "AND kind='manual_top_up' AND occurred_at>? AND occurred_at<=?",
                (segment["id"], previous["observed_at"], stamp),
            )
        )
        manual_r = sum(int(row["r"]) for row in manual)
        used = (
            int(current["u"]) - int(previous["u"])
            if current["u"] is not None and previous["u"] is not None
            else None
        )
        result = settle(int(previous["b"]), int(current["b"]), estimate, used=used, manual_r=manual_r)
        watermark = (
            self.db.execute("SELECT value FROM wallet_sequence WHERE id=1").fetchone()[0]
            if watermark_override is None
            else watermark_override
        )
        self.db.execute(
            "INSERT INTO account_interval(id,segment_id,start_endpoint,end_endpoint,e,e_watermark,r,c,a,"
            "raw_inflow,unresolved,method,algorithm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                interval_id,
                segment["id"],
                previous["id"],
                current["id"],
                str(estimate),
                watermark,
                str(result.recharge + manual_r),
                str(result.consumption),
                str(result.adjustment),
                str(result.candidate),
                str(result.unresolved),
                result.method,
                "account-quiet-v2",
            ),
        )
        methods = set(json.loads(segment["methods"]))
        self.db.execute(
            "INSERT INTO account_interval_revision VALUES(?,?,?,?)",
            (
                interval_id,
                1,
                json.dumps(
                    {
                        "R": str(result.recharge + manual_r),
                        "C": str(result.consumption),
                        "A": str(result.adjustment),
                        "E": str(estimate),
                        "method": result.method,
                        "watermark": watermark,
                        "rawInflow": str(result.candidate),
                        "unresolved": str(result.unresolved),
                    }
                ),
                stamp,
            ),
        )
        if (
            result.consumption >= 1_000_000_000_000
            and self.db.execute("SELECT 1 FROM remote_account WHERE id=?", (account_id,)).fetchone()
        ):
            self.db.execute(
                "INSERT INTO reconciliation_notice VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                "from_at=excluded.from_at,observed_at=excluded.observed_at,amount_pico=excluded.amount_pico",
                (account_id, vendor_id, previous["observed_at"], stamp, str(result.consumption)),
            )
        methods.add(result.method)
        self.db.execute(
            "UPDATE account_segment SET r=?,c=?,a=?,methods=? WHERE id=?",
            (
                str(int(segment["r"]) + result.recharge + manual_r),
                str(int(segment["c"]) + result.consumption),
                str(int(segment["a"]) + result.adjustment),
                json.dumps(sorted(methods)),
                segment["id"],
            ),
        )
        for row in manual:
            self.db.execute(
                "UPDATE account_posting SET matched=r,interval_id=? WHERE id=?", (interval_id, row["id"])
            )
        if result.recharge:
            self.post(
                work,
                vendor=vendor_id,
                segment=segment["id"],
                interval=interval_id,
                at=stamp,
                kind="top_up",
                r=result.recharge,
                note="区间充值（供应商核算）"  # noqa: RUF001
                if used is not None
                else "区间充值（余额与本地估算推算，向上取整）",  # noqa: RUF001
                operation="interval-recharge:" + interval_id,
            )
        self.post(
            work,
            vendor=vendor_id,
            segment=segment["id"],
            interval=interval_id,
            at=stamp,
            kind="consumption",
            c=result.consumption,
            a=result.adjustment,
            note="区间消费（供应商累计增量）"  # noqa: RUF001
            if used is not None
            else "区间消费（余额推算）" + ("；差额待核对" if result.unresolved else ""),  # noqa: RUF001
            operation="interval-consumption:" + interval_id,
        )
        # Interval contributions already include manual/automatic postings. Postings
        # are the immutable audit projection, not a second source of the same amount.
        work.touch("account")
        work.touch("wallet")

    def begin_run(self, segment_id: str | None = None) -> None:
        # Seal prior-run endpoints and E before collection can admit new tokens.
        # The new live waterline advances independently; each sealed range is unique.
        for pending in self.db.execute(
            "SELECT p.*,s.vendor_id,r.id account_id FROM account_pending p "
            "JOIN account_segment s ON s.id=p.segment_id JOIN remote_account r ON r.vendor_id=s.vendor_id "
            "WHERE s.ended_at IS NULL AND p.start_endpoint<>p.end_endpoint AND p.reviewed_at IS NULL "
            "AND (? IS NULL OR s.id=?)",
            (segment_id, segment_id),
        ).fetchall():
            previous = self.db.execute(
                "SELECT * FROM account_endpoint WHERE id=?", (pending["start_endpoint"],)
            ).fetchone()
            current = self.db.execute(
                "SELECT * FROM account_endpoint WHERE id=?", (pending["end_endpoint"],)
            ).fetchone()
            used = (
                int(current["u"]) - int(previous["u"])
                if current["u"] is not None and previous["u"] is not None
                else 0
            )
            manual = self.db.execute(
                "SELECT 1 FROM account_posting WHERE segment_id=? AND interval_id IS NULL "
                "AND kind='manual_top_up' AND occurred_at>? AND occurred_at<=? LIMIT 1",
                (pending["segment_id"], previous["observed_at"], current["observed_at"]),
            ).fetchone()
            if abs(int(current["b"]) - int(previous["b"])) < CENT and abs(used) < CENT and manual is None:
                continue
            last = max(
                pending["local_changed_at"],
                account_last(self.db, pending["vendor_id"]) or pending["local_changed_at"],
            )
            from htd.domain.common.time import parse_instant

            ready = format_instant(Instant(parse_instant(last).value + QUIET))
            self.db.execute(
                "INSERT OR IGNORE INTO account_pending_batch VALUES(?,?,?,?,?,?,?,?)",
                (
                    _id(pending["segment_id"], previous["id"], current["id"]),
                    pending["segment_id"],
                    pending["account_id"],
                    previous["id"],
                    current["id"],
                    ready,
                    str(self.estimate(pending["vendor_id"], previous["observed_at"], current["observed_at"])),
                    self.db.execute("SELECT value FROM wallet_sequence WHERE id=1").fetchone()[0],
                ),
            )
            batch_id = _id(pending["segment_id"], previous["id"], current["id"])
            self.db.execute(
                "INSERT OR IGNORE INTO account_pending_member SELECT i.harness,i.dedupe_key,? FROM inbox i "
                "WHERE i.occurred_at>? AND i.occurred_at<=? AND i.harness IN "
                "(SELECT h.harness FROM harness_selection h JOIN vendor_link v ON v.group_id=h.group_id "
                "WHERE v.vendor_id=? AND h.seq=(SELECT seq FROM harness_selection WHERE harness=h.harness "
                "ORDER BY effective_at DESC,seq DESC LIMIT 1) AND v.seq=(SELECT seq FROM vendor_link "
                "WHERE group_id=v.group_id ORDER BY effective_at DESC,seq DESC LIMIT 1))",
                (batch_id, previous["observed_at"], current["observed_at"], pending["vendor_id"]),
            )
            self.db.execute(
                "UPDATE account_pending SET start_endpoint=end_endpoint WHERE segment_id=?",
                (pending["segment_id"],),
            )

    def summary(self, vendor: str) -> AccountSummary:
        r = c = a = 0
        methods: set[str] = set()
        segments = list(
            self.db.execute(
                "SELECT * FROM account_segment WHERE vendor_id=? ORDER BY started_at,id", (vendor,)
            )
        )
        for segment in segments:
            if segment["kind"] == "remote":
                r += int(segment["r"])
                c += int(segment["c"])
                a += int(segment["a"])
                methods.update(json.loads(segment["methods"]))
            elif segment["ended_at"] is None:
                delta = tuple(
                    value - int(segment["anchor_" + key])
                    for key, value in zip("rca", self.local(vendor), strict=True)
                )
                r += delta[0]
                c += delta[1]
                a += delta[2]
                methods.add("legacy_local" if segment["kind"] == "legacy" else "local_estimate")
            else:
                r += int(segment["r"])
                c += int(segment["c"])
                a += int(segment["a"])
                methods.add("legacy_local" if segment["kind"] == "legacy" else "local_estimate")
        # Current local vendor created after the migration has no segment until first binding.
        if not segments:
            r, c, a = self.local(vendor)
            methods.add("local_estimate")
        for row in self.db.execute(
            "SELECT r,c,a FROM account_correction_total WHERE vendor_id=? UNION ALL "
            "SELECT p.r,p.c,p.a FROM account_posting p WHERE p.vendor_id=? "
            "AND p.kind='manual_top_up' AND p.interval_id IS NULL",
            (vendor, vendor),
        ):
            r += int(row["r"])
            c += int(row["c"])
            a += int(row["a"])
        active = self.active(vendor)
        endpoint = self.latest(active["id"]) if active and active["kind"] == "remote" else None
        if endpoint:
            balance = int(endpoint["b"])
            override = self.db.execute(
                "SELECT * FROM account_balance_override WHERE vendor_id=? AND endpoint_id=?",
                (vendor, endpoint["id"]),
            ).fetchone()
            if override:
                balance = int(override["balance"])
        elif active:
            delta = tuple(
                value - int(active["anchor_" + key])
                for key, value in zip("rca", self.local(vendor), strict=True)
            )
            balance = int(active["initial_balance"]) + delta[0] - delta[1] + delta[2]
        else:
            lr, lc, la = self.local(vendor)
            balance = lr - lc + la
        return {
            "R": str(r),
            "C": str(c),
            "A": str(a),
            "B": str(balance),
            "localEstimatePico": str(self.local(vendor)[1]),
            "observedAt": endpoint["observed_at"] if endpoint else None,
            "coverageStart": segments[0]["started_at"] if segments else None,
            "methods": sorted(methods),
            "remote": endpoint is not None,
            "revision": self.db.execute("SELECT value FROM revision WHERE scope='account'").fetchone()[0],
        }

    def reclassify(
        self,
        work: UnitOfWork,
        vendor: str,
        entry_id: str,
        amount: int,
        target: str,
        note: str,
        revision: int,
        operation: str,
        at: Instant,
    ) -> None:
        if revision != work.revision("account"):
            raise DomainError("ACCOUNT_REVISION_CONFLICT", "账户流水已变化，请刷新后重试")
        row = self.db.execute(
            "SELECT * FROM account_posting WHERE id=? AND vendor_id=?", (entry_id, vendor)
        ).fetchone()
        if row is None:
            # Legacy calibration vouchers remain hot; their original audit is retained.
            legacy = self.db.execute(
                "SELECT * FROM wallet_entry WHERE id=? AND vendor_id=? AND kind IN ('adjustment','top_up')",
                (entry_id, vendor),
            ).fetchone()
            if legacy is None:
                raise DomainError("ACCOUNT_ENTRY_INVALID", "原资金流水不存在或不支持改判")
            source = "top_up" if legacy["kind"] == "top_up" else "adjustment"
            original_amount = int(legacy["amount_pico"])
            segment = self.active(vendor)
            interval = None
        else:
            source = "top_up" if int(row["r"]) else "consumption" if int(row["c"]) else "adjustment"
            original_amount = int(
                row["r"] if source == "top_up" else row["c"] if source == "consumption" else row["a"]
            )
            segment = self.db.execute(
                "SELECT * FROM account_segment WHERE id=?", (row["segment_id"],)
            ).fetchone()
            interval = row["interval_id"]
        if target not in ("top_up", "adjustment", "consumption") or target == source or amount <= 0:
            raise DomainError("ACCOUNT_CLASSIFICATION_INVALID", "请选择不同类型和大于零的金额")
        used = sum(
            abs(int(item[0]))
            for item in self.db.execute(
                "SELECT matched FROM account_posting WHERE original_id=?", (entry_id,)
            )
        )
        if amount > abs(original_amount) - used:
            raise DomainError("ACCOUNT_AMOUNT_EXCEEDED", "改判金额超过原流水剩余可用额")
        sign = 1 if original_amount >= 0 else -1
        delta = {"r": 0, "c": 0, "a": 0}
        delta[{"top_up": "r", "consumption": "c", "adjustment": "a"}[source]] -= sign * amount
        # Balance-neutral transfer: R and A are inflows, C is an outflow.
        balance_effect = sign * amount * (-1 if source == "consumption" else 1)
        delta[{"top_up": "r", "consumption": "c", "adjustment": "a"}[target]] += (
            -balance_effect if target == "consumption" else balance_effect
        )
        before = self.summary(vendor)
        if int(before["R"]) + delta["r"] < 0 or int(before["C"]) + delta["c"] < 0:
            raise DomainError("ACCOUNT_AMOUNT_INVALID", "改判后累计充值或累计消费不能为负数")
        labels = {"top_up": "充值", "consumption": "消费", "adjustment": "校准"}
        remark = note or f"人工调整类型 {labels[source]}→{labels[target]}"
        remark += (
            f" (累计充值 {before['R']} → {int(before['R']) + delta['r']} pico，"
            f"累计消费 {before['C']} → {int(before['C']) + delta['c']} pico，余额不变)"
        )
        identity = self.post(
            work,
            vendor=vendor,
            segment=segment["id"] if segment else None,
            interval=interval,
            at=format_instant(at),
            kind="correction",
            r=delta["r"],
            c=delta["c"],
            a=delta["a"],
            note=remark,
            operation=operation,
            original=entry_id,
        )
        self.db.execute("UPDATE account_posting SET matched=? WHERE id=?", (str(amount), identity))

    def correct_consumption(
        self,
        work: UnitOfWork,
        vendor: str,
        target: int,
        revision: int,
        note: str,
        operation: str,
        at: Instant,
        *,
        remote_only: bool = False,
    ) -> None:
        if revision != work.revision("account"):
            raise DomainError("ACCOUNT_REVISION_CONFLICT", "账户累计已变化，请刷新后重试")
        if target < 0:
            raise DomainError("ACCOUNT_AMOUNT_INVALID", "累计消费不能为负数")
        if remote_only:
            from htd.adapters.sqlite.local_wallet import remote_summary

            remote = remote_summary(self.db, vendor)
            if remote is None:
                raise DomainError("REMOTE_ACCOUNT_REQUIRED", "请先配置远程账户")
            before = int(remote["C"])
        else:
            before = int(str(self.summary(vendor)["C"]))
        delta = target - before
        segment = self.active(vendor)
        self.post(
            work,
            vendor=vendor,
            segment=segment["id"] if segment else None,
            interval=None,
            at=format_instant(at),
            kind="consumption_correction",
            c=delta,
            a=delta,
            note=(note or "人工校准累计消费") + f"（{before} → {target} pico，余额不变）",  # noqa: RUF001
            operation=operation,
        )

    def calibrate_balance(
        self,
        work: UnitOfWork,
        vendor: str,
        target: int,
        operation: str,
        at: Instant,
        *,
        local_delta: int | None = None,
        remote_only: bool = False,
    ) -> None:
        summary = self.summary(vendor)
        before = int(str(summary["B"])) if local_delta is None else target - local_delta
        change = target - before
        consumption = int(str(summary["C"]))
        if remote_only:
            from htd.adapters.sqlite.local_wallet import remote_summary

            remote = remote_summary(self.db, vendor)
            if remote is None:
                raise DomainError("REMOTE_ACCOUNT_REQUIRED", "请先配置远程账户")
            consumption = int(remote["C"])
            before = int(remote["B"])
            change = target - before
        c = -min(max(0, consumption), change) if change > 0 else -change
        a = change + c
        if local_delta is not None:
            # Local legacy calibration already wrote A. Reclassify its effect to C
            # without applying the balance movement twice.
            a -= local_delta
        segment = self.active(vendor)
        self.post(
            work,
            vendor=vendor,
            segment=segment["id"] if segment else None,
            interval=None,
            at=format_instant(at),
            kind="balance_correction",
            c=c,
            a=a,
            note=f"余额校准 {before} → {target} pico；累计消费 {consumption} → {consumption + c} pico",  # noqa: RUF001
            operation=operation + ":account",
        )
        if segment and segment["kind"] == "remote":
            endpoint = self.latest(segment["id"])
            assert endpoint is not None
            self.db.execute(
                "INSERT INTO account_balance_override VALUES(?,?,?,?) ON CONFLICT(vendor_id) "
                "DO UPDATE SET endpoint_id=excluded.endpoint_id,balance=excluded.b"
                "alance,posted_at=excluded.posted_at",
                (vendor, endpoint["id"], str(target), format_instant(at)),
            )

    def entries(
        self, vendor: str, cursor: str | None, limit: int, *, remote_only: bool = False
    ) -> dict[str, object]:
        if remote_only:
            from htd.adapters.sqlite.wallet_inheritance import entries, seed

            if seed(self.db, vendor):
                return entries(self.db, vendor, cursor, limit, "remote")
        # Composite stable cursor, independent of timestamp ties and local voucher seq.
        values: list[object] = [vendor]
        clause = ""
        if cursor:
            try:
                stamp, identity = json.loads(cursor)
            except (ValueError, TypeError):
                raise DomainError("ACCOUNT_CURSOR_INVALID", "流水游标无效") from None
            clause = " AND (p.posted_at,p.id)<(?,?)"
            values += [stamp, identity]
        rows = list(
            self.db.execute(
                "WITH postings AS (SELECT * FROM account_posting UNION ALL "
                "SELECT w.id,w.vendor_id,NULL,NULL,w.occurred_at,w.occurred_at,w.kind,"
                "CASE WHEN w.kind='top_up' THEN w.amount_pico ELSE '0' END,'0',"
                "CASE WHEN w.kind='adjustment' THEN w.amount_pico ELSE '0' END,"
                "CASE WHEN w.note='calibration' THEN '原本地账余额校准' ELSE w.note END,NULL,"
                "COALESCE(w.operation_id,w.id),'0',1 FROM wallet_entry w "
                "WHERE w.kind IN ('top_up','adjustment') AND w.note<>'late_offset' "
                "AND NOT EXISTS(SELECT 1 FROM account_posting p WHERE p.operation_id='manual:'||w.id)) "
                "SELECT p.*,e1.observed_at from_at,e2.observed_at to_at FROM postings p "
                "LEFT JOIN account_interval i ON i.id=p.interval_id "
                "LEFT JOIN account_endpoint e1 ON e1.id=i.start_endpoint "
                "LEFT JOIN account_endpoint e2 ON e2.id=i.end_endpoint "
                "WHERE p.vendor_id=? AND (p.r<>'0' OR p.c<>'0' OR p.a<>'0')"
                + (
                    " AND p.segment_id IN (SELECT id FROM account_segment WHERE kind='remote')"
                    if remote_only
                    else ""
                )
                + clause
                + " ORDER BY p.posted_at DESC,p.id DESC LIMIT ?",
                (*values, limit + 1),
            )
        )
        items = [
            {
                "id": row["id"],
                "kind": row["kind"],
                "occurredAt": row["occurred_at"],
                "fromAt": row["from_at"],
                "toAt": row["to_at"],
                "postedAt": row["posted_at"],
                "R": row["r"],
                "C": row["c"],
                "A": row["a"],
                "note": row["note"],
                "originalId": row["original_id"],
                "intervalId": row["interval_id"],
                "availablePico": str(
                    max(
                        0,
                        max(abs(int(row[k])) for k in "rca")
                        - sum(
                            abs(int(x[0]))
                            for x in self.db.execute(
                                "SELECT matched FROM account_posting WHERE original_id=?", (row["id"],)
                            )
                        ),
                    )
                ),
            }
            for row in rows[:limit]
        ]
        return {
            "entries": items,
            "nextCursor": json.dumps([rows[limit - 1]["posted_at"], rows[limit - 1]["id"]])
            if len(rows) > limit
            else None,
            "revision": self.db.execute("SELECT value FROM revision WHERE scope='account'").fetchone()[0],
        }

    def preview_interval(self, vendor: str, identity: str) -> dict[str, object]:
        row = self.db.execute(
            "SELECT i.*,s.vendor_id FROM account_interval i JOIN account_segment s ON s.id=i.segment_id "
            "WHERE i.id=? AND s.vendor_id=?",
            (identity, vendor),
        ).fetchone()
        if row is None:
            raise DomainError("ACCOUNT_INTERVAL_INVALID", "账户区间不存在")
        start = self.db.execute(
            "SELECT * FROM account_endpoint WHERE id=?", (row["start_endpoint"],)
        ).fetchone()
        end = self.db.execute("SELECT * FROM account_endpoint WHERE id=?", (row["end_endpoint"],)).fetchone()
        estimate = self.estimate(vendor, start["observed_at"], end["observed_at"])
        manual = sum(
            int(item[0])
            for item in self.db.execute(
                "SELECT r FROM account_posting WHERE interval_id=? AND kind='manual_top_up'", (identity,)
            )
        )
        used = int(end["u"]) - int(start["u"]) if start["u"] is not None and end["u"] is not None else None
        result = settle(int(start["b"]), int(end["b"]), estimate, used=used, manual_r=manual)
        values = {
            "intervalId": identity,
            "fromAt": start["observed_at"],
            "toAt": end["observed_at"],
            "beforeR": row["r"],
            "beforeC": row["c"],
            "beforeA": row["a"],
            "localEstimatePico": str(estimate),
            "afterR": str(result.recharge + manual),
            "afterC": str(result.consumption),
            "afterA": str(result.adjustment),
            "method": result.method,
            "intervalRevision": row["revision"],
        }
        return dict(
            values,
            fingerprint=_id(json.dumps(values, sort_keys=True)),
            accountRevision=self.db.execute("SELECT value FROM revision WHERE scope='account'").fetchone()[0],
        )

    def revise_interval(
        self,
        work: UnitOfWork,
        vendor: str,
        identity: str,
        fingerprint: str,
        revision: int,
        operation: str,
        at: Instant,
    ) -> None:
        preview = self.preview_interval(vendor, identity)
        if work.revision("account") != revision or preview["fingerprint"] != fingerprint:
            raise DomainError("ACCOUNT_INTERVAL_CHANGED", "区间证据或账户已变化，请重新预览")
        row = self.db.execute("SELECT * FROM account_interval WHERE id=?", (identity,)).fetchone()
        segment = self.db.execute("SELECT * FROM account_segment WHERE id=?", (row["segment_id"],)).fetchone()
        old = {key: row[key.lower()] for key in "RCA"}
        delta = {key: int(str(preview["after" + key])) - int(old[key]) for key in "RCA"}
        stamp = format_instant(at)
        self.db.execute(
            "INSERT OR IGNORE INTO account_interval_revision VALUES(?,?,?,?)",
            (identity, row["revision"], json.dumps(dict(old, E=row["e"], method=row["method"])), stamp),
        )
        watermark = self.db.execute("SELECT value FROM wallet_sequence WHERE id=1").fetchone()[0]
        endpoints = list(
            self.db.execute(
                "SELECT b FROM account_endpoint WHERE id IN (?,?) ORDER BY observed_at",
                (row["start_endpoint"], row["end_endpoint"]),
            )
        )
        raw_inflow = (
            int(endpoints[1][0])
            - int(endpoints[0][0])
            + int(
                str(
                    preview["afterC"] if preview["method"] == "remote_usage" else preview["localEstimatePico"]
                )
            )
        )
        unresolved = str(preview["afterA"]) if preview["method"] == "balance_unresolved" else "0"
        self.db.execute(
            "INSERT INTO account_interval_revision VALUES(?,?,?,?)",
            (
                identity,
                row["revision"] + 1,
                json.dumps(
                    dict(preview, watermark=watermark, rawInflow=str(raw_inflow), unresolved=unresolved)
                ),
                stamp,
            ),
        )
        self.db.execute(
            "UPDATE account_interval SET "
            "r=?,c=?,a=?,e=?,e_watermark=?,revision=revision+1,method=?,raw_inflow=?,unresolved=? WHERE "
            "id=?",
            (
                preview["afterR"],
                preview["afterC"],
                preview["afterA"],
                preview["localEstimatePico"],
                watermark,
                preview["method"],
                str(raw_inflow),
                unresolved,
                identity,
            ),
        )
        self.db.execute(
            "UPDATE account_segment SET r=?,c=?,a=? WHERE id=?",
            (*(str(int(segment[key.lower()]) + delta[key]) for key in "RCA"), segment["id"]),
        )
        self.post(
            work,
            vendor=vendor,
            segment=segment["id"],
            interval=identity,
            at=stamp,
            kind="interval_revision",
            r=delta["R"],
            c=delta["C"],
            a=delta["A"],
            operation=operation,
            note=(
                f"显式重核区间；充值 {old['R']} → {preview['afterR']}，"  # noqa: RUF001
                f"消费 {old['C']} → {preview['afterC']} pico；远程原观测保留"  # noqa: RUF001
            ),
            original=_id("interval-consumption:" + identity),
        )
