"""Supplier-switch boundaries and persistent dismissal use synthetic local data."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.reconciliation import SqliteReconciliationStore
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.session_hit_rate import read, record
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.domain.common.time import Instant, format_instant
from htd.domain.pricing.model import HarnessSelection
from htd.domain.pricing.rules import create_group
from htd.domain.usage.model import Harness

from .collection_helpers import AT, collect, quote, usage
from .scan_helpers import seed_prices
from .test_remote_wallet import ACCOUNT
from .test_wallet_storage import vendor


def at(seconds: int) -> Instant:
    return Instant(AT.value + timedelta(seconds=seconds))


def switch(work: UnitOfWork) -> None:
    repo = PricingRepository(work.connection)
    old = repo.load()
    new = create_group(
        old, group_id="other", version_id="other-v", revision_id="other-r", name="Other", at=at(600)
    )
    new = replace(
        new,
        selections=(*new.selections, HarnessSelection(Harness.CODEX, "other", at(600), new.sequence + 1)),
    )
    repo.save(work, old, new)


@pytest.mark.parametrize(("gap", "expected"), [(299, 1), (300, 1), (301, 0), (600, 0)])
def test_quiet_supplier_switch_is_not_mistaken_for_a_current_mismatch(
    tmp_path: Path, gap: int, expected: int
) -> None:
    with WriteExecutor(tmp_path / "data.db") as writer, ReadPool(writer.path) as readers:
        writer.execute(seed_prices)
        writer.execute(vendor)
        remote = SqliteRemoteStore(writer, readers)
        remote.execute(lambda work: remote.save_account(work, ACCOUNT, clear=True))
        writer.execute(lambda work: WalletRepository(work.connection).link_group(work, "g", "vendor", at=AT))
        collect(writer, (usage(1, at=at(600 - gap), model="gpt-6-astra"),))
        writer.execute(
            lambda work: IngestRepository(work.connection).bill_batch(
                work,
                lambda event: replace(quote(event), group_id="g", version_id="v", revision_id="r"),
                at=AT,
            )
        )
        writer.execute(switch)
        collect(writer, (usage(2, at=at(630), model="gpt-6-astra"),), offset=2)
        writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=at(630)))
        writer.execute(
            lambda work: (
                work.connection.execute(
                    "INSERT INTO reconciliation_notice VALUES(?,?,?,?,?)",
                    (ACCOUNT.id, "vendor", format_instant(AT), format_instant(at(660)), str(10**12)),
                ).rowcount
            )
        )
        store = SqliteReconciliationStore(readers, writer)
        assert len(store.notices(at(660))) == expected

        def reselect_same_group(work: UnitOfWork) -> None:
            repo = PricingRepository(work.connection)
            old = repo.load()
            new = replace(
                old,
                selections=(
                    *old.selections,
                    HarnessSelection(Harness.CODEX, "other", at(700), old.sequence + 1),
                ),
                revision=old.revision + 1,
            )
            repo.save(work, old, new)

        writer.execute(reselect_same_group)
        assert len(store.notices(at(700))) == expected
        # A remote interval wholly before the switch cannot accuse the new group.
        writer.execute(
            lambda work: (
                work.connection.execute(
                    "UPDATE reconciliation_notice SET observed_at=?", (format_instant(at(599)),)
                ).rowcount
            )
        )
        assert store.notices(at(660)) == []


def test_dismissal_survives_restart_and_does_not_delete_a_newer_notice(tmp_path: Path) -> None:
    path = tmp_path / "data.db"
    old, new = format_instant(AT), format_instant(at(60))
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        writer.execute(vendor)
        remote = SqliteRemoteStore(writer, readers)
        remote.execute(lambda work: remote.save_account(work, ACCOUNT, clear=True))
        writer.execute(
            lambda work: (
                work.connection.execute(
                    "INSERT INTO reconciliation_notice VALUES(?,?,?,?,?)",
                    (ACCOUNT.id, "vendor", old, new, str(10**12)),
                ).rowcount
            )
        )
        store = SqliteReconciliationStore(readers, writer)
        store.dismiss("vendor", old)
        assert readers.get().execute("SELECT COUNT(*) FROM reconciliation_notice").fetchone()[0] == 1
        store.dismiss("vendor", new)
        store.dismiss("vendor", new)
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        assert readers.get().execute("SELECT COUNT(*) FROM reconciliation_notice").fetchone()[0] == 0


def test_session_order_and_obsolete_parallel_rows_are_cleaned_on_existing_writes(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.db") as writer, ReadPool(writer.path) as readers:
        writer.execute(lambda work: record(work.connection, usage(1, session="first", at=AT), AT))
        writer.execute(lambda work: record(work.connection, usage(2, session="second", at=at(60)), at(60)))
        writer.execute(lambda work: record(work.connection, usage(3, session="first", at=at(120)), at(120)))
        sessions = read(readers.get(), "codex", at(120))
        assert [row["startedAt"] for row in sessions] == [format_instant(AT), format_instant(at(60))]
        # On the minute clock the one still-active session is already the only card.
        assert len(read(readers.get(), "codex", at(661))) == 1
        writer.execute(lambda work: record(work.connection, usage(4, session="first", at=at(661)), at(661)))
        assert readers.get().execute("SELECT COUNT(*) FROM session_hit_rate").fetchone()[0] == 1
