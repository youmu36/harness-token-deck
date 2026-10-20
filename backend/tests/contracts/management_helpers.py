"""Synthetic management setup and IPC assertions shared by P4.4/P4.5 tests."""

from htu.bootstrap import Application
from htu.ipc.dto.pricing import CatalogResponse, ChangeResponse
from htu.ipc.dto.wallet import Vendor, WalletChangeResponse

from .command_helpers import invoke


def price(app: Application, command: str, **parameters: object) -> ChangeResponse:
    from uuid import uuid4

    return invoke(
        app,
        command,
        {
            "expectedRevision": app.revisions()["models" if command.startswith("models.") else "pricing"],
            "operationId": str(uuid4()),
            **parameters,
        },
        ChangeResponse,
    )


def group(app: Application, name: str = "Default") -> tuple[str, str]:
    result = price(app, "pricing.createGroup", name=name)
    assert result.groupId is not None and result.versionId is not None
    return result.groupId, result.versionId


def catalog(app: Application) -> CatalogResponse:
    return invoke(app, "pricing.catalog", {}, CatalogResponse)


def wallet(app: Application, command: str, **parameters: object) -> WalletChangeResponse:
    from uuid import uuid4

    return invoke(app, command, {"operationId": str(uuid4()), **parameters}, WalletChangeResponse)


def vendor(app: Application, name: str = "Vendor", initial: str | None = None) -> Vendor:
    result = wallet(app, "wallet.createVendor", name=name, initialBalance=initial)
    assert result.vendor is not None
    return result.vendor
