"""Released wallet values and vouchers seed both books exactly once."""

from datetime import timedelta
from pathlib import Path
from typing import Any, cast

from htu.adapters.sqlite.account_book import AccountBook
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.extensions_042 import install
from htu.adapters.sqlite.local_wallet import entries, reclassify, remote_summary, summary
from htu.adapters.sqlite.wallet_book import read
from htu.adapters.sqlite.wallet_closing import close_days
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import WriteExecutor
from htu.bootstrap import Application
from htu.domain.common.time import Instant
from htu.ipc.dto.wallet import AccountEntriesResponse

from backend.tests.integration.collection_helpers import AT
from backend.tests.integration.test_account_book import observe, setup
from backend.tests.integration.test_wallet_storage import USD, consumption


def test_old_totals_and_vouchers_seed_both_books_and_resume_independently(tmp_path: Path) -> None:
    path = tmp_path / "wallet.db"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = setup(writer, readers, "10", used=0)
        writer.execute(
            lambda w: WalletRepository(w.connection).top_up(w, "vendor", "5", at=AT, operation_id="fund")
        )
        observe(store, 14 * USD, 5, USD)
        old = cast(dict[str, Any], writer.execute(lambda w: AccountBook(w.connection).summary("vendor")))
        old_entries = cast(
            list[dict[str, Any]],
            writer.execute(lambda w: AccountBook(w.connection).entries("vendor", None, 100))["entries"],
        )
        writer.execute(lambda w: w.connection.execute("DELETE FROM meta WHERE key='wallet_inheritance_042'"))
        writer.execute(lambda w: install(w.connection))
        for scope in ("local", "remote"):
            current = writer.execute(
                lambda w, scope=scope: (
                    summary(w.connection, "vendor")
                    if scope == "local"
                    else remote_summary(w.connection, "vendor")
                )
            )
            assert current and {k: current[k] for k in "RCAB"} == {k: old[k] for k in "RCAB"}
            page = cast(
                dict[str, Any],
                writer.execute(
                    lambda w, scope=scope: (
                        entries(w.connection, "vendor", None, 100)
                        if scope == "local"
                        else AccountBook(w.connection).entries("vendor", None, 100, remote_only=True)
                    )
                ),
            )
            AccountEntriesResponse.model_validate(page)
            assert {(r["id"], r["R"], r["C"], r["A"]) for r in page["entries"]} == {
                (r["id"], r["R"], r["C"], r["A"]) for r in old_entries
            }
        writer.execute(lambda w: consumption(w, amount=USD))
        local = writer.execute(lambda w: summary(w.connection, "vendor"))
        assert int(local["C"]) == int(old["C"]) + USD
        assert int(local["B"]) == int(old["B"]) - USD
        remote = writer.execute(lambda w: remote_summary(w.connection, "vendor"))
        assert remote and remote["C"] == old["C"]
        later = Instant(AT.value + timedelta(days=1))
        for _ in range(3):
            writer.execute(lambda w: close_days(w, w.connection, at=later))
        book = writer.execute(
            lambda w: read(w.connection, "vendor", "local", AT.value.strftime("%Y-%m"), later)
        )
        assert int(book["C"]) == sum(int(r["C"]) for r in old_entries) + USD
        saved = writer.execute(lambda w: entries(w.connection, "vendor", None, 100))
    with WriteExecutor(path) as writer:
        assert writer.execute(lambda w: summary(w.connection, "vendor")) == local
        assert writer.execute(lambda w: entries(w.connection, "vendor", None, 100)) == saved
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM wallet_inheritance").fetchone()[0]
            )
            == 1
        )


def test_inherited_negative_adjustment_can_be_classified_locally(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "wallet.db") as writer, ReadPool(writer.path) as readers:
        setup(writer, readers, "-5")
        writer.execute(lambda w: w.connection.execute("DELETE FROM meta WHERE key='wallet_inheritance_042'"))
        writer.execute(lambda w: install(w.connection))
        page = writer.execute(lambda w: entries(w.connection, "vendor", None, 100))
        original = next(r for r in page["entries"] if r["A"].startswith("-"))
        before = writer.execute(lambda w: summary(w.connection, "vendor"))
        writer.execute(
            lambda w: reclassify(
                w.connection,
                w,
                "vendor",
                {
                    "entryId": original["id"],
                    "amountPico": str(USD),
                    "targetKind": "consumption",
                    "expectedAccountRevision": w.revision("wallet"),
                },
                "classify",
                AT,
            )
        )
        after = writer.execute(lambda w: summary(w.connection, "vendor"))
        assert after["B"] == before["B"] and int(after["C"]) == int(before["C"]) + USD


def test_default_wallet_survives_restart_without_adding_an_unknown_legacy_preference(tmp_path: Path) -> None:
    from uuid import uuid4

    with Application(tmp_path / "data", start_scheduler=False) as app:
        app.preferences.update({"walletDefaultScope": "remote"}, app.preferences.get().revision, str(uuid4()))
        assert (
            app.writer.execute(
                lambda w: w.connection.execute(
                    "SELECT 1 FROM preference WHERE key='walletDefaultScope'"
                ).fetchone()
            )
            is None
        )
    with Application(tmp_path / "data", start_scheduler=False) as app:
        assert app.preferences.get().values["walletDefaultScope"] == "remote"
