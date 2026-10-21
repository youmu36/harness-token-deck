"""P4.5 W-series commands, permanent funds dedupe, association history and rollback."""

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.maintenance_repo import MaintenanceRepository
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.application.ports import BilledUsage
from htd.bootstrap import Application
from htd.domain.billing import CostEstimate
from htd.domain.common.money import PICO_PER_USD
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant, parse_instant
from htd.ipc.dto.wallet import EntriesResponse, WalletChangeResponse, WalletListResponse

from backend.tests.integration.collection_helpers import usage

from .command_helpers import failure, invoke
from .management_helpers import catalog, group, price, vendor, wallet

AT = parse_instant("2026-09-30T10:00:00Z")


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Application]:
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as instance:
        yield instance


def balances(app: Application, vendor_id: str) -> tuple[int, int, int, int]:
    found = next(
        v
        for v in invoke(app, "wallet.list", {"includeArchived": True}, WalletListResponse).vendors
        if v.id == vendor_id
    )
    return (
        int(found.R) // PICO_PER_USD,
        int(found.C) // PICO_PER_USD,
        int(found.A) // PICO_PER_USD,
        int(found.B) // PICO_PER_USD,
    )


def cost(
    app: Application, group_id: str, index: int, amount: int, *, at: Instant = AT, unpriced: bool = False
) -> None:
    event = usage(index, at=at)
    billed = BilledUsage(event, CostEstimate(amount * PICO_PER_USD, False, unpriced, "base"), group_id)
    app.writer.execute(
        lambda work: WalletRepository(work.connection).append_consumption(
            work, billed, at=app.clock.now_utc()
        )
    )


def link(app: Application, vendor_id: str, groups: list[str]) -> WalletChangeResponse:
    return wallet(
        app,
        "wallet.linkGroups",
        vendorId=vendor_id,
        groupIds=groups,
        expectedRevision=app.revisions()["wallet"],
    )


def test_W01_to_W04_topups_consumption_calibration_and_retry(app: Application) -> None:
    g, _ = group(app)
    v = vendor(app)
    link(app, v.id, [g])
    wallet(app, "wallet.topUp", vendorId=v.id, amount="100")
    cost(app, g, 1, 30)
    assert balances(app, v.id) == (100, 30, 0, 70)
    calibrated = wallet(
        app, "wallet.calibrate", vendorId=v.id, target="65", expectedWalletRevision=app.revisions()["wallet"]
    )
    assert balances(app, v.id) == (100, 30, -5, 65)
    assert calibrated.vendor and calibrated.vendor.lastCalibratedAt == "2026-09-30T10:00:00.000Z"
    payload = {"vendorId": v.id, "amount": "20", "operationId": str(uuid4())}
    first = app.router.call("wallet.topUp", payload)
    assert first["ok"]
    cost(app, g, 2, 2, at=parse_instant("2026-09-30T10:01:00Z"))
    assert balances(app, v.id) == (120, 32, -5, 83)
    assert app.router.call("wallet.topUp", payload) == first
    assert balances(app, v.id) == (120, 32, -5, 83)
    assert failure(app, "wallet.topUp", payload | {"amount": "21"}).code == "OPERATION_ID_REUSED"
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(86400000)
    page = invoke(app, "wallet.entries", {"vendorId": v.id}, EntriesResponse)
    assert any(e.calibration is not None and e.kind == "adjustment" for e in page.entries)
    audit = app.writer.execute(lambda w: WalletRepository(w.connection).calibrations(v.id)[0])
    assert audit.target_pico == 65 * PICO_PER_USD and audit.observed_balance_pico == 70 * PICO_PER_USD
    assert audit.watermark_seq == 2
    daily = next(e for e in page.entries if e.kind == "consumption_daily")
    assert daily.amountPico == "32000000000000" and daily.count == 2


@pytest.mark.parametrize(
    ("initial", "balance", "calibrated"),
    [("-0.5", "-500000000000", True), (None, "0", False), ("", "0", False)],
)
def test_W05_W06_initial_balance(
    app: Application, initial: str | None, balance: str, calibrated: bool
) -> None:
    v = vendor(app, initial=initial)
    assert v.R == v.C == "0" and v.A == v.B == balance
    assert (v.lastCalibratedAt is not None) == calibrated
    assert len(invoke(app, "wallet.entries", {"vendorId": v.id}, EntriesResponse).entries) == int(calibrated)


def test_W07_late_consumption_offsets_once_and_reports_quality(app: Application) -> None:
    g, _ = group(app)
    v = vendor(app)
    link(app, v.id, [g])
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(120000)
    wallet(
        app, "wallet.calibrate", vendorId=v.id, target="50", expectedWalletRevision=app.revisions()["wallet"]
    )
    cost(app, g, 1, 3, at=parse_instant("2026-09-30T10:01:00Z"), unpriced=True)
    cost(app, g, 1, 3, at=parse_instant("2026-09-30T10:01:00Z"), unpriced=True)
    assert balances(app, v.id) == (0, 3, 53, 50)
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(86400000)
    page = invoke(app, "wallet.entries", {"vendorId": v.id}, EntriesResponse)
    assert len(page.entries) == 3
    assert next(e for e in page.entries if e.kind == "consumption_daily").amountPico == "3000000000000"
    assert sum(e.kind == "adjustment" for e in page.entries) == 2
    snapshot = invoke(app, "wallet.list", {}, WalletListResponse).vendors[0]
    assert snapshot.coverageIncomplete and snapshot.unpricedCount == 1


