"""Register manual maintenance, history and repricing commands."""

from htu.application.maintenance import Maintenance
from htu.application.maintenance.recalc import Recalculate
from htu.ipc.dto import maintenance as m
from htu.ipc.dto.base import EmptyRequest, WriteRequest
from htu.ipc.dto.tasks import TaskStartedResponse
from htu.ipc.router import Router


def register_maintenance(router: Router, maintenance: Maintenance, recalc: Recalculate) -> None:
    @router.register("maintenance.status", request=EmptyRequest, response=m.MaintenanceStatus, kind="read")
    def status(request: EmptyRequest) -> m.MaintenanceStatus:
        return m.MaintenanceStatus.model_validate(maintenance.status())

    @router.register(
        "maintenance.previewRetention", request=m.RetentionRequest, response=m.RetentionPreview, kind="read"
    )
    def preview_retention(request: m.RetentionRequest) -> m.RetentionPreview:
        return m.RetentionPreview.model_validate(maintenance.preview(request.retention))

    @router.register(
        "maintenance.setRetention",
        request=m.SetRetentionRequest,
        response=m.RetentionResponse,
        kind="write",
        timeout=300,
    )
    def set_retention(request: m.SetRetentionRequest) -> m.RetentionResponse:
        return m.RetentionResponse.model_validate(
            maintenance.set_retention(request.retention, request.expectedRevision, request.operationId)
        )

    @router.register("maintenance.vacuum", request=WriteRequest, response=TaskStartedResponse, kind="write")
    def vacuum(request: WriteRequest) -> TaskStartedResponse:
        return TaskStartedResponse.model_validate(
            maintenance.start_file("maintenance.vacuum", request.operationId)
        )

    @router.register("maintenance.backup", request=WriteRequest, response=TaskStartedResponse, kind="write")
    def backup(request: WriteRequest) -> TaskStartedResponse:
        return TaskStartedResponse.model_validate(
            maintenance.start_file("maintenance.backup", request.operationId)
        )

    @router.register(
        "maintenance.previewRecalc", request=m.RecalcRequest, response=m.RecalcPreview, kind="read"
    )
    def preview_recalc(request: m.RecalcRequest) -> m.RecalcPreview:
        return m.RecalcPreview.model_validate(recalc.preview(request.model_dump()))

    @router.register(
        "maintenance.commitRecalc",
        request=m.CommitRecalcRequest,
        response=m.RecalcResponse,
        kind="write",
        timeout=300,
    )
    def commit_recalc(request: m.CommitRecalcRequest) -> m.RecalcResponse:
        return m.RecalcResponse.model_validate(
            recalc.commit(request.model_dump(exclude={"operationId"}), request.operationId)
        )
