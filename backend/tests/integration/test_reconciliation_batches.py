"""Real voucher members, independent local publication and read-only remote evidence."""

from dataclasses import replace
from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.reconciliation_read import preview
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.session_activity import source_observed
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import format_instant
from htd.domain.remote.model import BalanceKind, BalanceObservation, RemoteAccount
from htd.domain.usage.model import Harness, Tokens, UsageEvent
from htd.domain.usage.session import SourceObservation
from htd.ipc.dto.reconciliation import BatchResponse, ReconciliationPreview

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.management_helpers import group, price, vendor, wallet

from .collection_helpers import AT, usage
from .test_quiet_account_output import observe, setup
from .test_remote_wallet import balance

USD = 10**12


def configured(app: Application) -> tuple[str, str]:
    g, v = group(app)
    price(
        app,
        "pricing.saveVersionPrices",
        versionId=v,
        models=[{"modelId": "Model", "input": "1", "cache": "0", "cacheWrite": "0", "output": "0"}],
    )
    for harness in ("codex", "claude"):
        price(app, "pricing.switchCurrent", harness=harness, groupId=g, versionId=v)
    supplier = vendor(app, initial="100")
    wallet(
        app,
        "wallet.linkGroups",
        vendorId=supplier.id,
        groupIds=[g],
        expectedRevision=app.revisions()["wallet"],
    )
    return g, supplier.id


def receive(app: Application, event: UsageEvent) -> None:
    root = str(app.writer.path.parent / event.harness.value)
    app.writer.execute(
        lambda work: SourcesRepository(work.connection).set_path(work, event.harness, root, available=True)
    )
    app.writer.execute(
        lambda work: IngestRepository(work.connection).commit_batch(
            work,
            event.harness,
            root,
            root + "/synthetic.jsonl",
            {"offset": event.request_id},
            (event,),
            at=app.clock.now_utc(),
        )
    )
    app.writer.execute(
        lambda work: source_observed(
            work.connection, SourceObservation(event.harness, app.clock.now_utc(), "ready", "synthetic")
        )
    )
    app.scan.billing.run()


def test_quiet_local_session_publishes_while_another_session_on_same_account_is_active(
    tmp_path: Path,
) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), session="a", tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(60_000)
        receive(app, usage(2, at=clock.now_utc(), session="b", tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(7 * 60_000)
        receive(app, usage(3, at=clock.now_utc(), session="b", tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(2 * 60_000)
        remote = SqliteRemoteStore(app.writer, app.readers)
        remote.flush_pending(clock.now_utc())
        db = app.readers.get()
        flows = db.execute(
            "SELECT id,c FROM wallet_flow WHERE vendor_id=? AND scope='local' AND kind='consumption'",
            (supplier,),
        ).fetchall()
        assert len(flows) == 1 and flows[0]["c"] == str(USD)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM wallet_flow_member WHERE flow_id=?", (flows[0]["id"],)
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM session_wallet_pending").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM session_request WHERE published_revision>0").fetchone()[0] == 1
        )
        clock.advance(9 * 60_000)
        remote.flush_pending(clock.now_utc())
        assert (
            sum(
                int(row[0])
                for row in db.execute(
                    "SELECT c FROM wallet_flow WHERE vendor_id=? AND scope='local' AND kind='consumption'",
                    (supplier,),
                )
            )
            == 3 * USD
        )
        assert db.execute("SELECT COUNT(*) FROM session_wallet_pending").fetchone()[0] == 0


def test_restart_keeps_original_members_and_due_time_when_same_session_resumes(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    data = tmp_path / "data"
    with Application(data, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), session="a", tokens=Tokens(1_000_000, 0, 0, 0)))
    clock.advance(60_000)
    with Application(data, clock=clock, start_scheduler=False) as app:
        receive(app, usage(2, at=clock.now_utc(), session="a", tokens=Tokens(1_000_000, 0, 0, 0)))
        db = app.readers.get()
        assert db.execute("SELECT COUNT(*) FROM session_activity_batch").fetchone()[0] == 2
        clock.advance(9 * 60_000)
        SqliteRemoteStore(app.writer, app.readers).flush_pending(clock.now_utc())
        assert [
            row[0]
            for row in db.execute(
                "SELECT c FROM wallet_flow WHERE vendor_id=? AND scope='local' AND kind='consumption'",
                (supplier,),
            )
        ] == [str(USD)]
        assert db.execute("SELECT COUNT(*) FROM session_wallet_pending").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM session_request WHERE published_revision>0").fetchone()[0] == 1
        )


