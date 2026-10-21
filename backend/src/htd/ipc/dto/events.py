"""Typed event payloads and buffered records for app.sync."""

from typing import Annotated, Literal

from pydantic import Field

from htd.application.ports import ScanTrigger, SourceStatus
from htd.ipc.dto.base import InstantStr, SafeInt, StrictModel
from htd.ipc.dto.tasks import TaskDoneEvent, TaskProgressEvent

type HarnessName = Literal["claude", "codex", "zcode", "dsh"]


class UsageChangedEvent(StrictModel):
    harnesses: list[HarnessName]
    revision: SafeInt


class WalletChangedEvent(StrictModel):
    vendorIds: list[str]
    revision: SafeInt


class RevisionChangedEvent(StrictModel):
    revision: SafeInt


class CollectionTickEvent(StrictModel):
    status: dict[HarnessName, SourceStatus]
    nextPeriodicAt: InstantStr | None
    lastCompletedAt: InstantStr | None
    trigger: ScanTrigger


class VisibilityEvent(StrictModel):
    visible: bool


class UsageRecord(StrictModel):
    name: Literal["usage.changed"] = "usage.changed"
    payload: UsageChangedEvent


class WalletRecord(StrictModel):
    name: Literal["wallet.changed"] = "wallet.changed"
    payload: WalletChangedEvent


class RevisionRecord(StrictModel):
    name: Literal[
        "pricing.changed",
        "preferences.changed",
        "sources.changed",
        "models.changed",
        "quality.changed",
        "output.changed",
    ]
    payload: RevisionChangedEvent


class CollectionRecord(StrictModel):
    name: Literal["collection.tick"] = "collection.tick"
    payload: CollectionTickEvent


class VisibilityRecord(StrictModel):
    name: Literal["app.visibility"] = "app.visibility"
    payload: VisibilityEvent


class TaskProgressRecord(StrictModel):
    name: Literal["task.progress"] = "task.progress"
    payload: TaskProgressEvent


class TaskDoneRecord(StrictModel):
    name: Literal["task.done"] = "task.done"
    payload: TaskDoneEvent


type EventRecord = Annotated[
    UsageRecord
    | WalletRecord
    | RevisionRecord
    | CollectionRecord
    | VisibilityRecord
    | TaskProgressRecord
    | TaskDoneRecord,
    Field(discriminator="name"),
]
