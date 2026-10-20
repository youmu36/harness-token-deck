"""Protocol handshake and visibility recovery contracts."""

from typing import Annotated, Literal

from pydantic import Field

from htu.ipc.dto.base import Revisions, SafeInt, StrictModel
from htu.ipc.dto.events import EventRecord


class HelloRequest(StrictModel):
    clientProtocol: Annotated[SafeInt, Field(ge=1)]


class HelloResponse(StrictModel):
    protocol: Literal[1] = 1
    appVersion: str
    capabilities: list[str]
    mode: Literal["desktop"] = "desktop"
    dataDir: str
    revisions: Revisions


class SyncRequest(StrictModel):
    pass


class SyncResponse(StrictModel):
    revisions: Revisions
    pendingEvents: list[EventRecord]


class PickPathRequest(StrictModel):
    kind: Literal["file", "dir"]
    initial: Annotated[str, Field(max_length=32767)] | None = None


class PickPathResponse(StrictModel):
    path: str | None


class OpenPathRequest(StrictModel):
    kind: Literal["dataDir", "logDir"]


class OpenPathResponse(StrictModel):
    opened: bool
