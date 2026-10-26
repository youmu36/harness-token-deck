"""Current feedback: exact allocations, independent session views and bounded copies."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from htd.adapters.sqlite.calibration_costs import adjustments, projection
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.writer import WriteExecutor
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.remote.model import BalanceKind, BalanceObservation, RemoteAccount
from htd.domain.usage.model import Harness, Tokens
from htd.domain.usage.session import session_reference
from htd.ipc.dto.explorer import ExploreResponse
from htd.ipc.dto.reconciliation import BatchResponse, CalibrationPreview

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.management_helpers import catalog, price

from .collection_helpers import AT, collect, usage
from .test_calibration_targets import apply, balance
from .test_reconciliation_batches import USD, configured, receive


@pytest.mark.parametrize(
    "mode,weights,target,expected",
    [
        ("proportional", {}, 5 * USD, (125 * USD // 100, 375 * USD // 100)),
        ("weighted", {"a": "1", "b": "0"}, 6 * USD, (3 * USD, 3 * USD)),
        ("proportional", {}, 2 * USD, (USD // 2, 3 * USD // 2)),
        ("proportional", {}, 4 * USD + 1, (USD, 3 * USD + 1)),
    ],
)
def test_model_allocation_changes_wallet_and_daily_cost_exactly(
    tmp_path: Path, mode: str, weights: dict[str, str], target: int, expected: tuple[int, int]
) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        version = catalog(app).groups[0].versions[0].id
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=version,
            models=[
                {"modelId": name, "input": str(rate), "cache": "0", "output": "0"}
                for name, rate in (("A", 1), ("B", 3))
            ],
        )
        clock.advance(60_000)
        for i, name in enumerate(("A", "B"), 1):
            receive(app, usage(i, model=name, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        end = format_instant(Instant(clock.now_utc().value + timedelta(milliseconds=1)))
        clock.advance(11 * 60_000)
        parameters: dict[str, object] = dict(
            vendorId=supplier,
            start=format_instant(AT),
            end=end,
            targetPico=str(target),
            confirmLocalScope=True,
            acknowledgeIncomplete=True,
            allocationMode=mode,
            modelWeights=weights,
        )
        result = apply(app, parameters)
        assert sum(int(row.targetPico) for row in result.modelTargets) == target
        assert [int(row.targetPico) for row in result.modelTargets] == list(expected)
        assert balance(app, supplier)[1:] == (target, 100 * USD - target)
        db = app.readers.get()
        for name, original, effective in zip(("a", "b"), (USD, 3 * USD), expected, strict=True):
            scope = dict(parameters, modelKeys=[name])
            assert int(adjustments(db, scope)["deltaPico"]) == effective - original
            assert projection(db, scope).period("2026-09-30T00:00:00.000Z", "hour", "UTC").costs(original)[
                "picoUsd"
            ] == str(original)
        assert sum(int(row[0]) for row in db.execute("SELECT pico FROM ledger")) == 4 * USD
        compared = ExploreResponse.model_validate(
            SqliteStatisticsStore(app.readers, app.writer).explore(
                dict(
                    parameters,
                    grain="day",
                    sections=["comparison"],
                    compareDimension="model",
                    compareIds=["a", "b"],
                ),
                clock.now_utc(),
            )
        )
        assert [int(row.totals.picoUsd) for row in compared.comparisons] == list(expected)
        apply(app, dict(parameters, action="revoke", targetId=result.targetId))
        assert balance(app, supplier)[1:] == (4 * USD, 96 * USD)


def test_cross_day_allocation_is_conserved_after_restart(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(86400_000)
        receive(app, usage(2, at=clock.now_utc(), tokens=Tokens(3_000_000, 0, 0, 0)))
        end = format_instant(Instant(clock.now_utc().value + timedelta(milliseconds=1)))
        parameters: dict[str, object] = dict(
            vendorId=supplier,
            start=format_instant(AT),
            end=end,
            targetPico=str(10 * USD),
            confirmLocalScope=True,
            acknowledgeIncomplete=True,
            allocationMode="proportional",
        )
        result = apply(app, parameters)
        assert [int(row.targetPico) for row in result.days] == [25 * USD // 10, 75 * USD // 10]
        assert sum(int(row.deltaPico) for row in result.days) == 6 * USD
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        assert balance(app, supplier)[1:] == (10 * USD, 90 * USD)
        assert adjustments(app.readers.get(), parameters)["deltaPico"] == str(6 * USD)


def test_invalid_custom_weights_are_rejected_without_changing_money(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        params = dict(
            vendorId=supplier,
            start=format_instant(AT),
            end="2026-09-30T00:02:00.000Z",
            targetPico="0",
            confirmLocalScope=True,
            acknowledgeIncomplete=True,
            allocationMode="weighted",
            modelWeights={"model": "0"},
        )
        response: Any = app.router.call("reconciliation.previewCalibration", params)
        assert not response["ok"] and response["error"]["code"] == "CALIBRATION_WEIGHTS"
        assert balance(app, supplier)[1:] == (USD, 99 * USD)


def test_remote_picker_includes_exact_end_boundary_and_readable_amounts(tmp_path: Path) -> None:
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
            AT,
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
        end = clock.now_utc()
        store.execute(
            lambda work: store.observe(
                work, replace(baseline, fetched_at=end, amount_pico=98 * USD, cumulative_used_pico=2 * USD)
            )
        )
        clock.advance(10 * 60_000)
        store.flush_pending(clock.now_utc())
        listed = invoke(
            app,
            "reconciliation.batches",
            dict(vendorId=supplier, start=format_instant(AT), end=format_instant(clock.now_utc())),
            BatchResponse,
        )
        interval = listed.remoteIntervals[0]
        assert interval.consumptionPico == str(2 * USD) and interval.estimatePico == str(USD)
        assert interval.sessionCount == 1 and interval.localEnd == format_instant(clock.now_utc())
        params: dict[str, object] = dict(
            vendorId=supplier,
            start=format_instant(Instant(AT.value + timedelta(milliseconds=1))),
            end=format_instant(Instant(end.value + timedelta(milliseconds=1))),
            intervalIds=[interval.id],
            targetPico=str(2 * USD),
            confirmLocalScope=True,
            basis="remote",
            allocationMode="proportional",
        )
        preview = invoke(app, "reconciliation.previewCalibration", params, CalibrationPreview)
        assert preview.coverageAligned and preview.memberCount == "1"
        apply(app, params)
        assert balance(app, supplier)[1:] == (2 * USD, 98 * USD)


def test_monitor_independent_chart_freeze_close_and_resume(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(
            writer,
            (
                usage(1, session="a", tokens=Tokens(100, 10, 0, 1)),
                usage(2, session="b", tokens=Tokens(200, 100, 0, 2)),
            ),
            at=AT,
        )
        store = SqliteStatisticsStore(readers)
        a, b = (session_reference(Harness.CODEX, value) for value in ("a", "b"))
        view = store.monitor_data("codex", parse_instant("2026-09-30T00:11:00Z"), a, chart_session_ref=b)
        assert view["selectedSessionRef"] == a and view["chartSessionRef"] == b
        assert view["sessionTotals"]["inputTokens"] == "100"
        assert view["windowTotals"]["inputTokens"] == "200"
        assert all(row["frozen"] for row in view["sessionHitRates"])
        closed = store.monitor_data("codex", parse_instant("2026-09-30T01:00:00Z"), a, chart_session_ref=b)
        assert not closed["sessionHitRates"] and closed["selectedSessionRef"] is None


def test_paginated_cached_read_copies_only_returned_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copied: list[str] = []

    class Model(dict[str, Any]):
        def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
            copied.append(self["modelKey"])
            return deepcopy(dict(self), memo)

    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = SqliteStatisticsStore(readers)
        cached: dict[str, Any] = dict(
            models=[Model(modelKey=str(i)) for i in range(5000)], totalModels=5000, quality=[], revision=1
        )

        def cached_read(*_: object) -> dict[str, Any]:
            return dict(cached)

        monkeypatch.setattr(store, "_explore_cached", cached_read)
        view: dict[str, Any] = store.explore(dict(offset=100, limit=50), AT)
        assert len(view["models"]) == 50 and len(copied) == 50
        assert len(cached["models"]) == 5000


def test_late_historical_reception_does_not_reopen_closed_monitor_sessions(tmp_path: Path) -> None:
    later = parse_instant("2026-09-30T02:00:00Z")
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, (usage(1, session="old", at=AT),), at=later)
        store = SqliteStatisticsStore(readers)
        result = store.monitor_data("codex", later, session_reference(Harness.CODEX, "old"))
        assert result["sessionHitRates"] == [] and result["selectedSessionRef"] is None
