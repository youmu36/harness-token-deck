"""Remote receipts own calibration boundaries, independently of new local requests."""

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.writer import WriteExecutor
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import format_instant, parse_instant
from htd.domain.remote.model import BalanceKind, BalanceObservation, RemoteAccount
from htd.domain.usage.model import Harness, Tokens
from htd.domain.usage.session import session_reference
from htd.ipc.dto.reconciliation import BatchResponse, CalibrationPreview

from backend.tests.contracts.command_helpers import invoke

from .collection_helpers import AT, collect, usage
from .test_calibration_targets import apply, balance
from .test_reconciliation_batches import USD, configured, receive


def account(app: Application, supplier: str) -> tuple[SqliteRemoteStore, BalanceObservation]:
    remote = RemoteAccount(
        "synthetic",
        supplier,
        "https://example.test",
        "HarnessTokenDeck/remote/synthetic",
        site_type="newapi",
        auth_mode="access_token",
        user_id="1",
    )
    store = SqliteRemoteStore(app.writer, app.readers)
    store.execute(lambda work: store.save_account(work, remote, clear=True))
    baseline = BalanceObservation(
        remote.id,
        app.clock.now_utc(),
        BalanceKind.ACCOUNT_REMAINING,
        "example.test",
        "USD",
        100 * USD,
        cumulative_used_pico=0,
        usage_scope="newapi:user:used_quota:v1",
    )
    store.execute(lambda work: store.observe(work, baseline))
    return store, baseline


def periods(app: Application, supplier: str) -> BatchResponse:
    return invoke(
        app,
        "reconciliation.batches",
        dict(
            vendorId=supplier,
            start=format_instant(AT),
            end=format_instant(app.clock.now_utc()),
        ),
        BatchResponse,
    )


def test_monitor_default_keeps_the_first_session_even_when_it_is_frozen(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, (usage(1, session="first", at=AT),), at=AT)
        later = parse_instant("2026-09-30T00:05:00.000Z")
        collect(writer, (usage(2, session="second", at=later),), at=later, offset=2)
        store = SqliteStatisticsStore(readers)
        first, second = (session_reference(Harness.CODEX, name) for name in ("first", "second"))
        result = store.monitor_data("codex", parse_instant("2026-09-30T00:11:00.000Z"))
        assert result["chartSessionRef"] == first
        assert next(row for row in result["sessionHitRates"] if row["sessionRef"] == first)["frozen"]
        assert (
            store.monitor_data("codex", parse_instant("2026-09-30T00:11:00.000Z"), chart_session_ref=second)[
                "chartSessionRef"
            ]
            == second
        )
        expired = store.monitor_data(
            "codex", parse_instant("2026-09-30T01:00:00.000Z"), chart_session_ref=first
        )
        assert expired["chartSessionRef"] == second
        empty = store.monitor_data(
            "codex", parse_instant("2026-09-30T01:05:00.000Z"), chart_session_ref=second
        )
        assert empty["chartSessionRef"] is None and not empty["sessionHitRates"]


@pytest.mark.parametrize("lead_minutes", [5, 6])
def test_lookback_receipt_freezes_before_eleventh_minute_and_survives_restart(
    tmp_path: Path,
    lead_minutes: int,
) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        clock.advance(60_000)
        start = clock.now_utc()
        receive(app, usage(1, at=start, session="first", tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(lead_minutes * 60_000)
        remote, baseline = account(app, supplier)
        clock.advance(60_000)
        stamp = clock.now_utc()
        # A distinct recent session makes the five-minute boundary observable.
        receive(app, usage(2, at=stamp, session="second", tokens=Tokens(1_000_000, 0, 0, 0)))
        remote.execute(
            lambda work: remote.observe(
                work,
                replace(
                    baseline,
                    fetched_at=stamp,
                    amount_pico=97 * USD,
                    cumulative_used_pico=3 * USD,
                ),
            )
        )
        assert periods(app, supplier).remoteIntervals == []
        # No background flush occurred during quietness; admission of the new input must seal it.
        clock.advance(11 * 60_000)
        receive(app, usage(3, at=clock.now_utc(), session="third", tokens=Tokens(4_000_000, 0, 0, 0)))
        remote.flush_pending(clock.now_utc())
        receipt = periods(app, supplier).remoteIntervals[0]
        expected = 2 if lead_minutes == 5 else 1
        assert receipt.sessionCount == expected
        assert receipt.estimatePico == str(expected * USD)
        assert receipt.localStart == format_instant(start if expected == 2 else stamp)
        assert receipt.localEnd is not None
        assert receipt.localEnd < format_instant(clock.now_utc())
        params: dict[str, object] = dict(
            vendorId=supplier,
            start=receipt.localStart,
            end=receipt.localEnd,
            intervalIds=[receipt.id],
            targetPico=str(3 * USD),
            basis="remote",
            confirmLocalScope=True,
            allocationMode="proportional",
        )
        preview = invoke(app, "reconciliation.previewCalibration", params, CalibrationPreview)
        assert preview.coverageAligned and preview.memberCount == str(expected)
        assert preview.currentEstimatePico == str(expected * USD)
        assert preview.quiet and preview.localComplete
        apply(app, params)
        # The new four-dollar request remains outside the locked target.
        assert balance(app, supplier)[1] == (7 if expected == 2 else 8) * USD
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        SqliteRemoteStore(app.writer, app.readers).begin_run(clock.now_utc())
        restored = periods(app, supplier).remoteIntervals[0]
        assert (restored.id, restored.localStart, restored.localEnd) == (
            receipt.id,
            receipt.localStart,
            receipt.localEnd,
        )
        assert restored.sessionCount == expected
        clock.advance(3 * 86400_000)
        assert periods(app, supplier).remoteIntervals == []
        rejected = cast(dict[str, Any], app.router.call("reconciliation.previewCalibration", params))
        assert not rejected["ok"] and rejected["error"]["code"] == "CALIBRATION_EXPIRED"


def test_old_receipt_members_are_rebuilt_once_only_after_journal_publication(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        remote, baseline = account(app, supplier)
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        remote.execute(
            lambda work: remote.observe(
                work,
                replace(
                    baseline,
                    fetched_at=clock.now_utc(),
                    amount_pico=98 * USD,
                    cumulative_used_pico=2 * USD,
                ),
            )
        )
        clock.advance(10 * 60_000)
        remote.flush_pending(clock.now_utc())
        original = periods(app, supplier).remoteIntervals[0]
        app.writer.execute(lambda work: work.connection.execute("DELETE FROM reconciliation_interval_scope"))
        app.writer.execute(lambda work: work.connection.execute("DELETE FROM reconciliation_interval_member"))
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        SqliteRemoteStore(app.writer, app.readers).begin_run(clock.now_utc())
        restored = periods(app, supplier).remoteIntervals[0]
        assert restored.sessionCount == 1 and restored.estimatePico == str(USD)
        assert restored.localStart == original.localStart and restored.localEnd == original.localEnd
