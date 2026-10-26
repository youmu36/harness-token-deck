"""Real inbox billing, wallet attribution, commit events and crash recovery."""

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from htd.adapters.sqlite.collection_store import SqliteCollectionStore
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.application.collection.billing import BillInbox
from htd.application.ports import UsageChanged, WalletChanged
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant, parse_instant
from htd.domain.pricing.model import ModelRate, PriceRevision

from .scan_helpers import AT, count, headers, request, scanner, seed_prices, sessions


def test_transaction_b_wallet_failure_rolls_back_events_and_inbox(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    (sessions(home) / "one.jsonl").write_bytes(headers() + request(1, AT))
    published: list[object] = []
    observed: list[tuple[int, int]] = []

    def publish(event: object) -> None:
        with sqlite3.connect(path) as connection:
            observed.append(
                (
                    connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
                    connection.execute("SELECT COUNT(*) FROM wallet_entry").fetchone()[0],
                )
            )
        published.append(event)

    with WriteExecutor(path, publish=publish) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        writer.execute(
            lambda w: WalletRepository(w.connection).create_vendor(
                w, vendor_id="vendor", name="Shop", at=AT, operation_id="vendor"
            )
        )
        writer.execute(lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=AT))
        scan = scanner(writer, pool, home, FakeClock(AT))
        scan.collect()
        assert len(published) == 1 and isinstance(published[0], UsageChanged)
        published.clear()
        observed.clear()
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail BEFORE UPDATE ON wallet_total BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            scan.billing.run()
        assert count(writer, "ledger") == count(writer, "wallet_entry") == 0
        assert count(writer, "inbox") == 1 and not published
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail"))
        assert scan.billing.run().batch.billed == 1
        assert len(published) == 2
        assert isinstance(published[0], UsageChanged) and published[0].revision == 2
        assert isinstance(published[1], WalletChanged) and published[1].vendor_ids == ("vendor",)
        assert observed == [(1, 1), (1, 1)]
        # Publication observes the committed database, not an uncommitted wallet callback.
        assert count(writer, "ledger") == count(writer, "wallet_entry") == 1
        scan.run()
        assert len(published) == 2


def wallet_prices(work: UnitOfWork, at: Instant) -> None:
    seed_prices(work, at)
    repo = PricingRepository(work.connection)
    old = repo.load()
    rate = ModelRate("gpt-6-astra", "AsTra", "50000", "0", "0")
    new = replace(
        old,
        revisions=(*old.revisions, PriceRevision("r2", "v", at, old.sequence + 1, (rate,))),
        revision=old.revision + 1,
    )
    repo.save(work, old, new)


@pytest.mark.parametrize("late", [True, False])
def test_w07_w08_in_real_billing_chain(tmp_path: Path, late: bool) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    file = sessions(home) / "one.jsonl"
    file.write_bytes(headers())
    before = parse_instant("2026-09-01T09:58:00Z")
    event_at = parse_instant("2026-09-01T09:59:00Z")
    link_at = parse_instant("2026-09-01T10:00:00Z")
    after = parse_instant("2026-09-01T10:01:00Z")
    clock = FakeClock(before)
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(lambda w: wallet_prices(w, before))
        writer.execute(
            lambda w: WalletRepository(w.connection).create_vendor(
                w, vendor_id="vendor", name="Shop", at=before, operation_id="vendor"
            )
        )
        writer.execute(
            lambda w: WalletRepository(w.connection).link_group(
                w, "g", "vendor", at=before if late else link_at
            )
        )
        scan = scanner(writer, pool, home, clock)
        scan.run()  # Establish the initial boundary before either event completes.
        clock.set_wall(link_at)
        if late:
            revision = writer.execute(lambda w: w.revision("wallet"))
            writer.execute(
                lambda w: WalletRepository(w.connection).calibrate(
                    w, "vendor", "50", expected_revision=revision, at=link_at, operation_id="calibrate"
                )
            )
        file.write_bytes(headers() + request(1, event_at) + (b"" if late else request(2, after)))
        clock.set_wall(after)
        result = scan.run()
        assert result.billing.batch.billed == (1 if late else 2)
        totals = WalletRepository(pool.get()).totals("vendor")
        assert totals.c == 3 * 10**12
        if late:
            assert totals.b == 50 * 10**12
            entries = WalletRepository(pool.get()).entries("vendor").entries
            offsets = [entry for entry in entries if entry.note == "late_offset"]
            assert len(offsets) == 1 and offsets[0].amount_pico == 3 * 10**12
        else:
            assert totals.b == -3 * 10**12


def test_process_objects_discarded_after_a_before_b_wallet_recovers_once(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    (sessions(home) / "one.jsonl").write_bytes(headers() + request(1, AT) + request(2, AT))
    clock = FakeClock(AT)
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        writer.execute(
            lambda w: WalletRepository(w.connection).create_vendor(
                w, vendor_id="vendor", name="Shop", at=AT, operation_id="vendor"
            )
        )
        writer.execute(lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=AT))
        assert scanner(writer, pool, home, clock).collect()[0].inserted == 2
        assert count(writer, "ledger") == count(writer, "wallet_entry") == 0
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        # Bill the saved inbox directly without rescanning any source.
        billing = BillInbox(SqliteCollectionStore(writer, pool), clock)
        assert billing.run().batch.billed == 2
        assert scanner(writer, pool, home, clock).run().billing.batch.billed == 0
        assert count(writer, "ledger") == count(writer, "wallet_entry") == 2
        assert WalletRepository(pool.get()).totals("vendor").c == 2 * 84_000_000
