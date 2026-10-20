"""Short writer transactions remain independent of network waits."""

from htu.adapters.sqlite.account_book import AccountBook
from htu.adapters.sqlite.command_store import SqliteCommandStore, connection, read_snapshot
from htu.adapters.sqlite.remote_repo import RemoteObservation, RemoteRepository
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.application.ports import OutputChanged, UnitOfWork, WalletChanged
from htu.domain.common.time import Instant, format_instant
from htu.domain.remote.model import BalanceObservation, RemoteAccount


class SqliteRemoteStore(SqliteCommandStore):
    def accounts(self, *, active_only: bool = False) -> tuple[RemoteAccount, ...]:
        with read_snapshot(self.readers) as db:
            accounts = RemoteRepository(db).accounts()
            active = {r[0] for r in db.execute("SELECT id FROM vendor WHERE status='active'")}
            return tuple(a for a in accounts if not active_only or a.vendor_id in active)

    def observations(self, account_id: str) -> tuple[BalanceObservation, ...]:
        with read_snapshot(self.readers) as db:
            import json

            from htu.domain.common.time import parse_instant
            from htu.domain.remote.model import BalanceKind

            history = tuple(o.balance() for o in RemoteRepository(db).observations(account_id))
            if any(value.can_calibrate for value in history):
                return history
            account = next(
                (value for value in RemoteRepository(db).accounts() if value.id == account_id), None
            )
            segment = AccountBook(db).active(account.vendor_id) if account else None
            endpoint = (
                AccountBook(db).latest(segment["id"]) if segment and segment["kind"] == "remote" else None
            )
            if endpoint is None:
                return history
            currency, conversion, scope, original_currency, conversion_rule = json.loads(
                endpoint["unit_version"]
            )
            return (
                *history,
                BalanceObservation(
                    account_id,
                    parse_instant(endpoint["observed_at"]),
                    BalanceKind.ACCOUNT_REMAINING,
                    endpoint["source"],
                    currency,
                    int(endpoint["b"]),
                    quota_per_unit=conversion,
                    cumulative_used_pico=int(endpoint["u"]) if endpoint["u"] is not None else None,
                    usage_scope=scope,
                    original_currency=original_currency,
                    conversion_rule=conversion_rule,
                ),
            )

    def auth_failed(self, account_id: str) -> bool:
        with read_snapshot(self.readers) as db:
            return (
                db.execute(
                    "SELECT 1 FROM runtime_state WHERE key=?", ("remote_auth_failed:" + account_id,)
                ).fetchone()
                is not None
            )

    def save_account(
        self, work: UnitOfWork, account: RemoteAccount, *, clear: bool, at: Instant | None = None
    ) -> None:
        db = connection(work)
        previous = next((value for value in RemoteRepository(db).accounts() if value.id == account.id), None)
        identity_fields = ("base_url", "site_type", "auth_mode", "user_id", "quota_per_unit", "adapter")
        if previous is not None and any(
            getattr(previous, name) != getattr(account, name) for name in identity_fields
        ):
            book = AccountBook(db)
            segment = book.active(account.vendor_id)
            endpoint = book.latest(segment["id"]) if segment else None
            if segment:
                book.detach(
                    account.vendor_id,
                    format_instant(at)
                    if at
                    else endpoint["observed_at"]
                    if endpoint
                    else segment["started_at"],
                )
            work.touch("account")
        RemoteRepository(db).save(work, account)
        WalletRepository(db).set_site(work, account.vendor_id, account.base_url)
        if clear:
            db.execute("DELETE FROM remote_usage_hint WHERE account_id=?", (account.id,))
            # Historical account endpoints/intervals are independent of the UI poll cache.
            db.execute("DELETE FROM reconciliation_session WHERE account_id=?", (account.id,))
            db.execute("DELETE FROM remote_billing_drift WHERE account_id=?", (account.id,))
            db.execute("DELETE FROM remote_observation WHERE account_id=?", (account.id,))
            db.execute("DELETE FROM reconciliation_notice WHERE account_id=?", (account.id,))
            db.execute("DELETE FROM runtime_state WHERE key=?", ("remote_auth_failed:" + account.id,))
            work.touch("wallet")
        work.emit(WalletChanged((account.vendor_id,), work.revision("wallet")))

    def delete_account(self, work: UnitOfWork, account_id: str) -> None:
        db = connection(work)
        account = next((a for a in RemoteRepository(db).accounts() if a.id == account_id), None)
        if account is not None:
            latest = AccountBook(db).active(account.vendor_id)
            if latest is not None:
                endpoint = AccountBook(db).latest(latest["id"])
                stamp = endpoint["observed_at"] if endpoint else latest["started_at"]
                AccountBook(db).detach(account.vendor_id, stamp)
        RemoteRepository(db).delete(work, account_id)
        db.execute("DELETE FROM runtime_state WHERE key=?", ("remote_auth_failed:" + account_id,))

    def observe(self, work: UnitOfWork, observation: BalanceObservation) -> None:
        repo = RemoteRepository(connection(work))
        before = work.revision("wallet")
        from dataclasses import replace
        from datetime import timedelta

        from htu.domain.common.time import Instant

        latest = repo.latest(observation.account_id)
        cached = observation
        if latest is not None and observation.fetched_at <= latest.fetched_at:
            if observation == latest.balance():
                return
            cached = replace(
                observation, fetched_at=Instant(latest.fetched_at.value + timedelta(milliseconds=1))
            )
        repo.observe(work, RemoteObservation.from_balance(cached))
        if cached.error is None and (cached.key_today_pico is not None or cached.key_total_pico is not None):
            connection(work).execute(
                "INSERT INTO remote_usage_hint VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                "observed_at=excluded.observed_at,today_pico=excluded.today_pico,total_pico=excluded.total_pico",
                (
                    cached.account_id,
                    format_instant(cached.fetched_at),
                    str(cached.key_today_pico) if cached.key_today_pico is not None else None,
                    str(cached.key_total_pico) if cached.key_total_pico is not None else None,
                ),
            )
        if observation.error is not None and observation.error.code == "auth_failed":
            connection(work).execute(
                "INSERT INTO runtime_state VALUES (?, 'true') ON CONFLICT(key) DO NOTHING",
                ("remote_auth_failed:" + observation.account_id,),
            )
        account = next(a for a in repo.accounts() if a.id == observation.account_id)
        book = AccountBook(connection(work))
        book.observe(work, account, observation)
        if observation.can_calibrate:
            book.flush_due(work, observation.fetched_at)
        if before != work.revision("wallet"):
            work.emit(WalletChanged((account.vendor_id,), work.revision("wallet")))

    def notify(self, vendor_id: str) -> None:
        self.execute(lambda work: work.emit(WalletChanged((vendor_id,), work.revision("wallet"))))

    def flush_pending(self, at: Instant) -> None:
        from datetime import timedelta

        from htu.adapters.sqlite.token_activity import QUIET

        cutoff = format_instant(Instant(at.value - QUIET))
        from zoneinfo import ZoneInfo

        from htu.adapters.sqlite.wallet_closing import bounds, timezone

        with read_snapshot(self.readers) as db:
            today = at.value.astimezone(ZoneInfo(timezone(db))).date().isoformat()
            midnight = format_instant(bounds(today, timezone(db))[0])
            due = db.execute(
                "SELECT 1 FROM account_pending_batch b WHERE ready_at<=? AND NOT EXISTS "
                "(SELECT 1 FROM account_pending_member m WHERE m.batch_id=b.id) UNION ALL "
                "SELECT 1 FROM account_pending p JOIN account_segment s ON s.id=p.segment_id "
                "WHERE s.ended_at IS NULL AND p.local_changed_at<=? "
                "AND p.start_endpoint<>p.end_endpoint AND p.reviewed_at IS NULL UNION ALL "
                "SELECT 1 FROM output_pending WHERE last_received_at<=? UNION ALL "
                "SELECT 1 FROM output_live o LEFT JOIN token_activity t USING(harness,model_key) "
                "WHERE o.finalized=0 AND "
                "(MAX(o.last_received_at,COALESCE(t.last_received_at,o.last_received_at))<=? "
                "OR o.last_output_at<?) UNION ALL SELECT 1 FROM output_reference WHERE day<? LIMIT 1",
                (format_instant(at), cutoff, cutoff, cutoff, midnight, today),
            ).fetchone()
            if due is None:
                due = db.execute(
                    "SELECT 1 FROM wallet_flow_pending WHERE received_at<=? UNION ALL "
                    "SELECT 1 FROM wallet_flow WHERE posted_at<? LIMIT 1",
                    (cutoff, format_instant(Instant(at.value - timedelta(days=30)))),
                ).fetchone()
        if due is None:
            return

        def flush(work: UnitOfWork) -> None:
            db = connection(work)
            from htu.adapters.sqlite.output_speed import close_quiet

            if close_quiet(db, at):
                work.touch("output")
                work.emit(OutputChanged(work.revision("output")))
            before = work.revision("account")
            AccountBook(db).flush_due(work, at)
            from htu.adapters.sqlite.wallet_flows import flush as flush_flows

            flow_vendors = flush_flows(work, db, at)
            if work.revision("account") != before or flow_vendors:
                vendors = tuple(
                    sorted(
                        set(flow_vendors)
                        | {
                            row[0]
                            for row in db.execute(
                                "SELECT vendor_id FROM account_segment "
                                "WHERE kind='remote' AND ended_at IS NULL"
                            )
                        }
                    )
                )
                work.emit(WalletChanged(vendors, work.revision("wallet")))

        self.execute(flush)

    def begin_run(self, at: Instant) -> None:
        from htu.adapters.sqlite.output_speed import begin_run, close_quiet

        def seal(work: UnitOfWork) -> None:
            db = connection(work)
            AccountBook(db).begin_run()
            begin_run(db)
            if close_quiet(db, at):
                work.touch("output")
                work.emit(OutputChanged(work.revision("output")))

        self.execute(seal)
        self.flush_pending(at)

    def stage_credential(self, reference: str, account_id: str) -> None:
        self.execute(
            lambda work: connection(work).execute(
                "INSERT OR IGNORE INTO credential_reference_journal VALUES(?,?)", (reference, account_id)
            )
        )

    def orphan_credentials(self) -> tuple[str, ...]:
        with read_snapshot(self.readers) as db:
            return tuple(
                row[0]
                for row in db.execute(
                    "SELECT reference FROM credential_reference_journal WHERE reference NOT IN "
                    "(SELECT credential_ref FROM remote_account)"
                )
            )

    def forget_credential(self, reference: str) -> None:
        self.execute(
            lambda work: connection(work).execute(
                "DELETE FROM credential_reference_journal WHERE reference=? AND reference NOT IN "
                "(SELECT credential_ref FROM remote_account)",
                (reference,),
            )
        )
