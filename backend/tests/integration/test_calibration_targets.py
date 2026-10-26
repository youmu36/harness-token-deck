"""Independent locked-target money, identity, allocation and current-basis revocation vectors."""

from pathlib import Path
from uuid import uuid4

import pytest
from htd.adapters.sqlite.calibration_costs import adjustments
from htd.application.ports import CostChanged, UsageChanged
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import format_instant
from htd.domain.usage.model import Tokens
from htd.ipc.dto.maintenance import RecalcPreview, RecalcResponse
from htd.ipc.dto.reconciliation import CalibrationPreview, CalibrationResponse
from htd.ipc.dto.wallet import WalletListResponse

from backend.tests.contracts.command_helpers import failure, invoke
from backend.tests.contracts.management_helpers import catalog, price

from .collection_helpers import AT, usage
from .test_reconciliation_batches import USD, configured, receive


def proposal(app: Application, supplier: str, target: int) -> dict[str, object]:
    return {
        "vendorId": supplier,
        "start": format_instant(AT),
        "end": "2026-09-30T00:02:00.000Z",
        "targetPico": str(target),
        "confirmLocalScope": True,
        "acknowledgeIncomplete": True,
    }


def apply(app: Application, parameters: dict[str, object]) -> CalibrationResponse:
    preview = invoke(app, "reconciliation.previewCalibration", parameters, CalibrationPreview)
    return invoke(
        app,
        "reconciliation.commitCalibration",
        parameters | {"fingerprint": preview.fingerprint, "operationId": str(uuid4())},
        CalibrationResponse,
    )


def balance(app: Application, supplier: str) -> tuple[int, int, int]:
    result = next(
        row for row in invoke(app, "wallet.list", {}, WalletListResponse).vendors if row.id == supplier
    )
    assert result.localWallet is not None
    return int(result.localWallet.R), int(result.localWallet.C), int(result.localWallet.B)


