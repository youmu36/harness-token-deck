"""Real-process management writes, clean EOF shutdown and persisted IPC replay."""

import json
import os
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from htu.ipc.dto.app import HelloResponse
from htu.ipc.dto.base import StrictModel, Success
from htu.ipc.dto.maintenance import MaintenanceStatus, RetentionResponse
from htu.ipc.dto.pricing import CatalogResponse, ChangeResponse
from htu.ipc.dto.tasks import TaskResponse, TaskStartedResponse
from htu.ipc.dto.wallet import WalletChangeResponse, WalletListResponse


@contextmanager
def bridge() -> Generator[subprocess.Popen[str], None, None]:
    root = Path(__file__).resolve().parents[3]
    process = subprocess.Popen(
        [sys.executable, "-m", "htu", "--headless-bridge"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "backend/src")},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process
        assert process.stdin is not None
        process.stdin.close()
        assert process.wait(10) == 0
        assert process.stderr and process.stderr.read() == ""
        assert process.stdout and process.stdout.read() == ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def call[T: StrictModel](
    process: subprocess.Popen[str], command: str, payload: dict[str, object], response: type[T]
) -> T:
    assert process.stdin and process.stdout
    process.stdin.write(json.dumps({"command": command, "payload": payload}) + "\n")
    process.stdin.flush()
    reply = json.loads(process.stdout.readline())
    assert reply["ok"], reply
    return Success[response].model_validate(reply).data


def test_prices_switch_and_wallet_receipts_cross_headless_restart() -> None:
    with bridge() as process:
        created = call(
            process,
            "pricing.createGroup",
            {"name": "Default", "expectedRevision": 0, "operationId": str(uuid4())},
            ChangeResponse,
        )
        saved = call(
            process,
            "pricing.saveVersionPrices",
            {
                "versionId": created.versionId,
                "expectedRevision": created.revision,
                "operationId": str(uuid4()),
                "models": [{"modelId": "Model", "input": "1.5"}],
            },
            ChangeResponse,
        )
        switch_payload: dict[str, object] = {
            "harness": "codex",
            "groupId": created.groupId,
            "versionId": created.versionId,
            "expectedRevision": saved.revision,
            "operationId": str(uuid4()),
        }
        switched = call(process, "pricing.switchCurrent", switch_payload, ChangeResponse)
        vendor = call(
            process,
            "wallet.createVendor",
            {"name": "Vendor", "initialBalance": "-0.5", "operationId": str(uuid4())},
            WalletChangeResponse,
        )
        call(
            process,
            "wallet.linkGroups",
            {
                "vendorId": vendor.vendorId,
                "groupIds": [created.groupId],
                "expectedRevision": vendor.revision,
                "operationId": str(uuid4()),
            },
            WalletChangeResponse,
        )
        topup_payload: dict[str, object] = {
            "vendorId": vendor.vendorId,
            "amount": "10",
            "operationId": str(uuid4()),
        }
        topup = call(process, "wallet.topUp", topup_payload, WalletChangeResponse)
        calibration_payload: dict[str, object] = {
            "vendorId": vendor.vendorId,
            "target": "8.75",
            "expectedWalletRevision": topup.revision,
            "operationId": str(uuid4()),
        }
        calibration = call(process, "wallet.calibrate", calibration_payload, WalletChangeResponse)
    with bridge() as process:
        prices = call(process, "pricing.catalog", {}, CatalogResponse)
        wallets = call(process, "wallet.list", {}, WalletListResponse)
        assert prices.current["codex"].versionId == created.versionId
        assert prices.groups[0].versions[0].models[0].input == "1.5"
        assert wallets.vendors[0].B == "8750000000000" and wallets.vendors[0].R == "10000000000000"
        assert wallets.vendors[0].linkedGroups == [created.groupId]
        assert call(process, "pricing.switchCurrent", switch_payload, ChangeResponse) == switched
        assert call(process, "wallet.topUp", topup_payload, WalletChangeResponse) == topup
        assert call(process, "wallet.calibrate", calibration_payload, WalletChangeResponse) == calibration


def test_maintenance_headless_commands_backup_and_receipt_restart() -> None:
    payload: dict[str, object] = {"retention": "forever", "expectedRevision": 0, "operationId": str(uuid4())}
    with bridge() as process:
        hello = call(process, "app.hello", {"clientProtocol": 1}, HelloResponse)
        assert {"migration.activate", "maintenance.backup", "maintenance.setRetention"} <= set(
            hello.capabilities
        )
        assert len(hello.capabilities) == len(set(hello.capabilities))
        saved = call(process, "maintenance.setRetention", payload, RetentionResponse)
        assert saved.complete
        started = call(process, "maintenance.backup", {"operationId": str(uuid4())}, TaskStartedResponse)
        deadline = time.monotonic() + 5
        while True:
            task = call(process, "tasks.get", {"taskId": started.taskId}, TaskResponse)
            if task.status not in ("pending", "running"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert task.status == "completed" and task.result and isinstance(task.result["path"], str)
        assert Path(task.result["path"]).is_file()
    with bridge() as process:
        assert call(process, "maintenance.status", {}, MaintenanceStatus).retention == "forever"
        assert call(process, "maintenance.setRetention", payload, RetentionResponse) == saved
