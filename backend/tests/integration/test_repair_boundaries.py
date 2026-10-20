"""Failure recovery, source quality, auxiliary costs and long-term publication."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.ingest_repo import IngestRepository
from htu.adapters.sqlite.remote_store import SqliteRemoteStore
from htu.adapters.sqlite.rollup_repo import RollupRepository
from htu.adapters.sqlite.title_repo import TitleRepository
from htu.adapters.sqlite.usage_daily import exact_summary
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import WriteExecutor
from htu.application.ports import TitleRequest
from htu.application.remote.ports import MemoryCredentialStore
from htu.application.remote.service import Remote
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, format_instant

from backend.tests.integration.collection_helpers import AT, collect, quote, usage
from backend.tests.integration.test_account_book import observe, setup, summary
from backend.tests.integration.test_history_rollup import seed
from backend.tests.integration.test_history_rollup import summary as local_summary
from backend.tests.integration.test_wallet_storage import USD, consumption, vendor
from backend.tests.unit.remote.test_refresh import Provider, settings


def test_receipt_failure_and_delete_failure_keep_old_credential_until_commit(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        writer.execute(vendor)
        clock = FakeClock(AT)
        credentials = MemoryCredentialStore()
        remote = Remote(SqliteRemoteStore(writer, readers), credentials, Provider(clock), clock)
        remote.save(settings(), "first")
        old = remote._account("vendor")
        assert old is not None
        secret = credentials.get(old.credential_ref)
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail_receipt BEFORE INSERT ON operation "
                "WHEN NEW.command IN ('remote.save','remote.delete') BEGIN SELECT "
                "RAISE(ABORT,'receipt failure'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            remote.save(settings(baseUrl="https://other.test", credential="synthetic-replacement"), "failed")
        assert remote._account("vendor") == old and credentials.get(old.credential_ref) == secret
        with pytest.raises(sqlite3.IntegrityError):
            remote.delete("vendor", "delete-failed")
        assert remote._account("vendor") == old and credentials.get(old.credential_ref) == secret
        assert remote.store.orphan_credentials()
        Remote(remote.store, credentials, Provider(clock), clock)
        assert not remote.store.orphan_credentials()
        assert credentials.get(old.credential_ref) == secret


def test_conflicting_identity_has_bounded_diagnostic_without_second_charge(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        event = usage(1)
        collect(writer, (event,))
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
        changed = replace(event, tokens=replace(event.tokens, output=20))
        for _ in range(3):
            collect(writer, (changed,))
        assert writer.execute(lambda w: w.connection.execute("SELECT output FROM ledger").fetchone()[0]) == 10
        assert (
            writer.execute(
                lambda w: w.connection.execute(
                    "SELECT COUNT(*) FROM diagnostic_log WHERE code='identity_conflict'"
                ).fetchone()[0]
            )
            == 1
        )


def test_title_cost_without_vendor_and_remote_consumption_does_not_double(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10", used=0)
        request = TitleRequest("f" * 64, Instant(AT.value + timedelta(minutes=1)), "title-model")
        writer.execute(lambda w: TitleRepository(w.connection).collect(w, (request,), at=request.occurred_at))
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT cost_pico FROM title_request").fetchone()[0]
            )
            == "1000000000"
        )
        observe(store, 10 * USD - 1_000_000_000, 5, 1_000_000_000)
        assert summary(writer)["C"] == "0"  # Sub-cent remote usage remains staged.
        observe(store, 10 * USD - USD // 100, 10, USD // 100)
        assert summary(writer)["C"] == str(USD // 100)  # The title's local E is not charged again.
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]) == 0
        )


def test_long_term_publication_is_exact_reversible_on_failure_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer:
        seed(writer)
        before = writer.execute(local_summary)
        later = Instant(AT.value + timedelta(days=160))
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail_publish BEFORE INSERT ON usage_daily "
                "BEGIN SELECT RAISE(ABORT,'disk failure'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later))
        assert writer.execute(local_summary) == before
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail_publish"))
        while writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later)):
            pass
        assert writer.execute(local_summary).metrics == before.metrics
        assert writer.execute(local_summary).history_notice
        assert writer.execute(local_summary).hourly == ()
        exact = writer.execute(
            lambda w: exact_summary(
                w.connection, format_instant(AT), format_instant(Instant(AT.value + timedelta(days=1))), "all"
            )
        )
        assert exact["totals"]["records"] == "100"
        assert exact["activeMinutes"] == "1"
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM usage_archive").fetchone()[0])
            == 0
        )
    with WriteExecutor(path) as writer:
        assert writer.execute(local_summary).metrics == before.metrics
        assert writer.execute(local_summary).history_notice
        assert writer.execute(local_summary).hourly == ()


def test_cold_only_finance_archives_vendor_instead_of_foreign_key_failure(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(vendor)
        writer.execute(lambda w: consumption(w, at=AT))
        later = Instant(AT.value + timedelta(days=100))
        while writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later)):
            pass
        assert (
            writer.execute(lambda w: WalletRepository(w.connection).delete_vendor(w, "vendor", at=later))
            == "archive"
        )


def test_large_wallet_validation_resumes_and_keeps_interleaved_funding(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        for index in range(501):
            writer.execute(lambda w, index=index: consumption(w, index, amount=USD, at=AT))
        writer.execute(lambda w: w.connection.execute("UPDATE wallet_total SET c='1'"))
    with WriteExecutor(path) as writer:
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_validation_job").fetchone()[0]
            )
            == 1
        )
        writer.execute(
            lambda w: WalletRepository(w.connection).top_up(
                w, "vendor", "3", at=AT, operation_id="between-batches"
            )
        )
        writer.execute(lambda w: WalletRepository(w.connection).validate_if_due(w, at=AT, force=True))
        totals = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        assert totals.r == 3 * USD and totals.c == 501 * USD
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_validation_job").fetchone()[0]
            )
            == 0
        )


def test_daily_authority_is_counted_once_across_cached_query_parts(tmp_path: Path) -> None:
    from htu.adapters.sqlite.statistics_store import SqliteStatisticsStore
    from htu.application.statistics import Statistics
    from htu.domain.statistics import Window

    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        statistics = Statistics(SqliteStatisticsStore(readers), FakeClock(AT))
        seed(writer)
        while writer.execute(
            lambda w: RollupRepository(w.connection).compact(w, at=Instant(AT.value + timedelta(days=160)))
        ):
            pass
        # The UTC midnight split crosses the same Asia/Shanghai business day.
        window = Window(
            Instant(AT.value - timedelta(days=4)), Instant(AT.value + timedelta(hours=3)), 345, "hour"
        )
        for _ in range(2):
            result, _ = statistics.summary("all", window)
            assert result.metrics.record_count == 100
            assert result.history_notice and result.hourly == result.hourly_totals == result.trend == ()
        assert statistics.long_term(window.start, window.end, "all")["totals"]["records"] == "100"


@pytest.mark.parametrize("revise_old", [False, True])
def test_account_projection_validation_resumes_without_publishing_partial_totals(
    tmp_path: Path, revise_old: bool
) -> None:
    from htu.adapters.sqlite.account_validation import step

    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10", used=0)
        amount = USD // 100  # Every interval must meet the current 0.01 posting threshold.
        for minute in range(1, 503):
            observe(store, 10 * USD - minute * amount, minute, minute * amount)
        writer.execute(lambda w: w.connection.execute("UPDATE account_segment SET c='1' WHERE kind='remote'"))
        writer.execute(lambda w: step(w.connection, w, at=AT, force=True))
        assert summary(writer)["C"] == "1"
        # Appended intervals preserve progress; a correction of frozen evidence restarts it.
        observe(store, 10 * USD - 503 * amount, 503, 503 * amount)
        assert summary(writer)["C"] == str(1 + amount)
        if revise_old:
            writer.execute(
                lambda w: w.connection.execute(
                    "UPDATE account_interval SET c=CAST(CAST(c AS INTEGER)+77 AS TEXT) "
                    "WHERE id=(SELECT id FROM account_interval ORDER BY id LIMIT 1)"
                )
            )
            writer.execute(lambda w: step(w.connection, w, at=AT, force=True))
            assert summary(writer)["C"] == str(1 + amount)
        writer.execute(lambda w: step(w.connection, w, at=AT, force=True))
        assert summary(writer)["C"] == str(503 * amount + (77 if revise_old else 0))
        assert (
            writer.execute(
                lambda w: w.connection.execute(
                    "SELECT COUNT(*) FROM runtime_state WHERE key='account_validation_job'"
                ).fetchone()[0]
            )
            == 0
        )


def test_fixed_conversion_evidence_survives_poll_cache_retirement_and_restart(tmp_path: Path) -> None:
    from htu.adapters.providers.deepseek import balance as deepseek_balance

    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = setup(writer, readers, "10")
        account = replace(
            store.accounts()[0],
            base_url="https://api.deepseek.com",
            site_type="deepseek",
            auth_mode="api_key",
            user_id=None,
        )
        store.execute(lambda w: store.save_account(w, account, clear=True, at=AT))
        for minute, value in ((1, "110"), (2, "109")):
            observation = deepseek_balance(
                {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": value}]},
                account.id,
                Instant(AT.value + timedelta(minutes=minute)),
                account.base_url,
            )
            store.execute(lambda w, observation=observation: store.observe(w, observation))
        store.flush_pending(Instant(AT.value + timedelta(minutes=12)))
        assert summary(writer)["C"] == str(USD)
        writer.execute(lambda w: w.connection.execute("DELETE FROM remote_observation"))
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        observed = SqliteRemoteStore(writer, readers).observations(account.id)[0]
        assert observed.amount_pico == 109 * USD
        assert observed.original_currency == "CNY" and observed.conversion_rule == "fixed-CNY-USD-1:1-v1"
