"""Safe remote IPC, exact calibration, receipts and concurrent source collection."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from htu.adapters.collectors.discovery import Discovery
from htu.application.remote.ports import MemoryCredentialStore
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.remote.model import BalanceObservation, RemoteAccount, RemoteFailure
from htu.ipc.dto.remote import RemoteSnapshot, RemoteTestResponse
from htu.ipc.dto.wallet import WalletChangeResponse, WalletListResponse

from backend.tests.integration.scan_helpers import headers, request, sessions
from backend.tests.unit.remote.test_refresh import Provider

from .command_helpers import failure, invoke
from .management_helpers import vendor


def configured(app: Application, vendor_id: str, **changes: object) -> dict[str, object]:
    return {
        "vendorId": vendor_id,
        "baseUrl": "https://example.test",
        "siteType": "newapi",
        "authMode": "access_token",
        "userId": "42",
        "credential": "synthetic-secret-abcd",
        "enabled": False,
        **changes,
    }


@pytest.mark.parametrize("auth_mode", ["api_key", "access_token"])
def test_pasted_bearer_and_outer_whitespace_save_and_test(auth_mode: str, tmp_path: Path) -> None:
    clock = FakeClock()
    credentials = MemoryCredentialStore()
    provider = Provider(clock)
    with Application(
        tmp_path,
        clock=clock,
        start_scheduler=False,
        credentials=credentials,
        provider_factory=lambda: provider,
    ) as app:
        supplier = vendor(app, "Pasted token")
        parameters = configured(
            app,
            supplier.id,
            credential=" \r\nBearer sk-synthetic-token-abcd \r\n",
            authMode=auth_mode,
            siteType="newapi" if auth_mode == "access_token" else "sub2api",
            userId="1" if auth_mode == "access_token" else None,
        )
        assert invoke(app, "remote.test", parameters, RemoteTestResponse).error is None
        saved = write(app, "remote.save", **parameters)
        assert saved.account and saved.account.maskedCredential == "••••abcd"
        assert (
            credentials.get(app.remote.service.store.accounts()[0].credential_ref)
            == "sk-synthetic-token-abcd"
        )
        raw = app.writer.execute(lambda w: "\n".join(w.connection.iterdump()))
        assert "sk-synthetic-token-abcd" not in raw


def write(app: Application, command: str, **parameters: object) -> RemoteSnapshot:
    return invoke(app, command, {"operationId": str(uuid4()), **parameters}, RemoteSnapshot)


def test_five_commands_masking_and_automatic_wallet_calibration(tmp_path: Path) -> None:
    clock = FakeClock()
    credentials = MemoryCredentialStore()
    provider = Provider(clock)
    with Application(
        tmp_path,
        clock=clock,
        start_scheduler=False,
        credentials=credentials,
        provider_factory=lambda: provider,
    ) as app:
        supplier = vendor(app, "Remote", "10")
        settings = configured(app, supplier.id)
        before_entries = app.wallet.entries(supplier.id, None, 100)["entries"]
        assert invoke(app, "remote.get", {"vendorId": supplier.id}, RemoteSnapshot).status == "unconfigured"
        tested = invoke(app, "remote.test", settings, RemoteTestResponse)
        assert (
            tested.observation
            and tested.observation.kind == "account_remaining"
            and tested.observation.canCalibrate
        )
        assert app.revisions()["wallet"] == supplier.walletRevision
        saved = write(app, "remote.save", **settings)
        assert saved.account and saved.account.maskedCredential == "••••abcd"
        assert saved.observation is None
        refreshed = write(app, "remote.refresh", vendorId=supplier.id)
        assert refreshed.observation and refreshed.observation.amountPico == "1975310000001"
        after_entries = app.wallet.entries(supplier.id, None, 100)["entries"]
        assert isinstance(after_entries, list) and isinstance(before_entries, list)
        assert (
            after_entries == before_entries
        )  # A first remote endpoint is a baseline, not a local calibration.
        data = json.dumps([saved.model_dump(), refreshed.model_dump()], ensure_ascii=False)
        assert "synthetic-secret-abcd" not in data and "credentialRef" not in data
        raw = app.writer.execute(lambda work: "\n".join(work.connection.iterdump()))
        assert "synthetic-secret-abcd" not in raw
        assert invoke(app, "wallet.list", {}, WalletListResponse).vendors[0].account.B == "1975310000001"
        deleted = write(app, "remote.delete", vendorId=supplier.id)
        assert deleted.account is None and not app.remote.service.store.accounts()
        assert write(app, "remote.delete", vendorId=supplier.id).status == "unconfigured"


def test_save_receipts_and_draft_test_do_not_replace_saved_secret(tmp_path: Path) -> None:
    clock = FakeClock()
    store = MemoryCredentialStore()
    with Application(
        tmp_path,
        clock=clock,
        start_scheduler=False,
        credentials=store,
        provider_factory=lambda: Provider(clock),
    ) as app:
        supplier = vendor(app, "Remote")
        settings = configured(app, supplier.id)
        operation = str(uuid4())
        first = invoke(app, "remote.save", {**settings, "operationId": operation}, RemoteSnapshot)
        again = invoke(app, "remote.save", {**settings, "operationId": operation}, RemoteSnapshot)
        assert first == again and first.account
        invoke(app, "remote.test", {**settings, "credential": "draft-secret-xyz9"}, RemoteTestResponse)
        assert store.get(app.remote.service.store.accounts()[0].credential_ref) == "synthetic-secret-abcd"
        assert (
            failure(
                app, "remote.save", {**settings, "credential": "different-secret", "operationId": operation}
            ).code
            == "OPERATION_ID_REUSED"
        )
        assert (
            failure(
                app, "wallet.topUp", {"vendorId": supplier.id, "amount": "1", "operationId": operation}
            ).code
            == "OPERATION_ID_REUSED"
        )
        assert write(app, "remote.save", **{**settings, "credential": None}).account == first.account


@pytest.mark.parametrize("mode", ["exact", "stale", "failed", "uncertain", "edited", "conflict", "site_type"])
def test_remote_calibration_validates_current_observation_and_preserves_full_pico(
    tmp_path: Path, mode: str
) -> None:
    clock = FakeClock()
    provider = Provider(clock)
    with Application(
        tmp_path,
        clock=clock,
        start_scheduler=False,
        credentials=MemoryCredentialStore(),
        provider_factory=lambda: provider,
    ) as app:
        supplier = vendor(app, "Remote", "10")
        write(app, "remote.save", **configured(app, supplier.id))
        provider.uncertain = mode == "uncertain"
        result = write(app, "remote.refresh", vendorId=supplier.id)
        assert result.observation
        payload: dict[str, object] = {
            "vendorId": supplier.id,
            "target": "1.975310",
            "remoteFetchedAt": result.observation.fetchedAt,
            "expectedWalletRevision": app.revisions()["wallet"],
            "operationId": str(uuid4()),
        }
        if mode == "stale":
            clock.advance(600_001)
        if mode == "failed":
            provider.error = RemoteFailure("offline", "离线")
            clock.advance(1)
            write(app, "remote.refresh", vendorId=supplier.id)
            payload["expectedWalletRevision"] = app.revisions()["wallet"]
        if mode == "edited":
            payload["target"] = "2"
        if mode == "site_type":
            saved = write(
                app,
                "remote.save",
                **configured(
                    app, supplier.id, siteType="sub2api", authMode="api_key", userId=None, credential=None
                ),
            )
            assert saved.account and saved.account.siteType == "sub2api" and saved.observation is None
            payload["expectedWalletRevision"] = app.revisions()["wallet"]
        if mode == "conflict":
            invoke(
                app,
                "wallet.topUp",
                {"vendorId": supplier.id, "amount": "1", "operationId": str(uuid4())},
                WalletChangeResponse,
            )
        if mode == "exact":
            calibrated = invoke(app, "wallet.calibrate", payload, WalletChangeResponse)
            assert calibrated.vendor and calibrated.vendor.account.B == "1975310000001"
            assert invoke(app, "wallet.calibrate", payload, WalletChangeResponse) == calibrated
        else:
            assert failure(app, "wallet.calibrate", payload).code == (
                "WALLET_REVISION_CONFLICT" if mode == "conflict" else "REMOTE_OBSERVATION_CHANGED"
            )


def test_network_wait_and_failure_do_not_block_scan_thread(tmp_path: Path) -> None:
    clock = FakeClock()
    started = Event()

    class WaitingProvider(Provider):
        async def fetch(self, account: RemoteAccount, credential: str) -> BalanceObservation:
            self.gate = asyncio.Event()
            started.set()
            await self.gate.wait()
            self.error = RemoteFailure("offline", "离线")
            return await super().fetch(account, credential)

    provider = WaitingProvider(clock)
    root = sessions(tmp_path / "home")
    (root / "synthetic.jsonl").write_bytes(headers())
    with Application(
        tmp_path / "data",
        clock=clock,
        discovery=Discovery(home=tmp_path / "home", environ={}),
        start_scheduler=False,
        credentials=MemoryCredentialStore(),
        provider_factory=lambda: provider,
    ) as app:
        supplier = vendor(app, "Remote")
        write(app, "remote.save", **configured(app, supplier.id))
        app.scan.collect()
        (root / "synthetic.jsonl").write_bytes(headers() + request(1, clock.now_utc()))
        with ThreadPoolExecutor() as threads:
            refresh = threads.submit(
                app.bridge.call, "remote.refresh", {"vendorId": supplier.id, "operationId": str(uuid4())}
            )
            assert started.wait(2)
            scan = threads.submit(app.scan.collect)
            assert scan.result(timeout=3) is not None
            assert not refresh.done()
            app.remote.invoke(lambda service: provider.gate.set() if provider.gate else None)
            response = refresh.result(timeout=3)
            assert response["ok"] and RemoteSnapshot.model_validate(response["data"]).status == "offline"
