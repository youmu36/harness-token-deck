"""Parse Codex request usage with explicit checkpointable state and no I/O."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from htu.domain.common.errors import SOURCE_CONTEXT_INVALID, DomainError
from htu.domain.common.hashing import identity_key
from htu.domain.common.time import format_instant, parse_instant
from htu.domain.usage.model import (
    Diagnostic,
    Harness,
    Result,
    Skip,
    SourceKind,
    Tokens,
    UsageEvent,
    optional_text,
)


def _object(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if type(value) is dict else None


def classify_source(originator: object, source: object) -> SourceKind | None:
    if originator not in ("Codex Desktop", "codex_work_desktop"):
        return None
    if source == "vscode":
        return "desktop"
    source_object = _object(source)
    subagent = _object(source_object.get("subagent")) if source_object is not None else None
    if subagent is not None and "thread_spawn" in subagent:
        return "desktop_subagent"
    return None


@dataclass(slots=True)
class CodexContext:
    """Mutable parser state owned by one file reader, not a domain value object."""

    file_session_id: str | None = None
    raw_file_session_id: str | None = None
    saw_session_meta: bool = False
    originator_ok: bool = False
    kind: SourceKind | None = None
    model_id: str | None = None
    turn_id: str | None = None
    saw_request_usage: bool = False
    provider_name: str | None = None
    context_compacted: bool = False
    request_started_at: str | None = None
    request_output_at: str | None = None
    last_timed_response: str | None = None
    last_generation_ms: int | None = None

    @property
    def accepted(self) -> bool:
        return self.saw_session_meta and self.originator_ok and self.kind is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "fileSessionId": self.file_session_id,
            "rawFileSessionId": self.raw_file_session_id,
            "sawSessionMeta": self.saw_session_meta,
            "originatorOk": self.originator_ok,
            "kind": self.kind,
            "modelId": self.model_id,
            "turnId": self.turn_id,
            "sawRequestUsage": self.saw_request_usage,
            "providerName": self.provider_name,
            "contextCompacted": self.context_compacted,
            "requestStartedAt": self.request_started_at,
            "requestOutputAt": self.request_output_at,
            "lastTimedResponse": self.last_timed_response,
            "lastGenerationMs": self.last_generation_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "CodexContext":
        if _object(data) is None:
            raise DomainError(SOURCE_CONTEXT_INVALID, "Codex 检查点上下文须为对象", "context")
        data = {
            "providerName": None,
            "contextCompacted": False,
            "requestStartedAt": None,
            "requestOutputAt": None,
            "lastTimedResponse": None,
            "lastGenerationMs": None,
            **data,
        }
        expected = cls().to_dict()
        if _object(data) is None or not expected.keys() <= data.keys():
            raise DomainError(SOURCE_CONTEXT_INVALID, "Codex 检查点上下文字段缺失", "context")
        for name in ("sawSessionMeta", "originatorOk", "sawRequestUsage", "contextCompacted"):
            if type(data[name]) is not bool:
                raise DomainError(SOURCE_CONTEXT_INVALID, "上下文标志必须为布尔值", name)
        for name in ("fileSessionId", "modelId", "turnId"):
            value = data[name]
            if value is not None and (type(value) is not str or optional_text(value) != value):
                raise DomainError(SOURCE_CONTEXT_INVALID, "上下文文本无效", name)
        raw_session = data["rawFileSessionId"]
        if (raw_session is not None and optional_text(raw_session) is None) or (
            optional_text(raw_session) != data["fileSessionId"]
        ):
            raise DomainError(SOURCE_CONTEXT_INVALID, "上下文会话原文与标识不一致", "rawFileSessionId")
        kind = data["kind"]
        request_start = cast(str | None, data["requestStartedAt"])
        if request_start is not None:
            request_start = format_instant(parse_instant(request_start))
        request_output = cast(str | None, data["requestOutputAt"])
        if request_output is not None:
            request_output = format_instant(parse_instant(request_output))
        last_response, last_ms = data["lastTimedResponse"], data["lastGenerationMs"]
        if (last_response is not None and optional_text(last_response) is None) or (
            last_ms is not None and (type(last_ms) is not int or last_ms <= 0)
        ):
            raise DomainError(SOURCE_CONTEXT_INVALID, "请求计时上下文无效", "context")
        if kind is not None and (type(kind) is not str or kind not in ("desktop", "desktop_subagent")):
            raise DomainError(SOURCE_CONTEXT_INVALID, "上下文来源分类无效", "kind")
        context = cls(
            file_session_id=cast(str | None, data["fileSessionId"]),
            raw_file_session_id=cast(str | None, raw_session),
            saw_session_meta=cast(bool, data["sawSessionMeta"]),
            originator_ok=cast(bool, data["originatorOk"]),
            kind=kind,
            model_id=cast(str | None, data["modelId"]),
            turn_id=cast(str | None, data["turnId"]),
            saw_request_usage=cast(bool, data["sawRequestUsage"]),
            provider_name=optional_text(data.get("providerName")),
            context_compacted=cast(bool, data["contextCompacted"]),
            request_started_at=request_start,
            request_output_at=request_output,
            last_timed_response=cast(str | None, last_response),
            last_generation_ms=last_ms,
        )
        if (
            (not context.saw_session_meta and context.to_dict() != expected)
            or (context.kind is not None and not context.originator_ok)
            or (
                not context.accepted
                and (context.model_id is not None or context.turn_id is not None or context.saw_request_usage)
            )
        ):
            raise DomainError(SOURCE_CONTEXT_INVALID, "Codex 检查点上下文状态矛盾", "context")
        return context


def parse_record(record: object, ctx: CodexContext) -> Result:
    """Consume one decoded record; a reader passes None for a malformed JSON line."""
    item = _object(record)
    if item is None:
        ctx.model_id = None
        return Diagnostic("invalid_json")
    record_type = item.get("type")
    payload = _object(item.get("payload")) or {}
    if record_type == "session_meta":
        if ctx.saw_session_meta:
            return Skip()
        ctx.saw_session_meta = True
        ctx.file_session_id = optional_text(payload.get("id"))
        ctx.raw_file_session_id = cast(str, payload["id"]) if ctx.file_session_id is not None else None
        ctx.originator_ok = payload.get("originator") in ("Codex Desktop", "codex_work_desktop")
        ctx.kind = classify_source(payload.get("originator"), payload.get("source"))
        ctx.provider_name = optional_text(payload.get("model_provider"))
        return Skip() if ctx.accepted else Diagnostic("desktop_source_unverified")
    if not ctx.accepted:
        return Skip()
    if record_type == "compacted" or (
        record_type == "event_msg" and payload.get("type") == "context_compacted"
    ):
        ctx.context_compacted = True
        return Skip()
    if record_type == "turn_context":
        ctx.model_id = optional_text(payload.get("model"))
        ctx.turn_id = optional_text(payload.get("turn_id"))
        _start_request(item, ctx)
        return Skip()
    if record_type == "event_msg" and payload.get("type") in (
        "task_started",
        "task_complete",
        "turn_aborted",
    ):
        if payload.get("type") == "turn_aborted":
            ctx.request_started_at = ctx.request_output_at = None
        elif payload.get("type") == "task_started":
            _start_request(item, ctx)
        return Skip()
    if (
        record_type == "response_item"
        and (
            (payload.get("type") == "message" and payload.get("role") == "assistant")
            or payload.get("type") in ("function_call", "custom_tool_call")
        )
    ) or (record_type == "event_msg" and payload.get("type") == "agent_message"):
        # Completed assistant output precedes usage bookkeeping in delayed logs.
        # It bounds the request estimate; it is not a first-token timestamp.
        if ctx.request_started_at is not None:
            try:
                stamp = format_instant(parse_instant(cast(str, item.get("timestamp"))))
                if stamp >= ctx.request_started_at:
                    ctx.request_output_at = max(ctx.request_output_at or stamp, stamp)
            except DomainError:
                pass
        return Skip()
    if record_type == "response_item" and payload.get("type") in (
        "function_call_output",
        "custom_tool_call_output",
    ):
        # The next request starts after tool work, never at the previous completion.
        _start_request(item, ctx)
        return Skip()
    if record_type == "token_usage_record":
        ctx.saw_request_usage = True
        return _parse_usage(item, payload, ctx)
    if (
        record_type == "event_msg"
        and payload.get("type") == "token_count"
        and not ctx.saw_request_usage
        and _object(payload.get("info")) is not None
    ):
        return Diagnostic("request_usage_format_unverified")
    return Skip()


def _start_request(item: dict[str, object], ctx: CodexContext) -> None:
    ctx.request_output_at = None
    try:
        ctx.request_started_at = format_instant(parse_instant(cast(str, item.get("timestamp"))))
    except DomainError:
        ctx.request_started_at = None


def _parse_usage(record: dict[str, object], payload: dict[str, object], ctx: CodexContext) -> Result:
    session = optional_text(payload.get("thread_id"))
    raw_session = cast(str, payload["thread_id"]) if session is not None else ctx.raw_file_session_id
    session = session if session is not None else ctx.file_session_id
    if session is None or raw_session is None:
        return Diagnostic("missing_session_id")
    if ctx.file_session_id is not None and session != ctx.file_session_id:
        return Diagnostic("session_identity_mismatch")
    response = optional_text(payload.get("response_id"))
    if response is None:
        return Diagnostic("missing_response_id")
    usage = _object(payload.get("usage"))
    if usage is None:
        return Diagnostic("missing_usage")
    timestamp = record.get("timestamp")
    if timestamp is None:
        return Diagnostic("missing_timestamp")
    turn = optional_text(payload.get("turn_id"))
    model = ctx.model_id if turn is not None and turn == ctx.turn_id else None
    try:
        duration = ctx.last_generation_ms if response == ctx.last_timed_response else None
        if response != ctx.last_timed_response and ctx.request_started_at is not None and turn == ctx.turn_id:
            end = parse_instant(cast(str, timestamp))
            if ctx.request_output_at is not None:
                output_at = parse_instant(ctx.request_output_at)
                if parse_instant(ctx.request_started_at) < output_at <= end:
                    end = output_at
            elapsed = end.value - parse_instant(ctx.request_started_at).value
            milliseconds = elapsed // timedelta(milliseconds=1)
            duration = milliseconds if milliseconds > 0 else None
        tokens = Tokens(
            input=cast(int | None, usage.get("input_tokens")),
            cache_read=cast(int | None, usage.get("cached_input_tokens")),
            cache_write=cast(int | None, usage.get("cache_write_input_tokens")),
            output=cast(int | None, usage.get("output_tokens")),
            reasoning=cast(int | None, usage.get("reasoning_output_tokens")),
        )
        event = UsageEvent(
            harness=Harness.CODEX,
            dedupe_key=identity_key([raw_session, cast(str, payload["response_id"])]),
            session_id=session,
            request_id=response,
            model_id=model,
            occurred_at=cast(str, timestamp),
            tokens=tokens,
            status="reported",
            source_kind=ctx.kind,
            provider_name=optional_text(payload.get("model_provider")) or ctx.provider_name,
            issues=("context_compacted",) if ctx.context_compacted else (),
            generation_milliseconds=duration,
        )
        ctx.context_compacted = False
        ctx.request_started_at = ctx.request_output_at = None
        ctx.last_timed_response, ctx.last_generation_ms = response, duration
        return event
    except DomainError as error:
        return Diagnostic(error.code.lower())


def compaction_key(record: object, ctx: CodexContext) -> str | None:
    """Identify the actual compaction request even when its marker arrives later."""
    item = _object(record) or {}
    payload = _object(item.get("payload")) or {}
    if (
        not ctx.accepted
        or ctx.raw_file_session_id is None
        or not (
            item.get("type") == "compacted"
            or (item.get("type") == "event_msg" and payload.get("type") == "context_compacted")
        )
    ):
        return None
    response = payload.get("compaction_response_id")
    if optional_text(response) is None:
        return None
    latest = _object(payload.get("latest_token_usage_record")) or {}
    thread = optional_text(latest.get("thread_id"))
    if thread is not None and thread != ctx.file_session_id:
        return None
    return identity_key([ctx.raw_file_session_id, cast(str, response)])


def filter_diagnostics(diagnostics: Iterable[Diagnostic], ctx: CodexContext) -> tuple[Diagnostic, ...]:
    """Filter source-status warnings without changing the file's diagnostic cache."""
    # A snapshot can precede the first request record in a healthy stream. Clear that
    # provisional warning once the format is proven; keep snapshot-only coverage visible.
    hidden = {"desktop_source_unverified"}
    if not ctx.accepted or ctx.saw_request_usage:
        hidden.add("request_usage_format_unverified")
    return tuple(diagnostic for diagnostic in diagnostics if diagnostic.code not in hidden)