def test_W08_association_boundary_transfer_and_unlink_events(app: Application) -> None:
    g, _ = group(app)
    old, new = vendor(app, "Old"), vendor(app, "New")
    link(app, old.id, [g])
    cost(app, g, 1, 5, at=parse_instant("2026-09-30T09:59:00Z"))
    cost(app, g, 2, 3)
    assert balances(app, old.id)[1] == 3
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(60000)
    events: list[object] = []
    app.bus.subscribe(events.append)
    moved = link(app, new.id, [g])
    assert moved.vendor and moved.vendor.linkedGroups == [g]
    snapshots = invoke(app, "wallet.list", {}, WalletListResponse).vendors
    assert snapshots[0].linkedGroups == [] and snapshots[1].linkedGroups == [g]
    links = app.readers.get().execute("SELECT vendor_id FROM vendor_link ORDER BY seq").fetchall()
    assert [row[0] for row in links] == [old.id, None, new.id]
    cost(app, g, 3, 2, at=app.clock.now_utc())
    assert balances(app, old.id)[1] == 3 and balances(app, new.id)[1] == 2
    from htd.application.ports import WalletChanged

    assert any(isinstance(e, WalletChanged) and set(e.vendor_ids) == {old.id, new.id} for e in events)
    assert catalog(app).groups[0].vendorId == new.id
    assert not link(app, new.id, [g]).changed
    link(app, new.id, [])
    assert catalog(app).groups[0].vendorId is None


def test_W09_calibration_conflict_and_zero_delta_audit(app: Application) -> None:
    v = vendor(app)
    revision = app.revisions()["wallet"]
    wallet(app, "wallet.topUp", vendorId=v.id, amount="100")
    error = failure(
        app,
        "wallet.calibrate",
        {"vendorId": v.id, "target": "50", "expectedWalletRevision": revision, "operationId": str(uuid4())},
    )
    assert error.code == "WALLET_REVISION_CONFLICT" and error.conflictRevision == app.revisions()["wallet"]
    assert error.field == "expectedWalletRevision" and balances(app, v.id) == (100, 0, 0, 100)
    zero = wallet(
        app, "wallet.calibrate", vendorId=v.id, target="100", expectedWalletRevision=app.revisions()["wallet"]
    )
    assert zero.changed and zero.vendor and zero.vendor.lastCalibratedAt is not None
    entries = invoke(app, "wallet.entries", {"vendorId": v.id}, EntriesResponse).entries
    assert entries[0].amountPico == "100000000000000" and entries[0].kind == "top_up"
    assert entries[0].calibration is None


def test_delete_archive_restore_and_active_name_conflict(app: Application) -> None:
    g, _ = group(app)
    empty = vendor(app, "Empty")
    link(app, empty.id, [g])
    deleted = wallet(app, "wallet.deleteVendor", vendorId=empty.id)
    assert deleted.action == "delete" and deleted.vendor is None
    assert catalog(app).groups[0].vendorId is None
    funded = vendor(app, "Funded", "0")
    link(app, funded.id, [g])
    archived = wallet(app, "wallet.deleteVendor", vendorId=funded.id)
    assert archived.action == "archive" and archived.vendor and archived.vendor.status == "archived"
    assert invoke(app, "wallet.list", {}, WalletListResponse).vendors == []
    assert len(invoke(app, "wallet.list", {"includeArchived": True}, WalletListResponse).vendors) == 1
    assert (
        failure(app, "wallet.topUp", {"vendorId": funded.id, "amount": "1", "operationId": str(uuid4())}).code
        == "VENDOR_ARCHIVED"
    )
    replacement = vendor(app, "Funded")
    assert (
        failure(app, "wallet.restoreVendor", {"vendorId": funded.id, "operationId": str(uuid4())}).code
        == "VENDOR_NAME_CONFLICT"
    )
    wallet(app, "wallet.renameVendor", vendorId=replacement.id, name="Replacement")
    restored = wallet(app, "wallet.restoreVendor", vendorId=funded.id)
    assert restored.vendor and restored.vendor.linkedGroups == [] and restored.vendor.status == "active"


