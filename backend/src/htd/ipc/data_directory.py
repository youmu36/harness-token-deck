"""Exclusive cutover shares the router barrier used for backup activation."""

from collections.abc import Callable

from htd.application.data_directory import DataDirectory
from htd.ipc.dto import data_directory as d
from htd.ipc.dto.base import StrictModel, WriteRequest
from htd.ipc.router import Router


def register_data_directory(router: Router, service: DataDirectory, restart: Callable[[], None]) -> None:
    @router.register(
        "dataDirectory.status",
        request=StrictModel,
        response=d.DirectoryStatus,
        kind="read",
        allow_during_exclusive=True,
    )
    def status(request: StrictModel) -> d.DirectoryStatus:
        return d.DirectoryStatus.model_validate(service.status())

    @router.register(
        "dataDirectory.preview", request=d.DirectoryPreviewRequest, response=d.DirectoryPreview, kind="read"
    )
    def preview(request: d.DirectoryPreviewRequest) -> d.DirectoryPreview:
        return d.DirectoryPreview.model_validate(service.preview(request.target))

    @router.register(
        "dataDirectory.move",
        request=d.DirectoryMoveRequest,
        response=d.DirectoryMoveResponse,
        kind="write",
        timeout=600,
        exclusive=True,
        after_success=restart,
    )
    def move(request: d.DirectoryMoveRequest) -> d.DirectoryMoveResponse:
        return d.DirectoryMoveResponse.model_validate(
            service.move(request.target, request.fingerprint, request.operationId)
        )

    @router.register(
        "dataDirectory.cleanup",
        request=WriteRequest,
        response=d.DirectoryCleanupResponse,
        kind="write",
        exclusive=True,
    )
    def cleanup(request: WriteRequest) -> d.DirectoryCleanupResponse:
        return d.DirectoryCleanupResponse.model_validate(service.cleanup(request.operationId))