def test_locked_target_recalculation_outside_members_and_revoke_current_estimate(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        g, supplier = configured(app)
        v = catalog(app).groups[0].versions[0].id
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=v,
            models=[{"modelId": "Model", "input": "8", "cache": "0", "output": "0"}],
        )
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(11 * 60_000)
        parameters = proposal(app, supplier, 10 * USD)
        events: list[object] = []
        app.bus.subscribe(events.append)
        result = apply(app, parameters)
        assert result.currentEstimatePico == str(8 * USD) and result.deltaPico == str(2 * USD)
        assert balance(app, supplier)[1:] == (10 * USD, 90 * USD)
        assert any(isinstance(event, CostChanged) for event in events)
        assert not any(isinstance(event, UsageChanged) for event in events)
        clock.advance(1000)
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=v,
            models=[{"modelId": "Model", "input": "9", "cache": "0", "output": "0"}],
        )
        recalc = {
            "harness": "codex",
            "start": parameters["start"],
            "end": parameters["end"],
            "groupId": g,
            "versionId": v,
        }
        preview = invoke(app, "maintenance.previewRecalc", recalc, RecalcPreview)
        assert preview.vendors[0].consumptionDelta == "0"
        invoke(
            app,
            "maintenance.commitRecalc",
            recalc | {"fingerprint": preview.fingerprint, "operationId": str(uuid4())},
            RecalcResponse,
        )
        db = app.readers.get()
        assert db.execute("SELECT 1 FROM revision WHERE scope='cost'").fetchone() is None
        from htd.domain.statistics import Window

        fixed = app.statistics.summary("all", Window(AT, clock.now_utc(), 0, "hour"))[0]
        assert fixed.metrics.estimate_pico == str(9 * USD)
        assert fixed.metrics.correction_pico == str(USD)
        assert fixed.metrics.pico_usd == str(10 * USD)
        target = db.execute(
            "SELECT estimate_pico,delta_pico,target_pico FROM calibration_target WHERE state='active'"
        ).fetchone()
        assert tuple(target) == (str(9 * USD), str(USD), str(10 * USD))
        assert balance(app, supplier)[1:] == (10 * USD, 90 * USD)
        assert db.execute("SELECT pico FROM ledger").fetchone()[0] == str(9 * USD)
        assert adjustments(db, dict(parameters))["deltaPico"] == str(USD)
        partial = adjustments(db, dict(parameters, modelKeys=["model"]))
        assert partial["deltaPico"] == "0" and partial["allocationIncomplete"]
        receive(app, usage(2, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        assert balance(app, supplier)[1:] == (19 * USD, 81 * USD)
        revoked = apply(app, dict(parameters, action="revoke", targetId=result.targetId))
        assert revoked.deltaPico == str(-USD)
        assert balance(app, supplier)[1:] == (18 * USD, 82 * USD)
        assert db.execute("SELECT COUNT(*) FROM calibration_owner").fetchone()[0] == 0


@pytest.mark.parametrize("difference", [1, -1, -USD])
def test_explicit_target_accepts_signed_and_sub_cent_deltas(tmp_path: Path, difference: int) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        result = apply(app, proposal(app, supplier, USD + difference))
        assert result.deltaPico == str(difference)
        assert balance(app, supplier)[1:] == (USD + difference, 99 * USD - difference)


def test_model_allocation_is_explicit_and_overlap_requires_whole_target_replacement(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        v = catalog(app).groups[0].versions[0].id
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=v,
            models=[{"modelId": name, "input": "4", "cache": "0", "output": "0"} for name in ("A", "B")],
        )
        clock.advance(60_000)
        for index, model in enumerate(("A", "B"), 1):
            receive(app, usage(index, at=clock.now_utc(), model=model, tokens=Tokens(1_000_000, 0, 0, 0)))
        params = proposal(app, supplier, 10 * USD)
        params["allocations"] = [
            {
                "day": "2026-09-30",
                "targetPico": str(6 * USD),
                "modelKeys": ["a"],
                "confirmModelCoverage": True,
            }
        ]
        first = apply(app, params)
        db = app.readers.get()
        assert adjustments(db, dict(params, modelKeys=["a"]))["deltaPico"] == str(2 * USD)
        assert adjustments(db, dict(params, modelKeys=["b"]))["allocationIncomplete"]
        assert (
            failure(
                app,
                "reconciliation.previewCalibration",
                dict(
                    params,
                    end="2026-09-30T00:01:00.001Z",
                    replaceTargetIds=[first.targetId],
                    confirmReplacement=True,
                ),
            ).code
            == "CALIBRATION_PARTIAL_OVERLAP"
        )
        assert (
            failure(app, "reconciliation.previewCalibration", proposal(app, supplier, 11 * USD)).code
            == "CALIBRATION_OVERLAP"
        )
        second = apply(
            app,
            dict(
                params, targetPico=str(11 * USD), replaceTargetIds=[first.targetId], confirmReplacement=True
            ),
        )
        assert balance(app, supplier)[1:] == (11 * USD, 89 * USD)
        assert db.execute("SELECT COUNT(*) FROM calibration_target WHERE state='active'").fetchone()[0] == 1
        assert second.deltaPico == str(USD)
        assert (
            failure(
                app,
                "reconciliation.previewCalibration",
                dict(params, action="revoke", targetId=first.targetId),
            ).code
            == "CALIBRATION_TARGET_NOT_FOUND"
        )


def test_timeout_replay_restart_and_atomic_failure_after_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from htd.adapters.sqlite import calibration_store

    clock = FakeClock(AT)
    data = tmp_path / "data"
    with Application(data, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        params = proposal(app, supplier, 2 * USD)
        preview = invoke(app, "reconciliation.previewCalibration", params, CalibrationPreview)
        payload = params | {"fingerprint": preview.fingerprint, "operationId": str(uuid4())}
        original = calibration_store.move_money

        def fail_after_money(*args: object, **kwargs: object) -> None:
            original(*args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("synthetic transaction failure")

        with monkeypatch.context() as patch:
            patch.setattr(calibration_store, "move_money", fail_after_money)
            assert failure(app, "reconciliation.commitCalibration", payload).code == "INTERNAL_ERROR"
        assert balance(app, supplier)[1:] == (USD, 99 * USD)
        assert app.readers.get().execute("SELECT COUNT(*) FROM calibration_target").fetchone()[0] == 0
        saved = invoke(app, "reconciliation.commitCalibration", payload, CalibrationResponse)
        assert invoke(app, "reconciliation.commitCalibration", payload, CalibrationResponse) == saved
        assert balance(app, supplier)[1:] == (2 * USD, 98 * USD)

    with Application(data, clock=clock, start_scheduler=False) as app:
        assert invoke(app, "reconciliation.commitCalibration", payload, CalibrationResponse) == saved
        assert app.readers.get().execute("SELECT COUNT(*) FROM calibration_target").fetchone()[0] == 1
        assert balance(app, supplier)[1:] == (2 * USD, 98 * USD)


def test_cross_day_targets_and_closed_day_book_are_exact(tmp_path: Path) -> None:
    from htd.adapters.sqlite.wallet_book import read
    from htd.adapters.sqlite.wallet_closing import close_days

    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        v = catalog(app).groups[0].versions[0].id
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=v,
            models=[{"modelId": "Model", "input": "4", "cache": "0", "output": "0"}],
        )
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(16 * 3600_000)
        receive(app, usage(2, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(11 * 60_000)
        params = dict(proposal(app, supplier, 10 * USD), end="2026-09-30T16:02:00.000Z")
        assert failure(app, "reconciliation.previewCalibration", params).code == "CALIBRATION_DATES_REQUIRED"
        app.writer.execute(lambda work: close_days(work, work.connection, at=clock.now_utc()))
        params["dayTargets"] = [
            {"day": "2026-09-30", "targetPico": str(6 * USD)},
            {"day": "2026-10-01", "targetPico": str(4 * USD)},
        ]
        result = apply(app, params)
        assert [row.deltaPico for row in result.days] == [str(2 * USD), "0"]
        assert balance(app, supplier)[1:] == (10 * USD, 90 * USD)
        clock.advance(24 * 3600_000)
        app.writer.execute(lambda work: close_days(work, work.connection, at=clock.now_utc()))
        db = app.readers.get()
        september = read(db, supplier, "local", "2026-09", clock.now_utc())
        october = read(db, supplier, "local", "2026-10", clock.now_utc())
        assert september["C"] == str(6 * USD) and october["C"] == str(4 * USD)


def test_late_unproven_member_is_separate_and_stale_preview_cannot_commit(tmp_path: Path) -> None:
    from htd.domain.common.time import parse_instant

    from backend.tests.contracts.maintenance_helpers import add_usage

    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        params = proposal(app, supplier, 2 * USD)
        stale = invoke(app, "reconciliation.previewCalibration", params, CalibrationPreview)
        clock.advance(1000)
        receive(app, usage(2, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        assert (
            failure(
                app,
                "reconciliation.commitCalibration",
                params | {"fingerprint": stale.fingerprint, "operationId": str(uuid4())},
            ).code
            == "CALIBRATION_PLAN_CHANGED"
        )
        apply(app, params)
        clock.advance(11 * 60_000)
        add_usage(
            app, [usage(3, at=parse_instant("2026-09-30T00:01:30Z"), tokens=Tokens(1_000_000, 0, 0, 0))]
        )
        assert balance(app, supplier)[1:] == (3 * USD, 97 * USD)
        assert app.readers.get().execute("SELECT COUNT(*) FROM calibration_owner").fetchone()[0] == 2


def test_old_flow_target_has_one_owner_and_new_target_replaces_it_without_double_money(
    tmp_path: Path,
) -> None:
    from htd.adapters.sqlite.remote_store import SqliteRemoteStore
    from htd.ipc.dto.wallet import AccountEntriesResponse, WalletChangeResponse

    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(11 * 60_000)
        SqliteRemoteStore(app.writer, app.readers).flush_pending(clock.now_utc())
        flow = next(
            row
            for row in invoke(
                app, "wallet.accountEntries", {"vendorId": supplier, "scope": "local"}, AccountEntriesResponse
            ).entries
            if row.kind == "consumption"
        )
        change = {
            "vendorId": supplier,
            "scope": "local",
            "entryId": flow.id,
            "R": "0",
            "C": str(2 * USD),
            "expectedEntryRevision": flow.entryRevision,
            "operationId": str(uuid4()),
        }
        invoke(app, "wallet.correctEntry", change, WalletChangeResponse)
        db = app.readers.get()
        old = db.execute("SELECT id,scope_json FROM calibration_target WHERE state='active'").fetchone()
        assert old is not None and balance(app, supplier)[1:] == (2 * USD, 98 * USD)
        import json

        params = dict(
            json.loads(old["scope_json"]),
            targetPico=str(3 * USD),
            confirmLocalScope=True,
            acknowledgeIncomplete=True,
            replaceTargetIds=[old["id"]],
            confirmReplacement=True,
        )
        applied = apply(app, params)
        assert applied.deltaPico == str(USD) and balance(app, supplier)[1:] == (3 * USD, 97 * USD)
        assert (
            failure(
                app,
                "wallet.correctEntry",
                change
                | {
                    "C": str(4 * USD),
                    "expectedEntryRevision": flow.entryRevision + 1,
                    "operationId": str(uuid4()),
                },
            ).code
            == "CALIBRATION_SCOPE_OWNED"
        )


def test_local_target_never_rewrites_remote_consumption_or_balance(tmp_path: Path) -> None:
    from dataclasses import replace

    from htd.adapters.sqlite.account_book import AccountBook
    from htd.adapters.sqlite.remote_store import SqliteRemoteStore
    from htd.domain.remote.model import BalanceKind, BalanceObservation, RemoteAccount

    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
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
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(2 * 60_000)
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
        before = AccountBook(db).summary(supplier)
        params = dict(
            proposal(app, supplier, 2 * USD),
            end="2026-09-30T00:03:00.000Z",
            intervalIds=[row[0] for row in db.execute("SELECT id FROM account_interval")],
        )
        apply(app, params)
        after = AccountBook(db).summary(supplier)
        assert (before["C"], before["B"]) == (after["C"], after["B"]) == (str(5 * USD), str(95 * USD))
        assert balance(app, supplier)[1:] == (2 * USD, 98 * USD)


def test_group_recalculation_moves_cost_capabilities_without_reassigning_original_supplier(
    tmp_path: Path,
) -> None:
    from backend.tests.contracts.management_helpers import group

    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        old_group, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        params = proposal(app, supplier, 2 * USD)
        apply(app, params)
        clock.advance(1)
        target_group, target_version = group(app, "New target")
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=target_version,
            models=[{"modelId": "Model", "input": "1", "cache": "0", "output": "0"}],
        )
        recalc = {
            "harness": "codex",
            "start": params["start"],
            "end": params["end"],
            "groupId": target_group,
            "versionId": target_version,
        }
        preview = invoke(app, "maintenance.previewRecalc", recalc, RecalcPreview)
        invoke(
            app,
            "maintenance.commitRecalc",
            recalc | {"fingerprint": preview.fingerprint, "operationId": str(uuid4())},
            RecalcResponse,
        )
        db = app.readers.get()
        assert balance(app, supplier)[1:] == (2 * USD, 98 * USD)
        assert adjustments(db, dict(params, groupIds=[target_group]))["deltaPico"] == str(USD)
        assert adjustments(db, dict(params, groupIds=[old_group]))["deltaPico"] == "0"
        assert db.execute("SELECT vendor_id FROM reconciliation_member").fetchone()[0] == supplier
