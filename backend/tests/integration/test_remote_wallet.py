"""Remote balance increases become exact, atomic top-ups with a late-usage waterline."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.remote_repo import RemoteRepository
from htu.adapters.sqlite.remote_store import SqliteRemoteStore
from htu.adapters.sqlite.writer import WriteExecutor
from htu.domain.common.time import Instant
from htu.domain.remote.model import BalanceKind, BalanceObservation, RemoteAccount, RemoteFailure

from .test_wallet_storage import LATER, USD, vendor

ACCOUNT = RemoteAccount(
    "account", "vendor", "https://example.test", "HarnessTokenUsage/remote/account", site_type="sub2api"
)


def balance(amount: int = 20 * USD, minute: int = 0) -> BalanceObservation:
    return BalanceObservation(
        ACCOUNT.id,
        Instant(LATER.value + timedelta(minutes=minute)),
        BalanceKind.ACCOUNT_REMAINING,
        "example.test",
        "USD",
        amount,
    )


@pytest.mark.parametrize("initial", [None, "0", "10"])
def test_first_observation_is_baseline_and_retry_never_creates_recharge(
    tmp_path: Path, initial: str | None
) -> None:
    from htu.adapters.sqlite.account_book import AccountBook

    target = 20 * USD + 123_456_789_012
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(lambda w: vendor(w, initial=initial))
        store = SqliteRemoteStore(writer, readers)
        store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
        for _ in range(3):
            store.execute(lambda w: store.observe(w, balance(target)))
        actual = writer.execute(lambda w: AccountBook(w.connection).summary("vendor"))
        assert actual["B"] == str(target) and actual["R"] == "0"
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM account_endpoint").fetchone()[0]
            )
            == 1
        )


@pytest.mark.parametrize(
    "mode", ["failed", "stale", "uncertain", "non_usd", "quota", "unlimited", "subscription", "archived"]
)
def test_ineligible_observations_never_create_account_contributions(tmp_path: Path, mode: str) -> None:
    observation = balance()
    if mode == "failed":
        observation = replace(observation, amount_pico=None, error=RemoteFailure("offline", "离线"))
    elif mode == "stale":
        observation = replace(observation, stale=True)
    elif mode == "uncertain":
        observation = replace(observation, currency_uncertain=True)
    elif mode == "non_usd":
        observation = replace(observation, currency="CNY")
    elif mode in ("quota", "unlimited"):
        observation = replace(
            observation,
            kind=BalanceKind.QUOTA_UNITS if mode == "quota" else BalanceKind.UNLIMITED,
            amount_pico=None,
        )
    elif mode == "subscription":
        observation = replace(observation, kind=BalanceKind.SUBSCRIPTION_REMAINING)
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(vendor)
        store = SqliteRemoteStore(writer, readers)
        store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
        if mode == "archived":
            writer.execute(lambda w: w.connection.execute("UPDATE vendor SET status='archived'"))
        store.execute(lambda w: store.observe(w, observation))
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM account_endpoint").fetchone()[0]
            )
            == 0
        )


def test_observation_interval_postings_and_aggregate_roll_back_together(tmp_path: Path) -> None:
    from htu.adapters.sqlite.account_book import AccountBook

    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(vendor)
        store = SqliteRemoteStore(writer, readers)
        store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
        store.execute(lambda w: store.observe(w, balance(10 * USD)))
        before = writer.execute(lambda w: AccountBook(w.connection).summary("vendor"))
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER reject_interval BEFORE INSERT ON account_interval "
                "BEGIN SELECT RAISE(ABORT,'synthetic interval failure'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="synthetic interval failure"):
            store.execute(lambda w: store.observe(w, balance(15 * USD, 10)))
        assert writer.execute(lambda w: AccountBook(w.connection).summary("vendor")) == before
        assert (
            writer.execute(lambda w: RemoteRepository(w.connection).latest(ACCOUNT.id).amount_pico)
            == 10 * USD
        )
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER reject_interval"))
        store.execute(lambda w: store.observe(w, balance(15 * USD, 10)))
        assert writer.execute(lambda w: AccountBook(w.connection).summary("vendor"))["R"] == str(5 * USD)
