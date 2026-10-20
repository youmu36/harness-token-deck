"""Expose migration use cases through the shared router."""

from collections.abc import Callable

from htu.application.migration import Migration
from htu.ipc.dto import migration as m
from htu.ipc.router import Router


def register_migration(router: Router, migration: Migration, *, changed: Callable[[], None]) -> None:
    @router.register(
        "migration.detect", request=m.MigrationDetectRequest, response=m.MigrationDetectResponse, kind="read"
    )
    def detect(request: m.MigrationDetectRequest) -> m.MigrationDetectResponse:
        return m.MigrationDetectResponse.model_validate(migration.detect(request.sourcePath))

    @router.register(
        "migration.run", request=m.MigrationRunRequest, response=m.MigrationRunResponse, kind="write"
    )
    def run(request: m.MigrationRunRequest) -> m.MigrationRunResponse:
        return m.MigrationRunResponse.model_validate(migration.run(request.operationId, request.sourcePath))

    @router.register(
        "migration.activate",
        request=m.MigrationActivateRequest,
        response=m.MigrationActivateResponse,
        kind="write",
        timeout=300,
        exclusive=True,
        after_success=changed,
    )
    def activate(request: m.MigrationActivateRequest) -> m.MigrationActivateResponse:
        return m.MigrationActivateResponse.model_validate(
            migration.activate(request.migrationId, request.operationId)
        )
