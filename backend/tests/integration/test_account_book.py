"""Product assertions replacing the 2026-10-18 diagnostic reproductions."""

from dataclasses import replace
from pathlib import Path

import pytest
from htd.adapters.sqlite.account_book import AccountBook, AccountSummary
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.token_activity import QUIET
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant

from backend.tests.integration.test_remote_wallet import ACCOUNT, balance
from backend.tests.integration.test_wallet_storage import USD, consumption, vendor


def setup(
    writer: WriteExecutor, readers: ReadPool, initial: str = "1.26", used: int | None = None
) -> SqliteRemoteStore:
    writer.execute(lambda w: vendor(w, initial=initial))
    store = SqliteRemoteStore(writer, readers)
    store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
    observe(store, int(float(initial) * USD), 0, used)
    return store


def observe(store: SqliteRemoteStore, amount: int, minute: int, used: int | None = None) -> None:
    observation = replace(
        balance(amount, minute),
        cumulative_used_pico=used,
        usage_scope="newapi:user:used_quota:v1" if used is not None else None,
    )
    store.execute(lambda w: store.observe(w, observation))
    # These cases test completed intervals; quiet-period timing has its own integration suite.
    store.flush_pending(Instant(observation.fetched_at.value + QUIET))


def summary(writer: WriteExecutor) -> AccountSummary:
    return writer.execute(lambda w: AccountBook(w.connection).summary("vendor"))


@pytest.mark.parametrize("reliable", [False, True])
def test_recharge_five_and_consumption_are_separate_from_local_estimates(
    tmp_path: Path, reliable: bool
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, used=0 if reliable else None)
        writer.execute(lambda w: consumption(w, amount=2_130_000_000_000, at=balance(0, 1).fetched_at))
        observe(store, 4 * USD, 5, 2_260_000_000_000 if reliable else None)
        actual = summary(writer)
        assert actual["R"] == str(5 * USD)
        assert actual["C"] == str(2_260_000_000_000)
        assert actual["B"] == str(4 * USD)
        assert actual["localEstimatePico"] == str(2_130_000_000_000)
        assert ("remote_usage" if reliable else "balance_estimate") in actual["methods"]
        # Late local requests change the comparison only, never settled recharge/C/B.
        writer.execute(lambda w: consumption(w, index=2, amount=USD, at=balance(0, 2).fetched_at))
        late = summary(writer)
        assert [late[k] for k in "RCB"] == [actual[k] for k in "RCB"]


def test_manual_funding_matched_once_and_remote_only_usage_progresses(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10")
        writer.execute(
            lambda w: WalletRepository(w.connection).top_up(
                w, "vendor", "3", at=balance(0, 1).fetched_at, operation_id="manual"
            )
        )
        observe(store, 13 * USD, 5)
        assert summary(writer)["R"] == str(3 * USD)
        assert summary(writer)["B"] == str(13 * USD)
        observe(store, 11 * USD, 10)
        assert summary(writer)["C"] == str(2 * USD)
        assert summary(writer)["B"] == str(11 * USD)


def test_one_dollar_and_two_identical_interval_recharges_are_valid(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10")
        observe(store, 11 * USD, 5)
        observe(store, 12 * USD, 10)
        assert summary(writer)["R"] == str(2 * USD)


def test_small_reliable_inflow_stays_intermediate_and_reset_starts_new_baseline(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10", used=USD)
        observe(store, 10 * USD + 123, 5, USD + 456)
        assert summary(writer)["R"] == "0"
        assert summary(writer)["C"] == "0"
        observe(store, 11 * USD, 10, 0)
        assert summary(writer)["C"] == "0"


def test_partial_reclassification_balance_neutral_and_idempotency_guard(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10")
        observe(store, 15 * USD, 5)
        entry = writer.execute(
            lambda w: w.connection.execute("SELECT id FROM account_posting WHERE kind='top_up'").fetchone()[0]
        )
        before = summary(writer)
        writer.execute(
            lambda w: AccountBook(w.connection).reclassify(
                w,
                "vendor",
                entry,
                2 * USD,
                "adjustment",
                "赠送",
                before["revision"],
                "classify",
                balance(0, 6).fetched_at,
            )
        )
        after = summary(writer)
        assert (
            after["R"] == str(3 * USD)
            and after["A"] == str(int(before["A"]) + 2 * USD)
            and after["B"] == before["B"]
        )
        with pytest.raises(DomainError, match="超过"):
            writer.execute(
                lambda w: AccountBook(w.connection).reclassify(
                    w,
                    "vendor",
                    entry,
                    4 * USD,
                    "adjustment",
                    "",
                    after["revision"],
                    "too-much",
                    balance(0, 7).fetched_at,
                )
            )
        observe(store, 15 * USD, 10)
        assert summary(writer)["R"] == str(3 * USD)
        writer.execute(
            lambda w: AccountBook(w.connection).correct_consumption(
                w, "vendor", USD, summary_revision(w), "", "correct-c", balance(0, 11).fetched_at
            )
        )
        assert summary(writer)["C"] == str(USD) and summary(writer)["B"] == before["B"]


def summary_revision(w: UnitOfWork) -> int:
    return w.revision("account")


def test_explicit_reconciliation_replaces_contribution_and_preserves_old_revision(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers)
        writer.execute(lambda w: consumption(w, amount=2_130_000_000_000, at=balance(0, 1).fetched_at))
        observe(store, 4 * USD, 5)
        interval = writer.execute(
            lambda w: w.connection.execute("SELECT id FROM account_interval").fetchone()[0]
        )
        writer.execute(lambda w: consumption(w, 2, amount=USD, at=balance(0, 2).fetched_at))
        assert summary(writer)["R"] == str(5 * USD)
        preview = writer.execute(lambda w: AccountBook(w.connection).preview_interval("vendor", interval))
        writer.execute(
            lambda w: AccountBook(w.connection).revise_interval(
                w,
                "vendor",
                interval,
                str(preview["fingerprint"]),
                w.revision("account"),
                "explicit-recheck",
                balance(0, 6).fetched_at,
            )
        )
        actual = summary(writer)
        assert (
            actual["R"] == str(6 * USD)
            and actual["C"] == str(3_260_000_000_000)
            and actual["B"] == str(4 * USD)
        )
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM account_interval_revision").fetchone()[0]
            )
            == 2
        )


def test_endpoint_evidence_outlives_fifty_poll_cache_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = setup(writer, readers, "10", used=0)
        for minute in range(5, 301, 5):
            observe(store, 10 * USD, minute, minute * 1000)
        before = summary(writer)
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT count(*) FROM account_endpoint").fetchone()[0]
            )
            == 61
        )
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT count(*) FROM remote_observation").fetchone()[0]
            )
            == 50
        )
    with WriteExecutor(path) as writer:
        assert summary(writer) == before
