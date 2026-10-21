"""Record successful calls for every P4 command using one synthetic temporary ledger."""

import json
import os
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import cast
from uuid import uuid4

import pytest
from htd.application.ports import TaskDone
from htd.application.remote.ports import MemoryCredentialStore
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.remote.model import BalanceObservation, RemoteAccount
from htd.domain.usage.model import Harness

from backend.tests.integration.collection_helpers import usage
from backend.tests.integration.scan_helpers import headers, request
from backend.tests.unit.remote.test_refresh import Provider

from .maintenance_helpers import add_usage
from .recording_helpers import Recorder

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures/ipc/responses.json"


def legacy_stopped(**_: object) -> bool:
    return False


class RecordingProvider(Provider):
    async def fetch(self, account: RemoteAccount, credential: str) -> BalanceObservation:
        value = await super().fetch(account, credential)
        # A real synthetic balance movement is necessary for an interval;
        # unchanged polls intentionally do not create zero-value vouchers.
        return replace(value, amount_pico=1_975_310_000_001 - int(self.clock.monotonic() >= 600) * 10**12)


def test_record_all_registered_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("htd.migration.legacy.reader.legacy_running", legacy_stopped)
    monkeypatch.setattr("htd.migration.engine.legacy_running", legacy_stopped)
    clock = FakeClock(parse_instant("2026-10-01T10:00:00.000Z"))
    legacy = Path(__file__).resolve().parents[3] / "fixtures/legacy/v022.sqlite3"
    with Application(
        tmp_path / "data",
        clock=clock,
        start_scheduler=False,
        credentials=MemoryCredentialStore(),
        provider_factory=lambda: RecordingProvider(clock),
    ) as app:
        recorder = Recorder(app, tmp_path)

        def write(command: str, **payload: object) -> dict[str, object]:
            return recorder.call(command, {"operationId": str(uuid4()), **payload})

        def price(command: str, **payload: object) -> dict[str, object]:
            return write(
                command,
                expectedRevision=app.revisions()["models" if command.startswith("models.") else "pricing"],
                **payload,
            )

        def task(command: str, **payload: object) -> dict[str, object]:
            done = Event()
            unsubscribe = app.bus.subscribe(lambda event: done.set() if isinstance(event, TaskDone) else None)
            try:
                started = write(command, **payload)
                assert done.wait(5), command
                result = recorder.call("tasks.get", {"taskId": started["taskId"]})
                assert result["status"] == "completed", result
                return result
            finally:
                unsubscribe()

        recorder.call("app.hello", {"clientProtocol": 1})
        recorder.error("app.pickPath", {"kind": "dir"}, "DESKTOP_REQUIRED")
        recorder.error("app.openPath", {"kind": "dataDir"}, "DESKTOP_REQUIRED")
        app.system_actions.pick = lambda kind, initial: str(tmp_path / "synthetic sessions")
        app.system_actions.open = lambda kind: None
        recorder.call("app.pickPath", {"kind": "dir"})
        recorder.call("app.openPath", {"kind": "dataDir"})
        recorder.call("preferences.get")
        recorder.call("reconciliation.status")
        recorder.call(
            "reconciliation.dismiss",
            {"vendorId": "synthetic-vendor", "observedAt": format_instant(clock.now_utc())},
        )
        recorder.call("pricing.unpriced")
        recorder.call("pricing.unpricedNotice")
        recorder.call("stats.exploreOptions")
        recorder.call("stats.outputReferences")
        recorder.call(
            "stats.explore", {"start": "2026-10-01T00:00:00.000Z", "end": "2026-10-02T00:00:00.000Z"}
        )
        recorder.call("stats.acknowledgeQuality", {"items": []})
        write("pricing.recalculateUnpriced", expectedPricingRevision=app.revisions()["pricing"])
        write("preferences.update", patch={"theme": "dark", "historyPrompted": True}, expectedRevision=0)
        recorder.error(
            "preferences.update",
            {"patch": {}, "expectedRevision": 0, "operationId": str(uuid4())},
            "PREFERENCES_REVISION_CONFLICT",
        )
        recorder.error("app.hello", {"clientProtocol": 1, "extra": True}, "INVALID_REQUEST")
        recorder.call("sources.list")
        write("sources.autoDetect", expectedRevision=app.revisions()["sources"])
        sessions = tmp_path / "synthetic sessions"
        sessions.mkdir()
        write(
            "sources.setPath",
            harness="codex",
            path=str(sessions),
            expectedRevision=app.revisions()["sources"],
        )
        recorder.call("sources.validatePath", {"harness": "codex", "path": str(sessions)})
        app.scheduler.start()
        write("collection.scanNow")
        write("collection.setPaused", paused=True)
        recorder.call("collection.status")

        created = price("pricing.createGroup", name="Fixture Prices")
        group_id, version_id = created["groupId"], created["versionId"]
        price("pricing.renameGroup", groupId=group_id, name="Recorded Prices")
        price("pricing.setGroupDisabled", groupId=group_id, disabled=True)
        price("pricing.setGroupDisabled", groupId=group_id, disabled=False)
        added = price("pricing.addVersion", groupId=group_id, number=2, label="Second")
        price("pricing.activateVersion", groupId=group_id, versionId=added["versionId"])
        price("pricing.setVersionLabel", versionId=version_id, label="Recorded")
        model = {
            "modelId": "gpt-6-astra",
            "input": "1.5",
            "cache": "0",
            "output": "2",
        }
        price("pricing.saveVersionPrices", versionId=version_id, models=[model], hints=[])
        switch = {"harness": "codex", "groupId": group_id, "versionId": version_id}
        recorder.call("pricing.previewSwitch", switch)
        price("pricing.switchCurrent", **switch)
        price("pricing.setVersionDisabled", versionId=added["versionId"], disabled=True)
        price("pricing.deleteVersion", versionId=added["versionId"])
        removable = price("pricing.createGroup", name="Remove Me")
        price("pricing.deleteGroup", groupId=removable["groupId"])
        price("models.add", modelIds=["gpt-6-astra", "excluded-model"])
        price("models.saveBaseRates", models=[{"modelId": "gpt-6-astra", "rates": {"input": "1.5"}}])
        price("models.remove", modelId="excluded-model")
        price("models.restore", modelIds=["excluded-model"])
        recorder.call("models.pool")
        recorder.call(
            "models.fillByMultiplier",
            {"rates": {"input": "1.5", "cache": "0", "output": None}, "multiplier": "1.2"},
        )
        scanned = task("models.scan", harness="codex")
        write("tasks.cancel", taskId=scanned["taskId"])

        vendor = write("wallet.createVendor", name="Fixture Vendor", initialBalance="-0.5")
        vendor_id = vendor["vendorId"]
        write("wallet.renameVendor", vendorId=vendor_id, name="Recorded Vendor")
        write(
            "wallet.linkGroups",
            vendorId=vendor_id,
            groupIds=[group_id],
            expectedRevision=app.revisions()["wallet"],
        )
        write("wallet.topUp", vendorId=vendor_id, amount="10000.123456")
        account_entries = recorder.call("wallet.accountEntries", {"vendorId": vendor_id})
        entries = cast(list[dict[str, object]], account_entries["entries"])
        original = next(row for row in entries if row["kind"] == "top_up")
        write(
            "wallet.reclassify",
            vendorId=vendor_id,
            entryId=original["id"],
            amount="1",
            targetKind="adjustment",
            note="人工改判",
            expectedAccountRevision=app.revisions()["account"],
        )
        write(
            "wallet.correctConsumption",
            vendorId=vendor_id,
            target="1",
            note="人工校准",
            expectedAccountRevision=app.revisions()["account"],
        )
        write(
            "wallet.calibrate",
            vendorId=vendor_id,
            target="8.75",
            expectedWalletRevision=app.revisions()["wallet"],
        )
        write("wallet.deleteVendor", vendorId=vendor_id)
        write("wallet.restoreVendor", vendorId=vendor_id)
        recorder.call("wallet.list")
        recorder.call("wallet.entries", {"vendorId": vendor_id, "limit": 1})
        recorder.call("wallet.entries", {"vendorId": vendor_id})
        recorder.call("remote.get", {"vendorId": vendor_id})
        remote_settings = {
            "vendorId": vendor_id,
            "baseUrl": "https://example.test",
            "siteType": "newapi",
            "authMode": "access_token",
            "userId": "42",
            "credential": "synthetic-remote-1234",
            "enabled": False,
        }
        recorder.call("remote.test", remote_settings)
        write("remote.save", **remote_settings)
        write("remote.refresh", vendorId=vendor_id)
        clock.advance(600_000)
        # Record real funding/consumption contributions; unchanged observations no longer post zero vouchers.
        write("wallet.topUp", vendorId=vendor_id, amount="1", walletScope="remote")
        write("remote.refresh", vendorId=vendor_id)
        clock.advance(600_000)
        app.remote.invoke(lambda service: service.store.flush_pending(clock.now_utc()))
        account_entries = recorder.call("wallet.accountEntries", {"vendorId": vendor_id})
        interval = next(
            row["intervalId"]
            for row in cast(list[dict[str, object]], account_entries["entries"])
            if row["intervalId"]
        )
        preview_interval = recorder.call(
            "wallet.previewReconcileInterval", {"vendorId": vendor_id, "intervalId": interval}
        )
        write(
            "wallet.reconcileInterval",
            vendorId=vendor_id,
            intervalId=interval,
            fingerprint=preview_interval["fingerprint"],
            expectedAccountRevision=app.revisions()["account"],
        )
        write("remote.delete", vendorId=vendor_id)
        write(
            "wallet.setLocalTotals",
            vendorId=vendor_id,
            expectedWalletRevision=app.revisions()["wallet"],
            pastTopUpPico="123",
        )
        recorder.call("wallet.dailyBook", {"vendorId": vendor_id, "scope": "local", "month": "2026-10"})
        write("wallet.setVisibility", vendorId=vendor_id, mode="both")
        local_entries = recorder.call("wallet.accountEntries", {"vendorId": vendor_id, "scope": "local"})
        local_row = next(
            row for row in cast(list[dict[str, object]], local_entries["entries"]) if row["kind"] == "top_up"
        )
        write(
            "wallet.correctEntry",
            vendorId=vendor_id,
            entryId=local_row["id"],
            scope="local",
            R=local_row["R"],
            C=local_row["C"],
            expectedEntryRevision=local_row["entryRevision"],
        )
        write(
            "wallet.dismissNotice", vendorId=vendor_id, entryId=local_row["id"], noticeId="synthetic-notice"
        )

        clock.advance(1000)
        at = clock.now_utc()
        (sessions / "fixture.jsonl").write_bytes(headers() + request(1, at))
        (Path(os.environ["HTD_SOURCE_ROOT_CLAUDE"]) / "fixture.jsonl").write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "fixture-claude",
                    "timestamp": "2026-09-30T18:00:00Z",
                    "providerName": "https://fixture-provider.example",
                    "message": {
                        "id": "fixture-message",
                        "model": "claude-sonnet-4-6",
                        "usage": {
                            "input_tokens": 100,
                            "cache_read_input_tokens": 40,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": 10,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        providers = task("wallet.scanProviders")
        provider_result = cast(dict[str, object], providers["result"])
        candidate = cast(list[dict[str, object]], provider_result["providers"])[0]
        write(
            "wallet.createScannedVendors",
            taskId=providers["taskId"],
            providers=[{"id": candidate["id"], "name": "Scanned Fixture"}],
        )
        write("collection.setPaused", paused=False)
        write("collection.scanNow")
        add_usage(app, [usage(100, harness=Harness.CLAUDE, at=clock.now_utc())])
        pending = recorder.call("pricing.unpriced")
        items = cast(list[dict[str, object]], pending["items"])
        assert items
        write(
            "pricing.resolveUnpriced",
            action="ignore",
            itemIds=[items[0]["id"]],
            expectedPricingRevision=app.revisions()["pricing"],
            expectedUsageRevision=app.revisions()["quality"],
        )
        window: dict[str, object] = {
            "harness": "codex",
            "start": format_instant(Instant(at.value - timedelta(seconds=1))),
            "end": format_instant(Instant(at.value + timedelta(seconds=1))),
        }
        recorder.call("stats.auxiliaryCosts", window)
        recorder.call("stats.longTerm", window)
        recorder.call("stats.analytics", {"harness": "all", "period": "all", "limit": 1})
        recorder.call("stats.summary", {**window, "offsetMinutes": 480, "grain": "hour"})
        recorder.call("stats.cacheMonitor", {"harness": "codex", "mode": "minute", "offsetMinutes": 480})
        clock.advance(1)
        price("pricing.saveVersionPrices", versionId=version_id, models=[{**model, "input": "2"}])
        recalc = {**window, "groupId": group_id, "versionId": version_id}
        preview = recorder.call("maintenance.previewRecalc", recalc)
        assert preview["recordCount"] == 1
        write("maintenance.commitRecalc", **recalc, fingerprint=preview["fingerprint"])
        recorder.call("pricing.catalog")
        recorder.call("maintenance.previewRetention", {"retention": 90})
        write(
            "maintenance.setRetention", retention="forever", expectedRevision=app.revisions()["preferences"]
        )
        task("maintenance.backup")
        task("maintenance.vacuum")
        recorder.call("maintenance.status")
        # Capture the genuine post-quiet output event alongside the other bridge events.
        clock.advance(600_000)
        app.remote.invoke(lambda service: service.store.flush_pending(clock.now_utc()))
        assert app.revisions()["output"] > 0
        app.events.set_visible(False)
        recorder.call("app.sync")
        legacy_copy = tmp_path / "synthetic-legacy.sqlite3"
        shutil.copyfile(legacy, legacy_copy)
        with Application(
            tmp_path / "migration-data",
            clock=clock,
            start_scheduler=False,
            legacy_path=legacy_copy,
        ) as migration_app:
            migration_recorder = Recorder(migration_app, tmp_path)
            detected = migration_recorder.call("migration.detect")
            assert detected["canMigrate"] is True
            migration_done = Event()
            unsubscribe = migration_app.bus.subscribe(
                lambda event: migration_done.set() if isinstance(event, TaskDone) else None
            )
            try:
                migration = migration_recorder.call(
                    "migration.run", {"dryRun": True, "operationId": str(uuid4())}
                )
                assert migration_done.wait(10)
            finally:
                unsubscribe()
            migration_recorder.call("tasks.get", {"taskId": migration["taskId"]})
            migration_recorder.call(
                "migration.activate",
                {"migrationId": migration["migrationId"], "operationId": str(uuid4())},
            )
            recorder.records.extend(migration_recorder.records)
        recorder.save(FIXTURE)
