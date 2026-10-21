"""Strict contracts for legacy detection, dry-run tasks, and activation."""

from typing import Annotated, Literal

from pydantic import Field

from htd.ipc.dto.base import StrictModel, WriteRequest
from htd.ipc.dto.tasks import TaskStartedResponse


class MigrationDetectRequest(StrictModel):
    sourcePath: Annotated[str, Field(min_length=1, max_length=32767)] | None = None


class MigrationDetectResponse(StrictModel):
    detected: bool
    sourcePath: str
    legacyRunning: bool
    targetEmpty: bool
    canMigrate: bool
    reason: str | None


class MigrationRunRequest(WriteRequest):
    dryRun: Literal[True]
    sourcePath: Annotated[str, Field(min_length=1, max_length=32767)] | None = None


class MigrationRunResponse(TaskStartedResponse):
    migrationId: str
    reportPath: str


class MigrationActivateRequest(WriteRequest):
    migrationId: str


class MigrationActivateResponse(StrictModel):
    activated: bool
    sourcePath: str
    targetPath: str
    reportPath: str
