"""Even legacy matching settings cannot override explicitly selected pricing."""

from dataclasses import replace
from pathlib import Path

import pytest
from htu.adapters.sqlite.collection_store import SqliteCollectionStore
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.application.collection.billing import BillInbox
from htu.domain.common.testing import FakeClock
from htu.domain.pricing import rules
from htu.domain.pricing.model import ModelRate
from htu.domain.usage.model import Harness

from .collection_helpers import AT, collect, usage


@pytest.mark.parametrize(
    ("enabled", "name", "expected"),
    [
        (False, "Shop", "wrong"),
        (True, " shop ", "wrong"),
        (True, "unmatched", "wrong"),
        (True, "remote-identity", "wrong"),
    ],
)
def test_retired_matching_never_changes_selected_group(
    tmp_path: Path, enabled: bool, name: str, expected: str
) -> None:
    path = tmp_path / "ledger.sqlite3"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:

        def seed(work: UnitOfWork) -> None:
            pricing = PricingRepository(work.connection)
            for group in ("wrong", "correct"):
                old = pricing.load()
                new = rules.create_group(
                    old, group_id=group, version_id=group + "v", revision_id=group + "r", name=group, at=AT
                )
                pricing.save(work, old, new)
                old = pricing.load()
                new = rules.save_version_prices(
                    old,
                    group + "v",
                    (ModelRate("Model", "Model", "1", "1", "1"),),
                    revision_id=group + "r2",
                    expected_revision=old.revision,
                    at=AT,
                )
                pricing.save(work, old, new)
            old = pricing.load()
            plan = rules.plan_switch(
                old,
                Harness.CODEX,
                "wrong",
                "wrongv",
                expected_revision=old.revision,
                operation_id="switch",
                at=AT,
            )
            pricing.save(work, old, rules.apply_switch(old, plan))
            wallet = WalletRepository(work.connection)
            wallet.create_vendor(work, vendor_id="shop", name="Shop", at=AT, operation_id="vendor")
            wallet.link_group(work, "correct", "shop", at=AT)
            work.connection.execute("INSERT INTO provider_alias VALUES('codex','remote-identity','shop')")
            work.connection.execute(
                "INSERT INTO preference VALUES('autoMatchProvider',?)", ("true" if enabled else "false",)
            )

        writer.execute(seed)
        collect(writer, (replace(usage(1), provider_name=name),))
        assert BillInbox(SqliteCollectionStore(writer, readers), FakeClock(AT)).run().batch.billed == 1
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT price_group_id FROM ledger").fetchone()[0])
            == expected
        )
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("shop")).c == (
            110_000_000 if expected == "correct" else 0
        )
