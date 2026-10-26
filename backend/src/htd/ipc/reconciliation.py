"""Independent reconciliation command and typed notices."""

from typing import Annotated

from pydantic import StringConstraints

from htd.application.calibration import Calibrations
from htd.application.reconciliation import Reconciliation
from htd.ipc.dto.base import EmptyRequest, InstantStr, PicoStr, StrictModel
from htd.ipc.dto.events import HarnessName
from htd.ipc.dto.reconciliation import (
    BatchQuery,
    BatchResponse,
    CalibrationPreview,
    CalibrationRequest,
    CalibrationResponse,
    CalibrationTargetsRequest,
    CalibrationTargetsResponse,
    CommitCalibrationRequest,
    ReconciliationPreview,
    ReconciliationQuery,
)
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


def register_reconciliation(router: Router, service: Reconciliation, calibrations: Calibrations) -> None:
    @router.register(
        "reconciliation.previewCalibration",
        request=CalibrationRequest,
        response=CalibrationPreview,
        kind="read",
    )
    def preview_calibration(request: CalibrationRequest) -> CalibrationPreview:
        return CalibrationPreview.model_validate(calibrations.preview(request.model_dump()))

    @router.register(
        "reconciliation.commitCalibration",
        request=CommitCalibrationRequest,
        response=CalibrationResponse,
        kind="write",
    )
    def commit_calibration(request: CommitCalibrationRequest) -> CalibrationResponse:
        return CalibrationResponse.model_validate(
            calibrations.commit(request.model_dump(exclude={"operationId"}), request.operationId)
        )

    @router.register(
        "reconciliation.targets",
        request=CalibrationTargetsRequest,
        response=CalibrationTargetsResponse,
        kind="read",
    )
    def targets(request: CalibrationTargetsRequest) -> CalibrationTargetsResponse:
        return CalibrationTargetsResponse.model_validate(calibrations.targets(request.model_dump()))

    @router.register(
        "reconciliation.preview", request=ReconciliationQuery, response=ReconciliationPreview, kind="read"
    )
    def preview(request: ReconciliationQuery) -> ReconciliationPreview:
        return ReconciliationPreview.model_validate(service.preview(request.model_dump()))

    @router.register("reconciliation.batches", request=BatchQuery, response=BatchResponse, kind="read")
    def batches(request: BatchQuery) -> BatchResponse:
        return BatchResponse.model_validate(service.batches(request.model_dump()))

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
