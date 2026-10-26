"""M-series repricing windows, historical revisions, fingerprints and atomic wallet deltas."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from htd.application.ports import UsageChanged, WalletChanged
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.model import Tokens
from htd.ipc.dto.maintenance import RecalcPreview, RecalcResponse
from htd.ipc.dto.wallet import WalletListResponse

from backend.tests.integration.collection_helpers import usage

from .command_helpers import failure, invoke
from .maintenance_helpers import add_usage
from .management_helpers import group, price, vendor, wallet

AT = parse_instant("2026-09-30T00:00:00Z")
MILLION = Tokens(1_000_000, 0, 0, 0, 0)


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Application]:
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as instance:
        yield instance


def rates(app: Application, version_id: str, rate: str, **extra: object) -> None:
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
                **extra,
            }
        ],
    )


def parameters(app: Application, group_id: str, version_id: str) -> dict[str, object]:
    return {
        "harness": "codex",
        "start": format_instant(AT),
        "end": format_instant(app.clock.now_utc()),
        "groupId": group_id,
        "versionId": version_id,
    }


def setup(app: Application, *, calibrate: bool = False) -> tuple[dict[str, object], str]:
    g, v = group(app)
    rates(app, v, "1")
    price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
    supplier = vendor(app)
    wallet(
        app,
        "wallet.linkGroups",
        vendorId=supplier.id,
        groupIds=[g],
        expectedRevision=app.revisions()["wallet"],
    )
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(60_000)
    add_usage(app, [usage(1, at=app.clock.now_utc(), tokens=MILLION)])
    app.clock.advance(60_000)
    if calibrate:
        wallet(
            app,
            "wallet.calibrate",
            vendorId=supplier.id,
            target="50",
            expectedWalletRevision=app.revisions()["wallet"],
        )
    # Copy prices into the first target revision, exercising pre-creation fallback.
    rates(app, v, "4")
    target = price(app, "pricing.addVersion", groupId=g, number=2, copyFromVersionId=v).versionId
    assert target
    return parameters(app, g, target), supplier.id


@pytest.mark.parametrize("calibrate", [False, True])
def test_M01_commit_original_wallet_delta_and_late_offset_replay_restart(
    app: Application, tmp_path: Path, calibrate: bool
) -> None:
    params, supplier = setup(app, calibrate=calibrate)
    preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
    assert (
        preview.recordCount == 1
        and preview.oldTotal == "1000000000000"
        and preview.newTotal == "4000000000000"
    )
    assert preview.vendors[0].consumptionDelta == "3000000000000"
    assert preview.vendors[0].balanceDelta == "-3000000000000"
    events: list[object] = []
    app.bus.subscribe(events.append)
    payload = {**params, "fingerprint": preview.fingerprint, "operationId": str(uuid4())}
    result = invoke(app, "maintenance.commitRecalc", payload, RecalcResponse)
    assert result.newTotal == preview.newTotal
    db = app.readers.get()
    assert db.execute("SELECT COUNT(*) FROM recalc_batch").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM recalc_item").fetchone()[0] == 1
    assert db.execute("SELECT pico FROM ledger").fetchone()[0] == "4000000000000"
    assert db.execute("SELECT COUNT(*) FROM wallet_entry WHERE kind='consumption_delta'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM wallet_entry WHERE note='late_offset'").fetchone()[0] == int(
        calibrate
    )
    current = next(v for v in invoke(app, "wallet.list", {}, WalletListResponse).vendors if v.id == supplier)
    assert current.C == "4000000000000" and ("50000000000000" if calibrate else "-4000000000000") == current.B
    assert (
        current.localWallet and ("47000000000000" if calibrate else "-4000000000000") == current.localWallet.B
    )
    assert any(isinstance(e, UsageChanged) for e in events) and any(
        isinstance(e, WalletChanged) for e in events
    )
    assert invoke(app, "maintenance.commitRecalc", payload, RecalcResponse) == result
    assert (
        failure(app, "maintenance.commitRecalc", payload | {"fingerprint": "0" * 64}).code
        == "OPERATION_ID_REUSED"
    )
    app.close()
    with Application(
        tmp_path / "data", clock=FakeClock(parse_instant("2026-09-30T00:02:00Z")), start_scheduler=False
    ) as restarted:
        assert invoke(restarted, "maintenance.commitRecalc", payload, RecalcResponse) == result


@pytest.mark.parametrize("change", ["price", "ledger", "billing", "calibration"])
def test_M02_changed_plan_rejects_without_audit_or_events(app: Application, change: str) -> None:
    params, supplier = setup(app)
    preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
    if change == "price":
        rates(app, str(params["versionId"]), "5")
    elif change == "ledger":
        add_usage(app, [usage(2, at=parse_instant("2026-09-30T00:01:30Z"), tokens=MILLION)])
    elif change == "billing":
        app.writer.execute(lambda work: work.connection.execute("UPDATE ledger SET pico='2'"))
    else:
        wallet(
            app,
            "wallet.calibrate",
            vendorId=supplier,
            target="50",
            expectedWalletRevision=app.revisions()["wallet"],
        )
    app.events.attach(lambda script: None)
    before = app.revisions()
    error = failure(
        app,
        "maintenance.commitRecalc",
        {**params, "fingerprint": preview.fingerprint, "operationId": str(uuid4())},
    )
    assert error.code == "RECALC_PLAN_CHANGED" and app.revisions() == before and app.events.snapshot() == []
    assert app.readers.get().execute("SELECT COUNT(*) FROM recalc_batch").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("start_offset", "end_offset", "error"),
    [
        (0, 0, "RANGE_INVALID"),
        (-72 * 3600, 0, None),
        (-72 * 3600 - 120, 0, "RECALC_RANGE_TOO_LONG"),
        (-72 * 3600 - 120, -180, "RECALC_TOO_OLD"),
        (-60, 120, "RECALC_IN_FUTURE"),
    ],
)
def test_M03_window_edges(app: Application, start_offset: int, end_offset: int, error: str | None) -> None:
    g, v = group(app)
    params = {
        "harness": "codex",
        "groupId": g,
        "versionId": v,
        "start": format_instant(Instant(AT.value + timedelta(seconds=start_offset))),
        "end": format_instant(Instant(AT.value + timedelta(seconds=end_offset))),
    }
    if error:
        assert failure(app, "maintenance.previewRecalc", params).code == error
    else:
        assert invoke(app, "maintenance.previewRecalc", params, RecalcPreview).recordCount == 0


def test_M04_latest_target_revision_and_half_open_window(app: Application) -> None:
    g, v = group(app)
    rates(app, v, "1")
    price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(12 * 3600 * 1000)
    rates(app, v, "3")
    app.clock.advance(2 * 3600 * 1000)
    add_usage(
        app,
        [
            usage(1, at=parse_instant("2026-09-30T06:00:00Z"), tokens=MILLION),
            usage(2, at=parse_instant("2026-09-30T13:00:00Z"), tokens=MILLION),
            usage(3, at=app.clock.now_utc(), tokens=MILLION),
        ],
    )
    params = parameters(app, g, v)
    preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
    assert preview.recordCount == 2 and preview.newTotal == "6000000000000"
    result = invoke(
        app,
        "maintenance.commitRecalc",
        {**params, "fingerprint": preview.fingerprint, "operationId": str(uuid4())},
        RecalcResponse,
    )
    assert result.newTotal == "6000000000000"
    revisions = (
        app.readers.get().execute("SELECT price_revision_id FROM ledger ORDER BY dedupe_key").fetchall()
    )
    assert revisions[0][0] == revisions[1][0]


@pytest.mark.parametrize("table", ["recalc_item", "wallet_total", "operation", "diagnostic_log"])
def test_commit_failure_rolls_back_ledger_wallet_audit_receipt_and_events(
    app: Application, table: str
) -> None:
    params, _ = setup(app, calibrate=True)
    preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
    snapshots = invoke(app, "wallet.list", {}, WalletListResponse)
    before = app.revisions()
    verb = "UPDATE" if table == "wallet_total" else "INSERT"
    app.writer.execute(
        lambda work: work.connection.execute(
            f"CREATE TRIGGER injected BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'secret'); END"
        )
    )
    app.events.attach(lambda script: None)
    payload = {**params, "fingerprint": preview.fingerprint, "operationId": str(uuid4())}
    error = failure(app, "maintenance.commitRecalc", payload)
    assert error.code == "INTERNAL_ERROR" and "secret" not in error.message
    assert invoke(app, "wallet.list", {}, WalletListResponse) == snapshots
    assert app.revisions() == before and app.events.snapshot() == []
    assert app.readers.get().execute("SELECT pico FROM ledger").fetchone()[0] == "1000000000000"
    assert app.readers.get().execute("SELECT COUNT(*) FROM recalc_batch").fetchone()[0] == 0
    app.writer.execute(lambda work: work.connection.execute("DROP TRIGGER injected"))
    assert invoke(app, "maintenance.commitRecalc", payload, RecalcResponse).recordCount == 1


def test_recalc_keeps_original_vendor_when_target_group_and_links_differ(app: Application) -> None:
    params, original = setup(app)
    other_group, other_version = group(app, "Other")
    rates(app, other_version, "2")
    target = price(
        app, "pricing.addVersion", groupId=other_group, number=2, copyFromVersionId=other_version
    ).versionId
    other = vendor(app, "Other")
    wallet(
        app,
        "wallet.linkGroups",
        vendorId=other.id,
        groupIds=[other_group],
        expectedRevision=app.revisions()["wallet"],
    )
    params.update(groupId=other_group, versionId=target)
    preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
    assert preview.vendors[0].vendorId == original
    invoke(
        app,
        "maintenance.commitRecalc",
        {**params, "fingerprint": preview.fingerprint, "operationId": str(uuid4())},
        RecalcResponse,
    )
    wallets = invoke(app, "wallet.list", {}, WalletListResponse).vendors
    assert next(v for v in wallets if v.id == original).C == "2000000000000"
    assert next(v for v in wallets if v.id == other.id).C == "0"


def test_unpriced_incomplete_counts_and_zero_amount_quality_delta(app: Application) -> None:
    params, supplier = setup(app)
    app.writer.execute(lambda work: work.connection.execute("UPDATE ledger SET unpriced=1,incomplete=1"))
    # Freeze a latest target rate equal to the original amount; only quality changes.
    params["versionId"] = app.readers.get().execute("SELECT price_version_id FROM ledger").fetchone()[0]
    rates(app, str(params["versionId"]), "1")
    preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
    assert preview.unpricedCount == preview.incompleteCount == 0
    invoke(
        app,
        "maintenance.commitRecalc",
        {**params, "fingerprint": preview.fingerprint, "operationId": str(uuid4())},
        RecalcResponse,
    )
    assert (
        next(v for v in invoke(app, "wallet.list", {}, WalletListResponse).vendors if v.id == supplier).C
        == "1000000000000"
    )
    delta = (
        app.readers.get()
        .execute("SELECT amount_pico,unpriced,incomplete FROM wallet_entry WHERE kind='consumption_delta'")
        .fetchone()
    )
    assert tuple(delta) == ("0", 0, 0)


def test_request_time_peak_and_tier_rules_and_missing_usage_quality(app: Application) -> None:
    g, v = group(app)
    rates(
        app,
        v,
        "1",
        billingRule={
            "mode": "peak",
            "rates": {"input": "5", "cache": "0", "output": "0"},
            "weekdays": [3],
            "hours": [6],
            "offsetMinutes": 0,
            "thresholdK": 1,
        },
    )
    price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(12 * 3600 * 1000)
    add_usage(
        app,
        [
            usage(1, at=parse_instant("2026-09-30T06:00:00Z"), tokens=MILLION),
            usage(2, at=parse_instant("2026-09-30T07:00:00Z"), tokens=MILLION),
            usage(3, at=parse_instant("2026-09-30T08:00:00Z"), model=None, tokens=Tokens()),
        ],
    )
    preview = invoke(app, "maintenance.previewRecalc", parameters(app, g, v), RecalcPreview)
    assert preview.newTotal == "6000000000000" and preview.unpricedCount == preview.incompleteCount == 1
    assert (
        app.readers.get()
        .execute("SELECT rule_applied FROM ledger WHERE dedupe_key=?", (bytes.fromhex(f"{1:064x}"),))
        .fetchone()[0]
        == "peak"
    )
    rates(
        app,
        v,
        "1",
        billingRule={
            "mode": "tier",
            "rates": {"input": "7", "cache": "0", "output": "0"},
            "weekdays": [1],
            "hours": [0],
            "offsetMinutes": 0,
            "thresholdK": 1,
        },
    )
    target = price(app, "pricing.addVersion", groupId=g, number=2, copyFromVersionId=v).versionId
    assert target
    preview = invoke(app, "maintenance.previewRecalc", parameters(app, g, target), RecalcPreview)
    assert preview.newTotal == "14000000000000"