@pytest.mark.parametrize(
    "command", ["wallet.createVendor", "wallet.topUp", "wallet.calibrate", "wallet.linkGroups"]
)
def test_failed_receipt_rolls_back_vendor_vouchers_links_and_revisions(
    app: Application, command: str
) -> None:
    g, _ = group(app)
    v = vendor(app)
    before = app.revisions()
    app.events.attach(lambda script: None)
    app.writer.execute(
        lambda work: work.connection.execute(
            "CREATE TRIGGER injected BEFORE INSERT ON operation BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    )
    payload: dict[str, object] = {"operationId": str(uuid4())}
    if command == "wallet.createVendor":
        payload.update(name="New", initialBalance="50")
    elif command == "wallet.topUp":
        payload.update(vendorId=v.id, amount="10")
    elif command == "wallet.calibrate":
        payload.update(vendorId=v.id, target="50", expectedWalletRevision=before["wallet"])
    else:
        payload.update(vendorId=v.id, groupIds=[g], expectedRevision=before["wallet"])
    assert failure(app, command, payload).code == "INTERNAL_ERROR"
    assert app.revisions() == before and app.events.snapshot() == []
    assert len(invoke(app, "wallet.list", {}, WalletListResponse).vendors) == 1
    assert invoke(app, "wallet.entries", {"vendorId": v.id}, EntriesResponse).entries == []
    assert catalog(app).groups[0].vendorId is None


def test_topup_full_receipt_restart_and_permanent_voucher_after_receipt_expiry(
    app: Application, tmp_path: Path
) -> None:
    v = vendor(app)
    payload = {"vendorId": v.id, "amount": "10", "operationId": str(uuid4())}
    first = app.router.call("wallet.topUp", payload)
    assert first["ok"]
    app.close()
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as restarted:
        assert restarted.router.call("wallet.topUp", payload) == first
        restarted.writer.execute(
            lambda work: work.connection.execute(
                "DELETE FROM operation WHERE id=?", (payload["operationId"],)
            )
        )
        retried = invoke(restarted, "wallet.topUp", payload, WalletChangeResponse)
        assert not retried.changed and retried.vendor and retried.vendor.R == "10000000000000"


def test_entries_keyset_cursor_daily_exactness_and_concurrent_append(app: Application) -> None:
    g, _ = group(app)
    v = vendor(app)
    link(app, v.id, [g])
    cost(app, g, 1, 999999999)
    cost(app, g, 2, 999999999)
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(86400000)
    wallet(app, "wallet.topUp", vendorId=v.id, amount="1")
    page1 = invoke(app, "wallet.entries", {"vendorId": v.id, "limit": 1}, EntriesResponse)
    assert page1.nextCursor is not None
    wallet(app, "wallet.topUp", vendorId=v.id, amount="2")
    cost(app, g, 3, 1)
    page2 = invoke(
        app, "wallet.entries", {"vendorId": v.id, "limit": 1, "cursor": page1.nextCursor}, EntriesResponse
    )
    assert page2.nextCursor is None and page2.entries[0].kind == "consumption_daily"
    assert page2.entries[0].amountPico == "1999999998000000000000" and page2.entries[0].count == 2
    corrections=invoke(app,"wallet.entries",{"vendorId":v.id},EntriesResponse)
    assert any("1999999998000000000000 → 1999999999000000000000" in e.note for e in corrections.entries)


@pytest.mark.parametrize(
    "payload", [{"amount": "0"}, {"amount": "-1"}, {"amount": 1}, {"amount": "1.0000001"}]
)
def test_invalid_topup_does_not_change_wallet(app: Application, payload: dict[str, object]) -> None:
    v = vendor(app)
    before = app.revisions()
    error = failure(app, "wallet.topUp", {"vendorId": v.id, "operationId": str(uuid4()), **payload})
    assert error.code in {"WALLET_INVALID", "INVALID_REQUEST", "AMOUNT_INVALID"}
    assert app.revisions() == before


def test_W10_real_ledger_retention_preserves_wallet_snapshot_and_vouchers(app: Application) -> None:
    g, v = group(app)
    price(
        app,
        "pricing.saveVersionPrices",
        versionId=v,
        models=[
            {"modelId": "Model", "input": "300000", "cache": "300000", "output": "0"}
        ],
    )
    price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
    supplier = vendor(app)
    link(app, supplier.id, [g])
    wallet(app, "wallet.topUp", vendorId=supplier.id, amount="100")
    root = app.store.sources().sources[0].path
    from htd.domain.usage.model import Harness

    app.writer.execute(
        lambda work: IngestRepository(work.connection).commit_batch(
            work, Harness.CODEX, root, root + "/file", {"offset": 1}, [usage(1, at=AT)], at=AT
        )
    )
    app.scan.billing.run()
    before = invoke(app, "wallet.list", {}, WalletListResponse)
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(86400000)
    entries = invoke(app, "wallet.entries", {"vendorId": supplier.id}, EntriesResponse)
    assert balances(app, supplier.id) == (100, 30, 0, 70)
    assert (
        app.writer.execute(
            lambda work: MaintenanceRepository(work.connection).delete_ledger_batch(
                work, parse_instant("2026-10-01T00:00:00Z")
            )
        )
        == 1
    )
    assert invoke(app, "wallet.list", {}, WalletListResponse) == before
    assert invoke(app, "wallet.entries", {"vendorId": supplier.id}, EntriesResponse) == entries
