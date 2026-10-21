"""Normalize in-memory Zcode rows and advance a verified read batch without I/O."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Never, cast

from htd.domain.common.errors import (
    SOURCE_BATCH_INVALID,
    SOURCE_CONTEXT_INVALID,
    SOURCE_ROW_INVALID,
    DomainError,
)
from htd.domain.common.hashing import identity_key
from htd.domain.common.time import Instant, format_instant, from_epoch_ms, parse_instant
from htd.domain.usage.model import Diagnostic, Harness, Tokens, UsageEvent, UsageStatus, optional_text

type StatusClass = Literal["pending", "terminal", "unsupported"]
_TERMINAL = ("completed", "error", "cancelled")


@dataclass(frozen=True, slots=True)
class ZcodeRow:
    rowid: int
    id: str
    session_id: object = None
    logical_request_id: object = None
    attempt_index: object = 0
    model_id: object = None
    status: object = None
    completed_at: object = None
    raw_usage_json: object = None
    input_tokens: object = None
    output_tokens: object = None
    reasoning_tokens: object = None
    cache_read_input_tokens: object = None
    cache_creation_input_tokens: object = None
    provider_id: object = None
    started_at: object = None
    first_token_at: object = None
    duration_ms: object = None
    time_to_first_token_ms: object = None

    def __post_init__(self) -> None:
        if type(self.rowid) is not int or self.rowid <= 0:
            raise DomainError(SOURCE_ROW_INVALID, "来源行号须为正整数", "rowid")
        if type(self.id) is not str or not self.id:
            raise DomainError(SOURCE_ROW_INVALID, "来源行标识不能为空", "id")


def classify_status(status: object, completed_at: object) -> StatusClass:
    if status == "running" or completed_at is None:
        return "pending"
    return "terminal" if type(status) is str and status in _TERMINAL else "unsupported"


def _reject_constant(value: str) -> Never:
    raise ValueError(f"Non-JSON number: {value}")


def generation_milliseconds(row: ZcodeRow) -> int | None:
    """Use per-model-request timing, excluding its first-token wait and tool work."""
    end, first, start = row.completed_at, row.first_token_at, row.started_at
    if type(end) is not int:
        return None
    if start is not None and (type(start) is not int or start < 0 or start > end):
        return None
    if first is not None:
        if type(first) is not int or not 0 < first < end or (type(start) is int and first < start):
            return None
        return end - first
    duration, wait = row.duration_ms, row.time_to_first_token_ms
    if type(duration) is not int or type(wait) is not int or not 0 <= wait < duration:
        return None
    if type(start) is int and duration > end - start:
        return None
    return duration - wait


def normalize_row(row: ZcodeRow) -> UsageEvent | Diagnostic:
    if type(row.status) is not str or row.status not in _TERMINAL:
        return Diagnostic("source_status_unsupported")
    session = optional_text(row.session_id)
    if session is None:
        return Diagnostic("missing_session_id")
    request = optional_text(row.logical_request_id)
    if request is None:
        return Diagnostic("missing_request_id")
    if type(row.attempt_index) is not int or row.attempt_index < 0:
        return Diagnostic("invalid_attempt_index")
    usage: dict[str, object] = {}
    if row.raw_usage_json is not None:
        if type(row.raw_usage_json) is not str:
            return Diagnostic("invalid_usage_json")
        try:
            decoded: object = json.loads(row.raw_usage_json, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return Diagnostic("invalid_usage_json")
        if type(decoded) is not dict:
            return Diagnostic("invalid_usage_json")
        usage = cast(dict[str, object], decoded)

    def count(key: str, native: object) -> int | None:
        value = usage.get(key)
        return cast(int | None, native if value is None else value)

    try:
        tokens = Tokens(
            input=count("inputTokens", row.input_tokens),
            cache_read=count("cacheReadTokens", row.cache_read_input_tokens),
            cache_write=count("cacheWriteTokens", row.cache_creation_input_tokens),
            output=count("outputTokens", row.output_tokens),
            reasoning=count("reasoningTokens", row.reasoning_tokens),
        )
        return UsageEvent(
            harness=Harness.ZCODE,
            dedupe_key=identity_key(
                [cast(str, row.session_id), cast(str, row.logical_request_id), row.attempt_index]
            ),
            session_id=session,
            request_id=request,
            model_id=optional_text(row.model_id),
            occurred_at=format_instant(from_epoch_ms(cast(int, row.completed_at))),
            tokens=tokens,
            status=cast(UsageStatus, row.status),
            provider_name=optional_text(row.provider_id),
            generation_milliseconds=generation_milliseconds(row),
        )
    except DomainError as error:
        return Diagnostic(error.code.lower())


@dataclass(frozen=True, slots=True)
class ZcodeCheckpoint:
    row_id: int = 0
    tail_id: str | None = None
    pending: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if type(self.row_id) is not int or self.row_id < 0:
            raise DomainError(SOURCE_CONTEXT_INVALID, "检查点行号无效", "rowId")
        if (self.row_id == 0 and self.tail_id is not None) or (
            self.row_id > 0 and (type(self.tail_id) is not str or not self.tail_id)
        ):
            raise DomainError(SOURCE_CONTEXT_INVALID, "检查点尾行标识无效", "tailId")
        if type(self.pending) not in (tuple, list) or any(
            type(rowid) is not int or not 0 < rowid <= self.row_id for rowid in self.pending
        ):
            raise DomainError(SOURCE_CONTEXT_INVALID, "检查点待重查行号无效", "pending")
        object.__setattr__(self, "pending", tuple(sorted(set(self.pending))))

    def to_dict(self) -> dict[str, object]:
        return {"rowId": self.row_id, "tailId": self.tail_id, "pending": list(self.pending)}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ZcodeCheckpoint":
        if type(data) is not dict or not {"rowId", "tailId", "pending"} <= data.keys():
            raise DomainError(SOURCE_CONTEXT_INVALID, "Zcode 检查点字段缺失", "checkpoint")
        return cls(
            cast(int, data["rowId"]), cast(str | None, data["tailId"]), cast(tuple[int, ...], data["pending"])
        )


@dataclass(frozen=True, slots=True)
class ZcodeBatch:
    checkpoint: ZcodeCheckpoint
    events: tuple[UsageEvent, ...]
    diagnostics: tuple[Diagnostic, ...]
    more: bool


def advance(
    checkpoint: ZcodeCheckpoint,
    pending_rows: Mapping[int, ZcodeRow | None],
    fresh_rows: Sequence[ZcodeRow],
    started_at: Instant,
) -> ZcodeBatch:
    """The reader verifies the tail before supplying all rechecks and one fresh page."""
    if (
        any(type(key) is not int for key in pending_rows)
        or set(pending_rows) != set(checkpoint.pending)
        or any(
            row is not None and (type(row) is not ZcodeRow or row.rowid != key)
            for key, row in pending_rows.items()
        )
    ):
        raise DomainError(SOURCE_BATCH_INVALID, "待重查结果必须完整且行号匹配", "pending_rows")
    if len(fresh_rows) > 500:
        raise DomainError(SOURCE_BATCH_INVALID, "新行批次不能超过 500 行", "fresh_rows")
    previous = checkpoint.row_id
    for row in fresh_rows:
        if type(row) is not ZcodeRow or row.rowid <= previous:
            raise DomainError(SOURCE_BATCH_INVALID, "新行必须按检查点后的行号严格递增", "fresh_rows")
        previous = row.rowid
    pending: list[int] = []
    events: list[UsageEvent] = []
    diagnostics: list[Diagnostic] = []

    def consume(row: ZcodeRow) -> None:
        classification = classify_status(row.status, row.completed_at)
        if classification == "pending":
            pending.append(row.rowid)
            return
        result = (
            Diagnostic("source_status_unsupported") if classification == "unsupported" else normalize_row(row)
        )
        if isinstance(result, Diagnostic):
            diagnostics.append(result)  # Terminal bad rows are quarantined by the reader.
        elif parse_instant(result.occurred_at) >= started_at:
            events.append(result)

    for rowid in checkpoint.pending:
        row = pending_rows[rowid]
        if row is None:
            diagnostics.append(Diagnostic("pending_request_removed"))
        else:
            consume(row)
    for row in fresh_rows:
        consume(row)
    last = fresh_rows[-1] if fresh_rows else None
    return ZcodeBatch(
        checkpoint=ZcodeCheckpoint(
            last.rowid if last else checkpoint.row_id, last.id if last else checkpoint.tail_id, tuple(pending)
        ),
        events=tuple(events),
        diagnostics=tuple(diagnostics),
        more=len(fresh_rows) == 500,
    )
