"""Independent reconciliation command and typed notices."""

from htu.application.reconciliation import Reconciliation
from htu.ipc.dto.base import EmptyRequest, InstantStr, PicoStr, StrictModel
from htu.ipc.dto.events import HarnessName
from htu.ipc.router import Router


class ReconciliationNotice(StrictModel):
    vendorId: str
    vendorName: str
    harness: HarnessName
    selectedGroupId: str | None
    selectedGroupName: str | None
    observedAt: InstantStr
    remoteDecreasePico: PicoStr


class ReconciliationResponse(StrictModel):
    notices: list[ReconciliationNotice]


def register_reconciliation(router: Router, service: Reconciliation) -> None:
    @router.register(
        "reconciliation.status", request=EmptyRequest, response=ReconciliationResponse, kind="read"
    )
    def status(request: EmptyRequest) -> ReconciliationResponse:
        return ReconciliationResponse.model_validate(service.status())
