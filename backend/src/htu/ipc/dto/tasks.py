"""Task state and event contracts; source-scan results remain JSON-safe."""

from typing import Annotated, Literal

from pydantic import JsonValue, StringConstraints

from htu.ipc.dto.base import Error, SafeInt, StrictModel, WriteRequest

type Identifier = Annotated[str, StringConstraints(min_length=1, max_length=160)]


class TaskRequest(StrictModel):
    taskId: Identifier


class CancelTaskRequest(WriteRequest):
    taskId: Identifier


class TaskStartedResponse(StrictModel):
    taskId: Identifier


class TaskResponse(StrictModel):
    taskId: Identifier
    status: Literal["pending", "running", "completed", "cancelled", "failed"]
    cancelRequested: bool
    completed: SafeInt
    total: SafeInt
    result: dict[str, JsonValue] | None
    error: Error | None


class TaskProgressEvent(StrictModel):
    taskId: Identifier
    completed: SafeInt
    total: SafeInt


class TaskDoneEvent(StrictModel):
    taskId: Identifier
    ok: bool
    result: dict[str, JsonValue] | None
    error: Error | None
