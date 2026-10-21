"""Independent reconciliation command and typed notices."""

from typing import Annotated

from pydantic import StringConstraints

from htd.application.reconciliation import Reconciliation
from htd.ipc.dto.base import EmptyRequest, InstantStr, PicoStr, StrictModel
from htd.ipc.dto.events import HarnessName
from htd.ipc.router import Router


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


class DismissNoticeRequest(StrictModel):
    vendorId: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    observedAt: InstantStr


def register_reconciliation(router: Router, service: Reconciliation) -> None:
    @router.register(
        "reconciliation.status", request=EmptyRequest, response=ReconciliationResponse, kind="read"
    )
    def status(request: EmptyRequest) -> ReconciliationResponse:
        return ReconciliationResponse.model_validate(service.status())

    @router.register(
        "reconciliation.dismiss", request=DismissNoticeRequest, response=ReconciliationResponse, kind="write"
    )
    def dismiss(request: DismissNoticeRequest) -> ReconciliationResponse:
        return ReconciliationResponse.model_validate(service.dismiss(request.vendorId, request.observedAt))
