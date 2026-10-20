"""JSON-safe primitives and discriminated IPC envelopes."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from htu.domain.common.errors import DomainError
from htu.domain.common.time import parse_instant

type Scope = Literal[
    "preferences",
    "pricing",
    "wallet",
    "sources",
    "usage",
    "account",
    "quality",
    "display",
    "models",
    "ownership",
    "auxiliary",
    "output",
]
type SafeInt = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
type Revisions = dict[Scope, SafeInt]
type PicoStr = Annotated[str, StringConstraints(strict=True, pattern=r"^(?:0|-?[1-9][0-9]*)$")]
type RateStr = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9]{1,9}(?:\.[0-9]{1,6})?$")]
type OperationId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]


def _valid_instant(value: str) -> str:
    try:
        parse_instant(value)
    except DomainError as error:
        raise ValueError("无效的 UTC 日期或时间") from error
    return value


type InstantStr = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
    ),
    AfterValidator(_valid_instant),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, json_schema_serialization_defaults_required=True)


class EmptyRequest(StrictModel):
    pass


class WriteRequest(StrictModel):
    operationId: OperationId


class Error(StrictModel):
    # Absent error metadata is omitted by the transport.
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]
    message: str
    retryable: bool
    field: str | None = None
    conflictRevision: SafeInt | None = None
    logId: str | None = None


class Success[T](StrictModel):
    # The router omits revisions; data DTOs still serialize all of their defaults.
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)
    ok: Literal[True] = True
    data: T
    revisions: Revisions | None = None


class Failure(StrictModel):
    ok: Literal[False] = False
    error: Error


type Envelope[T] = Annotated[Success[T] | Failure, Field(discriminator="ok")]
