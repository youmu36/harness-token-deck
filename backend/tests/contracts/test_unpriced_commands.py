"""Thirty-day recovery stays isolated from ordinary repricing and preserves financial history."""

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from htd.adapters.sqlite.rollup_repo import RollupRepository
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import parse_instant
from htd.domain.usage.model import Harness, Tokens
from htd.ipc.dto.pricing import RecalculateUnpricedResponse, UnpricedResponse
from htd.ipc.dto.wallet import WalletListResponse

from backend.tests.integration.collection_helpers import usage

from .command_helpers import failure, invoke
from .maintenance_helpers import add_usage
from .management_helpers import group, price, vendor, wallet

AT = parse_instant("2026-09-01T00:00:00Z")


def fill(app: Application, version_id: str, rate: str = "2") -> None:
    price(
        app,
        "pricing.saveVersionPrices",
        versionId=version_id,
        models=[
            {
                "modelId": "Model",
                "input": rate,
                "cache": "0",
                "output": "0",
                "cacheWrite": "3",
            }
        ],
    )


def setup(app: Application, clock: FakeClock) -> tuple[str, str, str]:
    g, v = group(app)
    price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
    supplier = vendor(app, initial="100")
    wallet(
        app,
        "wallet.linkGroups",
        vendorId=supplier.id,
        groupIds=[g],
        expectedRevision=app.revisions()["wallet"],
    )
    clock.advance(60_000)
    add_usage(app, [usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 100_000, 0, 0))])
    return g, v, supplier.id


def pending(app: Application) -> UnpricedResponse:
    return invoke(app, "pricing.unpriced", {}, UnpricedResponse)


def recalculate(app: Application) -> RecalculateUnpricedResponse:
    return invoke(
        app,
        "pricing.recalculateUnpriced",
        {
            "operationId": str(uuid4()),
            "expectedPricingRevision": app.revisions()["pricing"],
        },
        RecalculateUnpricedResponse,
    )


@pytest.mark.parametrize("calibrated", [False, True])
def test_original_latest_version_thirty_days_wallet_offsets_replay_and_restart(
    tmp_path: Path,
    calibrated: bool,
) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        g, v, supplier = setup(app, clock)
        if calibrated:
            wallet(
                app,
                "wallet.calibrate",
                vendorId=supplier,
                target="80",
                expectedWalletRevision=app.revisions()["wallet"],
            )
        clock.advance(10 * 86_400_000)
        app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=clock.now_utc()))
        p = pending(app)
        assert p.recordCount == 1 and p.readyCount == 0
        assert (p.items[0].groupId, p.items[0].versionId, p.items[0].modelId) == (g, v, "Model")
        fill(app, v)
        # Activate another version: recovery must still use the original version.
        other = price(app, "pricing.addVersion", groupId=g, number=2).versionId
        assert other
        fill(app, other, "99")
        price(app, "pricing.activateVersion", groupId=g, versionId=other)
        assert pending(app).readyCount == 1
        request = {"operationId": str(uuid4()), "expectedPricingRevision": app.revisions()["pricing"]}
        result = invoke(app, "pricing.recalculateUnpriced", request, RecalculateUnpricedResponse)
        assert result.recalculated == 1 and result.remaining == 0
        assert result.costDeltaPico == "2100000000000"  # .9M input * 2 + .1M creation * 3
        assert invoke(app, "pricing.recalculateUnpriced", request, RecalculateUnpricedResponse) == result
        assert pending(app).recordCount == 0
        state = invoke(app, "wallet.list", {}, WalletListResponse)
        saved = next(x for x in state.vendors if x.id == supplier)
        assert ("80000000000000" if calibrated else "97900000000000") == saved.B
        assert (
            saved.localWallet
            and ("77900000000000" if calibrated else "97900000000000") == saved.localWallet.B
        )
        assert saved.unpricedCount == 0
        app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=clock.now_utc()))
        assert not app.readers.get().execute("SELECT 1 FROM ledger").fetchone()
    with Application(tmp_path, clock=clock, start_scheduler=False) as restarted:
        assert pending(restarted).recordCount == 0
        assert (
            invoke(restarted, "pricing.recalculateUnpriced", request, RecalculateUnpricedResponse) == result
        )


def test_submit_rechecks_expiration_and_never_keeps_old_unpriced_details(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, v, _ = setup(app, clock)
        fill(app, v)
        clock.advance(30 * 86_400_000)
        assert pending(app).recordCount == 1  # exact boundary is eligible
        clock.advance(1)
        assert recalculate(app).recalculated == 0
        app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=clock.now_utc()))
        assert not app.readers.get().execute("SELECT 1 FROM ledger").fetchone()
        assert not app.readers.get().execute("SELECT 1 FROM wallet_entry WHERE kind='consumption'").fetchone()


