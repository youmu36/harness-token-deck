"""Reliable interrupted usage reaches acceptance, valuation, original wallet and queries once."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from htd.adapters.collectors.discovery import Discovery
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import format_instant, parse_instant
from htd.ipc.dto.maintenance import RecalcPreview, RecalcResponse
from htd.ipc.dto.reconciliation import CalibrationPreview, CalibrationResponse
from htd.ipc.dto.statistics import SummaryResponse
from htd.ipc.dto.wallet import WalletListResponse

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.management_helpers import group, price, vendor, wallet

from .scan_helpers import headers, request
from .test_reconciliation_batches import configured

AT = parse_instant("2026-09-01T00:00:00.000Z")
USAGE_AT = parse_instant("2026-09-01T00:00:02.000Z")


def status(kind: str) -> bytes:
    return (
        json.dumps(
            {
                "timestamp": "2026-09-01T00:00:01.000Z",
                "type": "response_item" if kind == "user_steer" else "event_msg",
                "payload": {"type": "message", "role": "user", "content": []}
                if kind == "user_steer"
                else {"type": kind, "turn_id": "turn-1"},
            }
        )
        + "\n"
    ).encode()


def test_requests_on_both_sides_of_a_steer_survive_partial_write_restart_and_archive_copy(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    source = home / ".codex/sessions"
    source.mkdir(parents=True)
    path = source / "steer.jsonl"
    path.write_bytes(headers())
    clock = FakeClock(AT)
    discovery = Discovery(home=home, environ={})

    # Use a synthetic model with a known one-dollar/million input price.
    def model_record(index: int) -> bytes:
        row = json.loads(request(index, USAGE_AT))
        row["payload"]["usage"] = {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10}
        return (json.dumps(row) + "\n").encode()

    context = json.loads(headers().splitlines()[1])
    context["payload"]["model"] = "Model"
    path.write_bytes(headers().splitlines(keepends=True)[0] + (json.dumps(context) + "\n").encode())
    third = model_record(3)
    with Application(tmp_path / "data", clock=clock, discovery=discovery, start_scheduler=False) as app:
        configured(app)
        app.scan.collect()
        with path.open("ab") as stream:
            stream.write(model_record(1) + status("user_steer") + model_record(2) + third[: len(third) // 2])
        clock.advance(3000)
        app.scan.run()
        assert app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2
    with path.open("ab") as stream:
        stream.write(third[len(third) // 2 :])
    archive = source.parent / "archived_sessions"
    archive.mkdir()
    (archive / "copy.jsonl").write_bytes(path.read_bytes())
    with Application(tmp_path / "data", clock=clock, discovery=discovery, start_scheduler=False) as app:
        app.scan.run()
        app.scan.run()
        rows = app.readers.get().execute("SELECT input,cache_read,output,pico FROM ledger").fetchall()
        assert len(rows) == 3
        assert tuple(sum(int(row[index]) for row in rows) for index in range(3)) == (300, 120, 30)
        assert sum(int(row[3]) for row in rows) == 180_000_000


@pytest.mark.parametrize("lifecycle", [None, "turn_aborted", "task_complete", "user_steer"])
@pytest.mark.parametrize("delayed", [False, True])
def test_interrupted_usage_wallet_projection_half_line_replay_and_restart(
    tmp_path: Path, lifecycle: str | None, delayed: bool
) -> None:
    home = tmp_path / "home"
    source = home / ".codex/sessions"
    source.mkdir(parents=True)
    path = source / "session.jsonl"
    path.write_bytes(headers())
    clock = FakeClock(AT)
    with Application(
        tmp_path / "data", clock=clock, discovery=Discovery(home=home, environ={}), start_scheduler=False
    ) as app:
        g, v = group(app)
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=v,
            models=[
                {
                    "modelId": "gpt-6-astra",
                    "input": "2",
                    "cache": "0.25",
                    "cacheWrite": "0",
                    "output": "10",
                }
            ],
        )
        price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
        supplier = vendor(app, "Synthetic supplier", "100")
        wallet(
            app,
            "wallet.linkGroups",
            vendorId=supplier.id,
            groupIds=[g],
            expectedRevision=app.revisions()["wallet"],
        )
        app.scan.collect()
        if lifecycle:
            with path.open("ab") as stream:
                stream.write(status(lifecycle))
        data = request(1, USAGE_AT)
        clock.advance(3000)
        if delayed:
            with path.open("ab") as stream:
                stream.write(data[: len(data) // 2])
            app.scan.run()
            assert app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
            app.close()
            with Application(
                tmp_path / "data",
                clock=clock,
                discovery=Discovery(home=home, environ={}),
                start_scheduler=False,
            ) as restarted:
                with path.open("ab") as stream:
                    stream.write(data[len(data) // 2 :])
                restarted.scan.run()
        else:
            with path.open("ab") as stream:
                stream.write(data)
            app.scan.run()
    with Application(
        tmp_path / "data", clock=clock, discovery=Discovery(home=home, environ={}), start_scheduler=False
    ) as app:
        archive = source.parent / "archived_sessions"
        archive.mkdir()
        (archive / "copy.jsonl").write_bytes(path.read_bytes())
        app.scan.run()
        app.scan.run()
        result = invoke(
            app,
            "stats.summary",
            {
                "harness": "codex",
                "start": format_instant(AT),
                "end": format_instant(clock.now_utc()),
                "offsetMinutes": 0,
            },
            SummaryResponse,
        )
        assert (
            result.metrics.recordCount,
            result.metrics.inputTokens,
            result.metrics.cacheReadTokens,
            result.metrics.outputTokens,
            result.metrics.pendingValuationCount,
        ) == (1, 100, 40, 10, 0)
        # Independent price: (100-40)*2e6 + 40*0.25e6 + 10*10e6.
        assert result.metrics.picoUsd == "230000000"
        current = next(
            item
            for item in invoke(app, "wallet.list", {}, WalletListResponse).vendors
            if item.id == supplier.id
        )
        assert current.C == "230000000" and current.localWallet and current.localWallet.B == "99999770000000"
        assert (
            app.readers.get()
            .execute("SELECT COUNT(*) FROM wallet_entry WHERE kind='consumption'")
            .fetchone()[0]
            == 1
        )
        assert app.readers.get().execute("SELECT records,estimate FROM session_total").fetchone()[:] == (
            "1",
            "230000000",
        )
        if lifecycle == "turn_aborted":
            assert app.readers.get().execute("SELECT duration_ms FROM output_session").fetchone()[0] == "0"


def test_cumulative_snapshot_and_abort_without_request_usage_never_create_consumption(tmp_path: Path) -> None:
    home = tmp_path / "home"
    source = home / ".codex/sessions"
    source.mkdir(parents=True)
    snapshot = (
        json.dumps(
            {
                "timestamp": format_instant(USAGE_AT),
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 999999, "output_tokens": 999999}},
                },
            }
        )
        + "\n"
    ).encode()
    (source / "session.jsonl").write_bytes(headers() + status("turn_aborted") + snapshot)
    with Application(
        tmp_path / "data",
        clock=FakeClock(AT),
        discovery=Discovery(home=home, environ={}),
        start_scheduler=False,
    ) as app:
        app.scan.run()
        assert app.readers.get().execute("SELECT COUNT(*) FROM identity").fetchone()[0] == 0
        assert app.readers.get().execute("SELECT COUNT(*) FROM wallet_entry").fetchone()[0] == 0


def test_interrupted_request_locked_target_reprice_and_restart_conserve_money_and_tokens(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    source = home / ".codex/sessions"
    source.mkdir(parents=True)
    path = source / "session.jsonl"
    path.write_bytes(headers())
    clock = FakeClock(AT)
    with Application(
        tmp_path / "data", clock=clock, discovery=Discovery(home=home, environ={}), start_scheduler=False
    ) as app:
        g, v = group(app)
        base = {"modelId": "gpt-6-astra", "input": "2", "cache": "0.25", "output": "10"}
        price(app, "pricing.saveVersionPrices", versionId=v, models=[base])
        price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
        supplier = vendor(app, "Synthetic supplier", "100")
        wallet(
            app,
            "wallet.linkGroups",
            vendorId=supplier.id,
            groupIds=[g],
            expectedRevision=app.revisions()["wallet"],
        )
        app.scan.collect()
        with path.open("ab") as stream:
            stream.write(status("turn_aborted") + request(1, USAGE_AT))
        clock.advance(3000)
        app.scan.run()
        clock.advance(11 * 60_000)
        scope = {"start": format_instant(AT), "end": format_instant(clock.now_utc())}
        params = dict(
            scope,
            vendorId=supplier.id,
            targetPico="300000000",
            confirmLocalScope=True,
            acknowledgeIncomplete=True,
        )
        preview = invoke(app, "reconciliation.previewCalibration", params, CalibrationPreview)
        assert preview.currentEstimatePico == "230000000"
        target = invoke(
            app,
            "reconciliation.commitCalibration",
            dict(params, fingerprint=preview.fingerprint, operationId=str(uuid4())),
            CalibrationResponse,
        )
        assert target.deltaPico == "70000000"
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=v,
            models=[dict(base, input="4", cache="0.5", output="20")],
        )
        recalc = dict(scope, harness="codex", groupId=g, versionId=v)
        preview_recalc = invoke(app, "maintenance.previewRecalc", recalc, RecalcPreview)
        assert preview_recalc.newTotal == "460000000"
        invoke(
            app,
            "maintenance.commitRecalc",
            dict(recalc, fingerprint=preview_recalc.fingerprint, operationId=str(uuid4())),
            RecalcResponse,
        )
    with Application(
        tmp_path / "data", clock=clock, discovery=Discovery(home=home, environ={}), start_scheduler=False
    ) as app:
        app.scan.run()
        result = invoke(app, "stats.summary", dict(scope, harness="codex", offsetMinutes=0), SummaryResponse)
        assert (
            result.metrics.recordCount,
            result.metrics.inputTokens,
            result.metrics.cacheReadTokens,
            result.metrics.outputTokens,
        ) == (1, 100, 40, 10)
        assert (result.metrics.estimatePico, result.metrics.correctionPico, result.metrics.picoUsd) == (
            "460000000",
            "-160000000",
            "300000000",
        )
        current = next(
            row for row in invoke(app, "wallet.list", {}, WalletListResponse).vendors if row.id == supplier.id
        )
        assert current.localWallet is not None
        assert (current.localWallet.C, current.localWallet.B) == ("300000000", "99999700000000")
        db = app.readers.get()
        assert db.execute("SELECT COUNT(*) FROM identity").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM wallet_entry WHERE kind='consumption'").fetchone()[0] == 1
        assert db.execute(
            "SELECT estimate_pico,delta_pico,target_pico FROM calibration_target WHERE id=?",
            (target.targetId,),
        ).fetchone()[:] == ("460000000", "-160000000", "300000000")
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
