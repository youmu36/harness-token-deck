"""Historical spending is independently editable and never changes financial records."""

from pathlib import Path
from uuid import uuid4

from htd.bootstrap import Application
from htd.ipc.dto.wallet import EntriesResponse, WalletChangeResponse, WalletListResponse

from .command_helpers import failure, invoke


def test_create_edit_replay_validation_and_restart_preserve_balance_and_entries(tmp_path: Path) -> None:
    with Application(tmp_path, start_scheduler=False) as app:
        created = invoke(
            app,
            "wallet.createVendor",
            {
                "operationId": str(uuid4()),
                "name": "supplier",
                "initialBalance": "20",
                "pastTopUp": "300",
                "pastConsumption": "280.123456",
            },
            WalletChangeResponse,
        )
        assert created.vendor
        identifier = created.vendorId
        assert created.vendor.pastConsumptionPico == "280123456000000"
        before = invoke(app, "wallet.entries", {"vendorId": identifier}, EntriesResponse)
        request = {
            "operationId": str(uuid4()),
            "vendorId": identifier,
            "name": "supplier",
            "pastConsumption": "88.000001",
        }
        changed = invoke(app, "wallet.renameVendor", request, WalletChangeResponse)
        assert invoke(app, "wallet.renameVendor", request, WalletChangeResponse) == changed
        assert changed.vendor and changed.vendor.pastConsumptionPico == "88000001000000"
        assert (changed.vendor.R, changed.vendor.C, changed.vendor.A, changed.vendor.B) == (
            created.vendor.R,
            created.vendor.C,
            created.vendor.A,
            created.vendor.B,
        )
        after = invoke(app, "wallet.entries", {"vendorId": identifier}, EntriesResponse)
        assert after.entries == before.entries
        assert (
            failure(
                app, "wallet.renameVendor", {**request, "operationId": str(uuid4()), "pastConsumption": "-1"}
            ).code
            == "INVALID_REQUEST"
        )
    with Application(tmp_path, start_scheduler=False) as app:
        state = invoke(app, "wallet.list", {}, WalletListResponse)
        assert state.vendors[0].pastConsumptionPico == "88000001000000"