def test_remote_preview_preserves_original_vendor_and_is_read_only(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        g, supplier = configured(app)
        account = RemoteAccount(
            "synthetic",
            supplier,
            "https://example.test",
            "HarnessTokenDeck/remote/synthetic",
            site_type="newapi",
            auth_mode="access_token",
            user_id="1",
        )
        store = SqliteRemoteStore(app.writer, app.readers)
        store.execute(lambda work: store.save_account(work, account, clear=True))
        baseline = BalanceObservation(
            account.id,
            clock.now_utc(),
            BalanceKind.ACCOUNT_REMAINING,
            "example.test",
            "USD",
            100 * USD,
            cumulative_used_pico=0,
            usage_scope="newapi:user:used_quota:v1",
        )
        store.execute(lambda work: store.observe(work, baseline))
        for index, harness in enumerate((Harness.CODEX, Harness.CLAUDE), 1):
            clock.advance(60_000)
            receive(
                app,
                usage(
                    index,
                    harness=harness,
                    at=clock.now_utc(),
                    session=str(index),
                    tokens=Tokens(1_000_000, 0, 0, 0),
                ),
            )
        clock.advance(60_000)
        store.execute(
            lambda work: store.observe(
                work,
                replace(
                    baseline, fetched_at=clock.now_utc(), amount_pico=95 * USD, cumulative_used_pico=5 * USD
                ),
            )
        )
        clock.advance(10 * 60_000)
        store.flush_pending(clock.now_utc())
        db = app.readers.get()
        interval = db.execute("SELECT id FROM account_interval").fetchone()[0]
        query = {
            "vendorId": supplier,
            "start": format_instant(AT),
            "end": "2026-09-30T00:03:00.000Z",
            "intervalIds": [interval],
        }
        before = app.revisions()
        result = invoke(app, "reconciliation.preview", query, ReconciliationPreview)
        assert result.memberCount == "2" and result.currentEstimatePico == str(2 * USD)
        assert result.remoteTargetPico == str(5 * USD) and result.reliability == "reliable_usage"
        assert result.coverageAligned and result.localComplete and result.quiet
        assert result.externalUsagePossible and app.revisions() == before
        assert db.execute("SELECT COUNT(*) FROM reconciliation_interval_member").fetchone()[0] == 2
        clock.advance(60_000)
        receive(app, usage(3, at=clock.now_utc(), session="later", tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(60_000)
        store.execute(
            lambda work: store.observe(
                work,
                replace(
                    baseline, fetched_at=clock.now_utc(), amount_pico=90 * USD, cumulative_used_pico=10 * USD
                ),
            )
        )
        clock.advance(10 * 60_000)
        store.flush_pending(clock.now_utc())
        recent = invoke(
            app,
            "reconciliation.batches",
            {"vendorId": supplier, "start": format_instant(AT), "end": format_instant(clock.now_utc())},
            BatchResponse,
        )
        assert len(recent.remoteIntervals) == 2
        latest, earlier = recent.remoteIntervals
        assert latest.localStart is not None and earlier.localStart is not None
        assert latest.localStart > earlier.localStart
        assert recent.remoteIntervals[1].id == interval
        other = vendor(app, name="Other")
        wallet(
            app,
            "wallet.linkGroups",
            vendorId=other.id,
            groupIds=[g],
            expectedRevision=app.revisions()["wallet"],
        )
        reread = invoke(app, "reconciliation.preview", query, ReconciliationPreview)
        assert reread.currentEstimatePico == result.currentEstimatePico
        assert db.execute("SELECT DISTINCT vendor_id FROM reconciliation_member").fetchone()[0] == supplier
        # Selecting one local batch cannot certify the entire account interval.
        partial = invoke(
            app,
            "reconciliation.preview",
            query | {"localBatchIds": [result.batchIds[0]]},
            ReconciliationPreview,
        )
        assert (
            partial.memberCount == "1" and partial.omittedMemberCount == "1" and not partial.coverageAligned
        )


@pytest.mark.parametrize("reliable", [False, True])
def test_reliability_is_independent_of_balance_estimates_and_changed_units(
    tmp_path: Path,
    reliable: bool,
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, used=0 if reliable else None)
        observe(store, 5 * USD, 3, 5 * USD if reliable else None)
        store.flush_pending(balance(0, 13).fetched_at)
        query = {
            "vendorId": "vendor",
            "start": format_instant(balance(0, 0).fetched_at),
            "end": format_instant(balance(0, 3).fetched_at),
            "intervalIds": [row[0] for row in readers.get().execute("SELECT id FROM account_interval")],
        }
        result = preview(readers.get(), query, balance(0, 13).fetched_at)
        assert result["reliability"] == ("reliable_usage" if reliable else "balance_estimate")
        assert result["memberCount"] == "0" and not result["localComplete"]
        assert result["remoteTargetPico"] == str(5 * USD)
        if reliable:
            changed = replace(
                balance(5 * USD, 20),
                cumulative_used_pico=0,
                usage_scope="newapi:user:used_quota:v1",
                original_currency="CNY",
                conversion_rule="fixed-CNY-USD-1:1-v1",
            )
            store.execute(lambda work: store.observe(work, changed))
            store.execute(
                lambda work: store.observe(
                    work,
                    replace(
                        changed,
                        fetched_at=balance(0, 23).fetched_at,
                        amount_pico=4 * USD,
                        cumulative_used_pico=USD,
                    ),
                )
            )
            store.flush_pending(balance(0, 33).fetched_at)
            query["end"] = format_instant(balance(0, 23).fetched_at)
            query["intervalIds"] = [
                row[0] for row in readers.get().execute("SELECT id FROM account_interval")
            ]
            assert len(query["intervalIds"]) == 2
            mixed = preview(readers.get(), query, balance(0, 33).fetched_at)
            assert mixed["reliability"] == "invalid" and not mixed["coverageAligned"]
