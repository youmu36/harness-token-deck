"""Repricing cannot leave ledger costs outside the wallet or hide zero adjustments."""

from pathlib import Path
from uuid import uuid4

import pytest
from htd.adapters.sqlite.wallet_flows import flush
from htd.adapters.sqlite.wallet_recovery import KEY, install, repair
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import format_instant, parse_instant
from htd.domain.usage.model import Tokens
from htd.ipc.dto.maintenance import RecalcPreview, RecalcResponse
from htd.ipc.dto.wallet import WalletListResponse

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.maintenance_helpers import add_usage
from backend.tests.contracts.management_helpers import group, price, vendor, wallet
from backend.tests.contracts.test_unpriced_commands import fill, recalculate
from backend.tests.contracts.test_unpriced_resolution import resolve
from backend.tests.integration.collection_helpers import usage

AT = parse_instant("2026-10-21T12:00:00Z")
TOKENS = Tokens(1_000_000, 0, 0, 0, 0)


def orphan(app: Application, clock: FakeClock, *, selected: bool = False) -> tuple[str, str, str]:
    g, v = group(app)
    if selected:
        price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
    clock.advance(60_000)
    add_usage(app, [usage(1, at=clock.now_utc(), tokens=TOKENS)])
    assert app.readers.get().execute("SELECT price_group_id FROM ledger").fetchone()[0] == (
        g if selected else None
    )
    clock.advance(60_000)
    supplier = vendor(app, initial="10")
    wallet(
        app,
        "wallet.linkGroups",
        vendorId=supplier.id,
        groupIds=[g],
        expectedRevision=app.revisions()["wallet"],
    )
    fill(app, v)
    resolve(app, "assign", groupId=g, versionId=v)
    return g, v, supplier.id


def state(app: Application, supplier: str):
    return next(v for v in invoke(app, "wallet.list", {}, WalletListResponse).vendors if v.id == supplier)


@pytest.mark.parametrize("selected", [False, True])
def test_new_supplier_at_explicit_repricing_creates_consumption_and_survives_restart(
    tmp_path: Path, selected: bool
) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, _, supplier = orphan(app, clock, selected=selected)
        assert recalculate(app).costDeltaPico == "2000000000000"
        assert state(app, supplier).localWallet.C == "2000000000000"
        assert state(app, supplier).localWallet.B == "8000000000000"
        row = app.readers.get().execute("SELECT * FROM wallet_entry WHERE kind='consumption'").fetchone()
        assert row["recalc_batch_id"] and row["note"] == "补价补记消费"
        clock.advance(600_001)
        app.writer.execute(lambda w: flush(w, w.connection, clock.now_utc()))
        flow = app.readers.get().execute("SELECT * FROM wallet_flow WHERE kind='consumption'").fetchone()
        assert flow["c"] == "2000000000000"
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        assert recalculate(app).recalculated == 0
        assert repair(app.readers, app.writer, clock.now_utc()) == 0
        assert state(app, supplier).localWallet.C == "2000000000000"


