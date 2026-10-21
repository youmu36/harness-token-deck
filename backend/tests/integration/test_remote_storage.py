"""Remote observation caps, reference-only credentials, and local-wallet isolation."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.migrate import MIGRATIONS, migrate
from htd.adapters.sqlite.remote_repo import RemoteAccount, RemoteObservation, RemoteRepository
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.domain.common.time import Instant, format_instant

from backend.tests.integration.collection_helpers import AT
from backend.tests.integration.test_wallet_storage import vendor

ACCOUNT = RemoteAccount("account", "vendor", "https://example.test", "HarnessTokenDeck/remote/account")


@pytest.mark.parametrize("site_type", ["auto", "newapi", "sub2api"])
def test_site_type_is_persisted_across_restart(tmp_path: Path, site_type: str) -> None:
    path = tmp_path / "data.sqlite3"
    account = replace(
        ACCOUNT,
        site_type=site_type,
        auth_mode="access_token" if site_type == "newapi" else "api_key",
        user_id="42",
    )
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        writer.execute(lambda work: RemoteRepository(work.connection).save(work, account))
    with WriteExecutor(path) as writer:
        assert writer.execute(lambda work: RemoteRepository(work.connection).accounts()) == (account,)


def test_old_remote_account_migrates_to_auto_without_changing_identity_or_credentials(tmp_path: Path) -> None:
    old = tmp_path / "old-migrations"
    old.mkdir()
    for script in MIGRATIONS.glob("*.sql"):
        if script.name < "0009":
            (old / script.name).write_bytes(script.read_bytes())
    path = tmp_path / "data.sqlite3"
    migrate(path, migrations=old, now=AT)
    with closing(open_connection(path)) as db:
        db.execute(
            "INSERT INTO vendor VALUES (?,?,?,?,?)", ("vendor", "Shop", "active", format_instant(AT), 1)
        )
        db.execute("INSERT INTO wallet_total VALUES('vendor','0','0','0',0)")
        db.execute(
            "INSERT INTO remote_account VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ACCOUNT.id,
                ACCOUNT.vendor_id,
                ACCOUNT.adapter,
                ACCOUNT.base_url,
                ACCOUNT.auth_mode,
                ACCOUNT.user_id,
                ACCOUNT.credential_ref,
                None,
                ACCOUNT.refresh_minutes,
                int(ACCOUNT.enabled),
            ),
        )
    migrate(path, now=AT)
    with closing(open_connection(path)) as db:
        assert RemoteRepository(db).accounts() == (ACCOUNT,)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE remote_account SET site_type='unknown'")


def test_remote_restart_fifty_latest_out_of_order_and_no_wallet_mutation(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        writer.execute(lambda w: RemoteRepository(w.connection).save(w, ACCOUNT))
        before = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))

        def observations(work: UnitOfWork) -> None:
            repo = RemoteRepository(work.connection)
            for index in reversed(range(60)):
                repo.observe(
                    work,
                    RemoteObservation(
                        "account",
                        Instant(AT.value + timedelta(minutes=index)),
                        True,
                        "token_remaining",
                        amount_pico=index * 10**25,
                        raw_quota=str(index),
                    ),
                )

        writer.execute(observations)
        totals = writer.execute(lambda w: WalletRepository(w.connection).totals("vendor"))
        assert (totals.r, totals.c, totals.a, totals.b) == (before.r, before.c, before.a, before.b)
    with WriteExecutor(path) as writer:
        accounts = writer.execute(lambda w: RemoteRepository(w.connection).accounts())
        assert accounts == (ACCOUNT,)
        values = writer.execute(lambda w: RemoteRepository(w.connection).observations("account"))
        assert len(values) == 50 and values[0].raw_quota == "59" and values[-1].raw_quota == "10"
        assert values[0].amount_pico == 59 * 10**25


def test_failed_observation_keeps_success_and_redacts_error(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        writer.execute(lambda w: RemoteRepository(w.connection).save(w, ACCOUNT))
        success = RemoteObservation("account", AT, True, "account_remaining", amount_pico=1)
        failure = RemoteObservation(
            "account",
            Instant(AT.value + timedelta(minutes=1)),
            False,
            "account_remaining",
            error_code="auth_failed",
            error_message="Bearer abcdef sk-sensitive api_key=secret " + "x" * 250,
        )
        writer.execute(lambda w: RemoteRepository(w.connection).observe(w, success))
        writer.execute(lambda w: RemoteRepository(w.connection).observe(w, failure))
        writer.execute(lambda w: RemoteRepository(w.connection).observe(w, failure))
        assert (
            writer.execute(lambda w: RemoteRepository(w.connection).latest("account", successful_only=True))
            == success
        )
        latest = writer.execute(lambda w: RemoteRepository(w.connection).latest("account"))
        assert latest is not None and latest.error_message is not None
        assert len(latest.error_message) == 200
        assert (
            "abcdef" not in latest.error_message
            and "sk-sensitive" not in latest.error_message
            and "=secret" not in latest.error_message
        )
        with pytest.raises(ValueError, match="different"):
            writer.execute(
                lambda w: RemoteRepository(w.connection).observe(w, replace(failure, error_code="offline"))
            )


def test_remote_vendor_unique_delete_cascade_and_rollback(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        writer.execute(lambda w: RemoteRepository(w.connection).save(w, ACCOUNT))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            writer.execute(
                lambda w: RemoteRepository(w.connection).save(
                    w, replace(ACCOUNT, id="other", credential_ref="HarnessTokenDeck/remote/other")
                )
            )
        writer.execute(
            lambda w: RemoteRepository(w.connection).observe(
                w, RemoteObservation("account", AT, True, "unlimited")
            )
        )
        writer.execute(lambda w: RemoteRepository(w.connection).delete(w, "account"))
        assert writer.execute(lambda w: RemoteRepository(w.connection).observations("account")) == ()


def test_remote_cap_delete_failure_rolls_back_new_observation(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        writer.execute(vendor)
        writer.execute(lambda w: RemoteRepository(w.connection).save(w, ACCOUNT))

        def fill(work: UnitOfWork) -> None:
            repo = RemoteRepository(work.connection)
            for i in range(50):
                repo.observe(
                    work,
                    RemoteObservation("account", Instant(AT.value + timedelta(minutes=i)), True, "unlimited"),
                )

        writer.execute(fill)
        before = writer.execute(lambda w: RemoteRepository(w.connection).observations("account"))
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail BEFORE DELETE ON remote_observation "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            writer.execute(
                lambda w: RemoteRepository(w.connection).observe(
                    w,
                    RemoteObservation(
                        "account", Instant(AT.value + timedelta(minutes=60)), True, "unlimited"
                    ),
                )
            )
        assert writer.execute(lambda w: RemoteRepository(w.connection).observations("account")) == before


@pytest.mark.parametrize(
    "url",
    [
        "http://example.test",
        "https://user:secret@example.test",
        "https://example.test?api_key=secret",
        "file:///test",
        "https://example.test/#key",
    ],
)
def test_remote_account_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(ValueError):
        replace(ACCOUNT, base_url=url)


@pytest.mark.parametrize("kind", ["unlimited", "quota_units"])
def test_non_monetary_observation_rejects_amount(kind: str) -> None:
    with pytest.raises(ValueError):
        RemoteObservation("account", AT, True, kind, amount_pico=1)
