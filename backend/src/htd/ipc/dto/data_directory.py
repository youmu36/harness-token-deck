"""Directory drafts never change the active data location."""

from typing import Annotated, Literal

from pydantic import StringConstraints

from htd.ipc.dto.base import SafeInt, StrictModel, WriteRequest

type DirectoryPath = Annotated[str, StringConstraints(min_length=1, max_length=32767)]


class DirectoryStatus(StrictModel):
    root: str
    source: Literal["explicit", "environment", "locator", "default"]
    phase: str
    previousRoot: str | None
    canMove: bool
    canCleanup: bool
    pendingTarget: str | None = None


class DirectoryPreviewRequest(StrictModel):
    target: DirectoryPath


class DirectoryPreview(StrictModel):
    source: str
    target: str
    datasetId: str
    requiredBytes: SafeInt
    availableBytes: SafeInt
    unchanged: bool
    fingerprint: str
    resumeOperationId: str | None = None


class DirectoryMoveRequest(WriteRequest):
    target: DirectoryPath
    fingerprint: str


class DirectoryMoveResponse(StrictModel):
    root: str
    restartRequired: bool


class DirectoryCleanupResponse(StrictModel):
    deletedFiles: SafeInt