@pytest.mark.parametrize("rate", ["1", "2", "3"])
def test_existing_consumption_gets_separate_adjustment_even_when_unchanged(tmp_path: Path, rate: str) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        g, v, supplier = orphan(app, clock)
        recalculate(app)
        clock.advance(600_001)
        app.writer.execute(lambda w: flush(w, w.connection, clock.now_utc()))
        original = dict(
            app.readers.get().execute("SELECT * FROM wallet_flow WHERE kind='consumption'").fetchone()
        )
        fill(app, v, rate)
        target = price(app, "pricing.addVersion", groupId=g, number=2, copyFromVersionId=v).versionId
        assert target
        params = {
            "harness": "codex",
            "start": format_instant(AT),
            "end": format_instant(clock.now_utc()),
            "groupId": g,
            "versionId": target,
        }
        preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
        request = params | {"fingerprint": preview.fingerprint, "operationId": str(uuid4())}
        invoke(app, "maintenance.commitRecalc", request, RecalcResponse)
        invoke(app, "maintenance.commitRecalc", request, RecalcResponse)
        assert (
            dict(
                app.readers.get()
                .execute("SELECT * FROM wallet_flow WHERE id=?", (original["id"],))
                .fetchone()
            )
            == original
        )
        assert app.readers.get().execute("SELECT COUNT(*) FROM wallet_recalc_pending").fetchone()[0] == 1
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        clock.advance(600_001)
        app.writer.execute(lambda w: flush(w, w.connection, clock.now_utc()))
        row = (
            app.readers.get()
            .execute("SELECT * FROM wallet_flow WHERE kind='adjustment' AND note LIKE '重算调整%'")
            .fetchone()
        )
        assert row and int(row["c"]) == (int(rate) - 2) * 10**12
        assert f"2 → {rate} USD" in row["note"]
        assert row["before_c"] == "2000000000000" and int(row["after_c"]) == int(rate) * 10**12
        assert str(int(rate) * 10**12) == state(app, supplier).localWallet.C


@pytest.mark.parametrize("selected", [False, True])
def test_upgrade_recovers_only_audited_missing_consumption_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: bool
) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, _, supplier = orphan(app, clock, selected=selected)
        with monkeypatch.context() as patch:
            patch.setattr("htd.adapters.sqlite.wallet_recovery.append_missing", lambda *args: None)
            recalculate(app)
        assert state(app, supplier).localWallet.C == "0"

        def upgrade(work):
            work.connection.execute("DELETE FROM runtime_state WHERE key=?", (KEY,))
            install(work.connection)

        app.writer.execute(upgrade)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        # Startup uses the existing quiet-period cadence and may have already repaired it.
        assert state(app, supplier).localWallet.C == "2000000000000"
        assert repair(app.readers, app.writer, clock.now_utc()) == 0
        assert state(app, supplier).localWallet.C == "2000000000000"
        assert app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1
        assert app.readers.get().execute("SELECT COUNT(*) FROM recalc_item").fetchone()[0] == 1


def test_zero_adjustment_is_kept_when_no_ledger_field_changes(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        g, v, _ = orphan(app, clock)
        recalculate(app)
        target = price(app, "pricing.addVersion", groupId=g, number=2, copyFromVersionId=v).versionId
        params = {
            "harness": "codex",
            "start": format_instant(AT),
            "end": format_instant(clock.now_utc()),
            "groupId": g,
            "versionId": target,
        }
        for _ in range(2):
            preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
            request = params | {"fingerprint": preview.fingerprint, "operationId": str(uuid4())}
            result = invoke(app, "maintenance.commitRecalc", request, RecalcResponse)
        assert (
            app.readers.get()
            .execute("SELECT changed_count FROM recalc_batch WHERE id=?", (result.batchId,))
            .fetchone()[0]
            == 0
        )
        clock.advance(600_001)
        app.writer.execute(lambda w: flush(w, w.connection, clock.now_utc()))
        rows = (
            app.readers.get()
            .execute("SELECT c,note FROM wallet_flow WHERE kind='adjustment' AND note LIKE '重算调整%'")
            .fetchall()
        )
        assert len(rows) == 2 and all(r["c"] == "0" and "2 → 2 USD" in r["note"] for r in rows)


def test_missing_voucher_failure_rolls_back_repricing_and_can_resume(tmp_path: Path) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, _, supplier = orphan(app, clock)
        app.writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail_recovery BEFORE INSERT ON wallet_entry "
                "WHEN NEW.kind='consumption' BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        assert recalculate(app).errorCode is not None
        assert app.readers.get().execute("SELECT pico,unpriced FROM ledger").fetchone()[:] == ("0", 1)
        assert app.readers.get().execute("SELECT COUNT(*) FROM recalc_batch").fetchone()[0] == 0
        assert state(app, supplier).localWallet.C == "0"
        app.writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail_recovery"))
        assert recalculate(app).recalculated == 1
        assert state(app, supplier).localWallet.C == "2000000000000"
