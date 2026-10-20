"""Explicit recovery decisions are bounded, durable and financially isolated."""

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import parse_instant
from htu.domain.pricing.history import HISTORY_GROUP_ID, HISTORY_VERSION_ID
from htu.domain.usage.model import Tokens
from htu.ipc.dto.pricing import CatalogResponse, ResolveUnpricedResponse
from htu.ipc.dto.wallet import WalletListResponse

from backend.tests.integration.collection_helpers import usage

from .command_helpers import failure, invoke
from .maintenance_helpers import add_usage
from .management_helpers import group, price, vendor, wallet
from .test_unpriced_commands import pending, recalculate, setup

AT = parse_instant("2026-10-17T00:00:00Z")
MODEL = {"modelId": "Model", "input": "2", "cache": "0", "output": "0", "cacheWrite": "3"}


def resolve(app: Application, action: str, **values: object) -> ResolveUnpricedResponse:
    return invoke(
        app,
        "pricing.resolveUnpriced",
        {
            "operationId": str(uuid4()),
            "action": action,
            "itemIds": [pending(app).items[0].id],
            "expectedPricingRevision": app.revisions()["pricing"],
            "expectedUsageRevision": app.revisions()["quality"],
            **values,
        },
        ResolveUnpricedResponse,
    )


def test_partial_ignore_restore_new_arrivals_replay_and_restart(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        group(app)
        add_usage(app, [usage(i, at=AT) for i in (1, 2)])
        request = {
            "operationId": str(uuid4()),
            "action": "ignore",
            "recordLimit": 1,
            "itemIds": [pending(app).items[0].id],
            "expectedPricingRevision": app.revisions()["pricing"],
            "expectedUsageRevision": app.revisions()["quality"],
        }
        result = invoke(app, "pricing.resolveUnpriced", request, ResolveUnpricedResponse)
        assert invoke(app, "pricing.resolveUnpriced", request, ResolveUnpricedResponse) == result
        assert result.changed == 1
        assert (pending(app).recordCount, pending(app).ignoredCount) == (1, 1)
        add_usage(app, [usage(3, at=AT)])
        assert (pending(app).recordCount, pending(app).ignoredCount) == (2, 1)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        ignored = next(i for i in pending(app).items if i.ignored)
        resolve(app, "restore", itemIds=[ignored.id])
        assert (pending(app).recordCount, pending(app).ignoredCount) == (3, 0)
        assert recalculate(app).remaining == 3


def test_manual_price_in_history_recovers_orphans_without_wallet_entries(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        group(app)
        add_usage(app, [usage(1, at=AT, tokens=Tokens(1_000_000, 0, 100_000, 0, 0))])
        resolve(app, "price", model=MODEL, multiplier="1.5")
        item = pending(app).items[0]
        assert item.groupId == HISTORY_GROUP_ID and item.versionId == HISTORY_VERSION_ID
        assert item.readyCount == 1 and item.multiplier == "1.5"
        assert item.price and item.price.input == "2"
        assert recalculate(app).costDeltaPico == "2100000000000"
        db = app.readers.get()
        assert db.execute("SELECT pico,unpriced,price_group_id FROM ledger").fetchone()[:] == (
            "2100000000000",
            0,
            HISTORY_GROUP_ID,
        )
        assert db.execute("SELECT COUNT(*) FROM wallet_entry").fetchone()[0] == 0
        supplier = vendor(app)
        assert (
            failure(
                app,
                "wallet.linkGroups",
                {
                    "operationId": str(uuid4()),
                    "vendorId": supplier.id,
                    "groupIds": [HISTORY_GROUP_ID],
                    "expectedRevision": app.revisions()["wallet"],
                },
            ).code
            == "WALLET_INVALID"
        )
        assert (
            failure(
                app,
                "pricing.switchCurrent",
                {
                    "operationId": str(uuid4()),
                    "harness": "codex",
                    "groupId": HISTORY_GROUP_ID,
                    "versionId": HISTORY_VERSION_ID,
                    "expectedRevision": app.revisions()["pricing"],
                },
            ).code
            == "PRICING_INVALID"
        )
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        assert pending(app).recordCount == 0
        assert next(
            g for g in invoke(app, "pricing.catalog", {}, CatalogResponse).groups if g.id == HISTORY_GROUP_ID
        ).historical


def test_assignment_uses_existing_version_but_preserves_original_wallet(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, _, original = setup(app, clock)
        g, v = group(app, "new group")
        price(app, "pricing.saveVersionPrices", versionId=v, models=[MODEL])
        other = vendor(app, name="other supplier", initial="100")
        wallet(
            app,
            "wallet.linkGroups",
            vendorId=other.id,
            groupIds=[g],
            expectedRevision=app.revisions()["wallet"],
        )
        resolve(app, "assign", groupId=g, versionId=v)
        assert pending(app).readyCount == 1
        assert recalculate(app).recalculated == 1
        balances = {v.id: v.B for v in invoke(app, "wallet.list", {}, WalletListResponse).vendors}
        assert balances[original] == "97900000000000" and balances[other.id] == "100000000000000"


def test_price_and_decision_rollback_together_and_stale_selection_is_rejected(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        group(app)
        add_usage(app, [usage(1, at=AT)])
        original = pending(app)
        app.writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER reject_decision BEFORE INSERT ON unpriced_resolution "
                "BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            app.unpriced.resolve(
                {
                    "action": "price",
                    "model": MODEL,
                    "itemIds": [original.items[0].id],
                    "expectedPricingRevision": original.pricingRevision,
                    "expectedUsageRevision": original.usageRevision,
                },
                str(uuid4()),
                model=invoke_model(),
            )
        assert pending(app) == original
        assert (
            not app.readers.get()
            .execute("SELECT 1 FROM price_group WHERE id=?", (HISTORY_GROUP_ID,))
            .fetchone()
        )
        add_usage(app, [usage(2, at=AT)])
        assert (
            failure(
                app,
                "pricing.resolveUnpriced",
                {
                    "operationId": str(uuid4()),
                    "action": "ignore",
                    "itemIds": [original.items[0].id],
                    "expectedPricingRevision": original.pricingRevision,
                    "expectedUsageRevision": original.usageRevision,
                },
            ).code
            == "QUALITY_REVISION_CONFLICT"
        )


def test_direct_price_replaces_a_case_equivalent_multiplier_hint(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, version_id, _ = setup(app, clock)
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=version_id,
            models=[{**MODEL, "modelId": "model"}],
            hints=[{"modelId": "model", "multiplier": "2"}],
        )
        resolve(
            app,
            "price",
            groupId=pending(app).items[0].groupId,
            versionId=version_id,
            model=MODEL,
            multiplier="3",
        )
        catalog = invoke(app, "pricing.catalog", {}, CatalogResponse)
        version = next(v for g in catalog.groups for v in g.versions if v.id == version_id)
        assert len(version.hints) == 1 and version.hints[0].multiplier == "3"


def invoke_model():
    from htu.ipc.dto.pricing import ModelPrice

    return ModelPrice.model_validate(MODEL).price()