def test_group_version_breakdown_partial_prices_conflict_and_already_priced_untouched(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        g, v, _ = setup(app, clock)
        second = price(app, "pricing.addVersion", groupId=g, number=2).versionId
        assert second
        price(app, "pricing.activateVersion", groupId=g, versionId=second)
        clock.advance(1)
        add_usage(app, [usage(2, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0, 0))])
        assert {item.versionId for item in pending(app).items} == {v, second}
        before = app.revisions()["pricing"]
        fill(app, v)
        assert (
            failure(
                app,
                "pricing.recalculateUnpriced",
                {"operationId": str(uuid4()), "expectedPricingRevision": before},
            ).code
            == "PRICING_REVISION_CONFLICT"
        )
        assert recalculate(app).recalculated == 1
        assert pending(app).recordCount == 1
        fill(app, v, "90")
        assert recalculate(app).recalculated == 0  # priced requests stay at 2.1 USD
        assert (
            app.readers.get().execute("SELECT pico FROM ledger WHERE unpriced=0").fetchone()[0]
            == "2100000000000"
        )


def test_rollback_includes_ledger_wallet_audit_and_receipt(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, v, _ = setup(app, clock)
        fill(app, v)
        before = app.wallet.list(False)
        app.writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER reject_recovery BEFORE INSERT ON recalc_item "
                "BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        )
        operation = str(uuid4())
        partial = app.unpriced.recalculate(app.revisions()["pricing"], operation)
        assert partial["complete"] is False and partial["recalculated"] == 0
        assert pending(app).recordCount == 1 and app.wallet.list(False) == before
        db = app.readers.get()
        assert db.execute("SELECT COUNT(*) FROM recalc_batch").fetchone()[0] == 0
        assert db.execute("SELECT 1 FROM operation WHERE command='pricing.recalculateUnpriced'").fetchone()
        app.writer.execute(lambda w: w.connection.execute("DROP TRIGGER reject_recovery"))
        retried = app.unpriced.recalculate(app.revisions()["pricing"], operation)
        assert retried["complete"] is True and retried["recalculated"] == 1


def test_large_frozen_job_resumes_after_restart_without_new_arrivals(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    operation = str(uuid4())
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, version, _ = setup(app, clock)
        add_usage(
            app,
            [usage(i, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 100_000, 0, 0)) for i in range(2, 502)],
        )
        fill(app, version)
        app.writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER reject_second_batch BEFORE INSERT ON recalc_item "
                "WHEN (SELECT COUNT(*) FROM recalc_item)>=500 BEGIN SELECT "
                "RAISE(ABORT,'second batch failure'); END"
            )
        )
        revision = app.revisions()["pricing"]
        partial = app.unpriced.recalculate(revision, operation)
        assert partial["complete"] is False and partial["recalculated"] == 500
        add_usage(app, [usage(503, at=clock.now_utc(), model="Other")])
        app.writer.execute(lambda w: w.connection.execute("DROP TRIGGER reject_second_batch"))
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        result = app.unpriced.recalculate(revision, operation)
        assert result["complete"] is True and result["recalculated"] == 501
        assert app.unpriced.recalculate(revision, operation) == result
        assert pending(app).recordCount == 1


def test_missing_identity_deleted_group_and_legacy_summary_are_explained(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        g, _, _ = setup(app, clock)
        add_usage(app, [replace(usage(0, at=clock.now_utc()), harness=Harness.CLAUDE, source_kind=None)])
        price(app, "pricing.deleteGroup", groupId=g)
        result = pending(app)
        assert result.recordCount == 2 and result.readyCount == 0
        assert any("未选择" in item.reasons[0] for item in result.items)
        assert any("已删除" in item.reasons[0] for item in result.items)
        assert recalculate(app).remaining == 2


def test_old_summary_reports_its_lost_recovery_metadata_without_guessing(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        setup(app, clock)
        clock.advance(4 * 86_400_000)
        # Materialize the pre-upgrade shape, whose rollups have no original version or request tokens.
        app.writer.execute(lambda w: w.connection.execute("UPDATE ledger SET unpriced=0"))
        app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=clock.now_utc()))
        app.writer.execute(lambda w: w.connection.execute("UPDATE usage_rollup SET unpriced=1"))
        result = pending(app)
        assert result.recordCount == 0 and len(result.archived) == 1
        assert result.archived[0].modelId == "Model" and result.archived[0].buckets == 1
        assert recalculate(app).recalculated == 0
