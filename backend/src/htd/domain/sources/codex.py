"""Parse Codex request usage with explicit checkpointable state and no I/O."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

from htd.domain.common.errors import SOURCE_CONTEXT_INVALID, DomainError
from htd.domain.common.hashing import identity_key
from htd.domain.common.time import format_instant, from_epoch_ms, parse_instant
from htd.domain.usage.model import (
    OUTPUT_TIMING_UNCERTAIN,
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
    request_turn_id: str | None = None
    request_origin: str | None = None
    request_uncertain: bool = False
    task_active: bool = False
    task_context_seen: bool = False
    timing_latest_at: str | None = None
    last_user_at: str | None = None
    last_usage_at: str | None = None
    last_timing_uncertain: bool = False
    stream_started_at: str | None = None
    stream_completed_at: str | None = None
    stream_has_reasoning: bool = False

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
            "requestTurnId": self.request_turn_id,
            "requestOrigin": self.request_origin,
            "requestUncertain": self.request_uncertain,
            "taskActive": self.task_active,
            "taskContextSeen": self.task_context_seen,
            "timingLatestAt": self.timing_latest_at,
            "lastUserAt": self.last_user_at,
            "lastUsageAt": self.last_usage_at,
            "lastTimingUncertain": self.last_timing_uncertain,
            "streamStartedAt": self.stream_started_at,
            "streamCompletedAt": self.stream_completed_at,
            "streamHasReasoning": self.stream_has_reasoning,
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
            # Old checkpoints have no evidence about the active task lifecycle.
            "requestTurnId": data.get("turnId"),
            "requestOrigin": "turn" if data.get("requestStartedAt") else None,
            "requestUncertain": False,
            "taskActive": bool(data.get("requestStartedAt")),
            "taskContextSeen": bool(data.get("requestStartedAt")),
            "timingLatestAt": data.get("requestStartedAt"),
            "lastUserAt": None,
            "lastUsageAt": None,
            "lastTimingUncertain": False,
            "streamStartedAt": None,
            "streamCompletedAt": None,
            "streamHasReasoning": False,
            **data,
        }
        expected = cls().to_dict()
        if _object(data) is None or not expected.keys() <= data.keys():
            raise DomainError(SOURCE_CONTEXT_INVALID, "Codex 检查点上下文字段缺失", "context")
        for name in (
            "sawSessionMeta",
            "originatorOk",
            "sawRequestUsage",
            "contextCompacted",
            "requestUncertain",
            "taskActive",
            "taskContextSeen",
            "lastTimingUncertain",
            "streamHasReasoning",
        ):
            if type(data[name]) is not bool:
                raise DomainError(SOURCE_CONTEXT_INVALID, "上下文标志必须为布尔值", name)
        for name in ("fileSessionId", "modelId", "turnId", "requestTurnId"):
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
        for name in ("timingLatestAt", "lastUserAt", "lastUsageAt", "streamStartedAt", "streamCompletedAt"):
            if data[name] is not None:
                try:
                    data[name] = format_instant(parse_instant(cast(str, data[name])))
                except DomainError as error:
                    raise DomainError(SOURCE_CONTEXT_INVALID, "请求计时时间无效", name) from error
        if data["requestOrigin"] not in (None, "turn", "task", "tool", "user"):
            raise DomainError(SOURCE_CONTEXT_INVALID, "请求起点类型无效", "requestOrigin")
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
            request_turn_id=cast(str | None, data["requestTurnId"]),
            request_origin=cast(str | None, data["requestOrigin"]),
            request_uncertain=cast(bool, data["requestUncertain"]),
            task_active=cast(bool, data["taskActive"]),
            task_context_seen=cast(bool, data["taskContextSeen"]),
            timing_latest_at=cast(str | None, data["timingLatestAt"]),
            last_user_at=cast(str | None, data["lastUserAt"]),
            last_usage_at=cast(str | None, data["lastUsageAt"]),
            last_timing_uncertain=cast(bool, data["lastTimingUncertain"]),
            stream_started_at=cast(str | None, data["streamStartedAt"]),
            stream_completed_at=cast(str | None, data["streamCompletedAt"]),
            stream_has_reasoning=cast(bool, data["streamHasReasoning"]),
        )
        if (
            (context.stream_started_at is None) != (context.stream_completed_at is None)
            or (context.stream_has_reasoning and context.stream_started_at is None)
        ) or (
            context.stream_started_at is not None
            and (
                context.request_started_at is None
                or context.stream_completed_at is None
                or not context.request_started_at <= context.stream_started_at < context.stream_completed_at
            )
        ):
            raise DomainError(SOURCE_CONTEXT_INVALID, "流式计时边界无效", "streamStartedAt")
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
        if _stale_boundary(item, ctx):
            return Skip()
        previous_turn = ctx.turn_id
        ctx.model_id = optional_text(payload.get("model"))
        ctx.turn_id = optional_text(payload.get("turn_id"))
        if (
            ctx.task_active
            and not ctx.task_context_seen
            and ctx.request_origin in ("task", "user")
            and ctx.request_turn_id in (None, ctx.turn_id)
        ):
            # task_started/user submission can precede the first turn_context.
            ctx.request_turn_id = ctx.turn_id
        elif ctx.turn_id != previous_turn or ctx.turn_id is None:
            _start_request(item, ctx, "turn", ctx.turn_id)
        # Repeated context in one active turn is not a new network request.
        ctx.task_context_seen = True
        return Skip()
    if record_type == "event_msg" and payload.get("type") == "item_completed":
        _stream_item(item, payload, ctx)
        return Skip()
    if (record_type == "event_msg" and payload.get("type") == "user_message") or (
        record_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "user"
    ):
        _user_message(item, payload, ctx, committed=record_type == "event_msg")
        return Skip()
    if record_type == "event_msg" and payload.get("type") in (
        "task_started",
        "task_complete",
        "turn_aborted",
    ):
        turn = optional_text(payload.get("turn_id"))
        if _stale_boundary(item, ctx):
            return Skip()
        if turn is not None and turn != ctx.request_turn_id and payload.get("type") != "task_started":
            return Skip()
        if payload.get("type") == "turn_aborted":
            ctx.request_started_at = ctx.request_output_at = None
            ctx.stream_started_at = ctx.stream_completed_at = None
            ctx.stream_has_reasoning = False
            ctx.request_uncertain = True
            ctx.task_active = False
        elif payload.get("type") == "task_started":
            if not ctx.task_active or (turn is not None and turn != ctx.request_turn_id):
                # A freshly restored/context-seeded parser can already know the
                # turn. Never borrow a prior request's turn for an unbound task.
                task_turn = turn or (ctx.turn_id if ctx.request_turn_id is None else None)
                _start_request(item, ctx, "task", task_turn)
                ctx.task_context_seen = False
        else:
            # Keep the completed clock until its delayed usage record arrives.
            ctx.task_active = False
        if payload.get("type") != "task_started":
            ctx.timing_latest_at = _timestamp(item) or ctx.timing_latest_at
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
        turn = optional_text(payload.get("turn_id"))
        if ctx.request_started_at is not None and turn in (None, ctx.request_turn_id):
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
        if not _stale_boundary(item, ctx):
            pending_output = (
                ctx.request_output_at is not None
                or ctx.stream_started_at is not None
                or (ctx.request_uncertain and ctx.request_started_at is not None)
            )
            tool_turn = optional_text(payload.get("turn_id")) or ctx.request_turn_id
            if tool_turn is None and not ctx.task_active:
                tool_turn = ctx.turn_id
            _start_request(item, ctx, "tool", tool_turn)
            # Without a response identity, late usage could describe either span.
            ctx.request_uncertain |= pending_output
            ctx.task_context_seen = True
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


def _timestamp(item: dict[str, object]) -> str | None:
    try:
        return format_instant(parse_instant(cast(str, item.get("timestamp"))))
    except DomainError:
        return None


def _stale_boundary(item: dict[str, object], ctx: CodexContext) -> bool:
    stamp = _timestamp(item)
    return stamp is not None and ctx.timing_latest_at is not None and stamp < ctx.timing_latest_at


def _start_request(item: dict[str, object], ctx: CodexContext, origin: str, turn: str | None) -> None:
    ctx.request_started_at = _timestamp(item)
    ctx.request_output_at = None
    ctx.stream_started_at = ctx.stream_completed_at = None
    ctx.stream_has_reasoning = False
    ctx.request_turn_id = turn
    ctx.request_origin = origin
    ctx.request_uncertain = ctx.request_started_at is None
    ctx.task_active = True
    if ctx.request_started_at is not None:
        ctx.timing_latest_at = ctx.request_started_at


def _stream_item(record: dict[str, object], payload: dict[str, object], ctx: CodexContext) -> None:
    """Use model-item streaming clocks; command/file-change clocks time tool work."""
    item = _object(payload.get("item")) or {}
    if (
        item.get("type") not in ("Reasoning", "AgentMessage")
        or ctx.request_started_at is None
        or ctx.request_uncertain
        or payload.get("thread_id") != ctx.file_session_id
        or payload.get("turn_id") != ctx.request_turn_id
    ):
        return
    try:
        start = format_instant(from_epoch_ms(cast(int, payload.get("started_at_ms"))))
        end = format_instant(from_epoch_ms(cast(int, payload.get("completed_at_ms"))))
        reported = _timestamp(record)
        if reported is None or not ctx.request_started_at <= start < end <= reported:
            return
    except DomainError:
        return
    ctx.stream_started_at = min(ctx.stream_started_at or start, start)
    ctx.stream_completed_at = max(ctx.stream_completed_at or end, end)
    ctx.stream_has_reasoning |= item.get("type") == "Reasoning"


def _user_message(
    item: dict[str, object], payload: dict[str, object], ctx: CodexContext, *, committed: bool
) -> None:
    stamp = _timestamp(item)
    if stamp is not None and (
        (ctx.last_user_at is not None and stamp <= ctx.last_user_at) or _stale_boundary(item, ctx)
    ):
        return
    if not committed:
        # role=user also carries replayed context/approval envelopes. Only the
        # user_message event establishes an idle submission boundary.
        if ctx.task_active and ctx.task_context_seen:
            ctx.request_uncertain = True
        return
    if not ctx.task_active:
        # A submitted prompt is an idle/new-turn boundary, never a keyboard timer.
        _start_request(item, ctx, "user", optional_text(payload.get("turn_id")))
        ctx.task_context_seen = False
    elif (
        not ctx.task_context_seen
        and ctx.request_origin in ("task", "user")
        and ctx.request_output_at is None
        and ctx.stream_started_at is None
        and (
            ctx.last_user_at is None
            or (ctx.request_started_at is not None and ctx.last_user_at < ctx.request_started_at)
        )
    ):
        # The initial prompt envelope may follow task_started. It must precede
        # the first context; a later steering message cannot shorten this clock.
        if stamp is not None:
            ctx.request_started_at = max(ctx.request_started_at or stamp, stamp)
            ctx.timing_latest_at = stamp
    else:
        ctx.request_uncertain = True
    if stamp is None:
        ctx.request_uncertain = True
    else:
        ctx.last_user_at = stamp


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
        tokens = Tokens(
            input=cast(int | None, usage.get("input_tokens")),
            cache_read=cast(int | None, usage.get("cached_input_tokens")),
            cache_write=cast(int | None, usage.get("cache_write_input_tokens")),
            output=cast(int | None, usage.get("output_tokens")),
            reasoning=cast(int | None, usage.get("reasoning_output_tokens")),
        )
        reported_at = format_instant(parse_instant(cast(str, timestamp)))
        replay = response == ctx.last_timed_response
        matching_turn = turn is not None and turn == ctx.request_turn_id
        older_usage = ctx.last_usage_at is not None and reported_at < ctx.last_usage_at
        before_start = ctx.request_started_at is not None and reported_at <= ctx.request_started_at
        uncertain = (
            ctx.last_timing_uncertain
            if replay
            else (
                ctx.request_uncertain
                or older_usage
                or before_start
                or (ctx.request_started_at is not None and not matching_turn)
            )
        )
        duration = ctx.last_generation_ms if replay else None
        if not replay and not uncertain and ctx.request_started_at is not None and matching_turn:
            start = parse_instant(ctx.request_started_at)
            end = parse_instant(reported_at)
            if ctx.request_output_at is not None:
                output_at = parse_instant(ctx.request_output_at)
                if start < output_at <= end:
                    end = output_at
            if (
                ctx.stream_started_at is not None
                and ctx.stream_completed_at is not None
                and ctx.request_output_at is not None
                and (tokens.reasoning == 0 or ctx.stream_has_reasoning)
                and ctx.stream_started_at < ctx.request_output_at <= reported_at
                and ctx.stream_completed_at <= reported_at
            ):
                # output_tokens already includes reasoning. Start with its first
                # streamed model item, then include reasoning, text and tool args.
                start = parse_instant(ctx.stream_started_at)
                end = max(end, parse_instant(ctx.stream_completed_at))
            elapsed = end.value - start.value
            milliseconds = elapsed // timedelta(milliseconds=1)
            duration = milliseconds if milliseconds > 0 else None
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
            issues=(("context_compacted",) if ctx.context_compacted else ())
            + ((OUTPUT_TIMING_UNCERTAIN,) if uncertain else ()),
            generation_milliseconds=duration,
        )
        ctx.context_compacted = False
        # Replays and old-turn reports must not consume a newer request's clock.
        if not replay and not older_usage and not before_start and matching_turn:
            ctx.request_started_at = ctx.request_output_at = None
            ctx.stream_started_at = ctx.stream_completed_at = None
            ctx.stream_has_reasoning = False
            ctx.request_origin = None
        if not replay and not older_usage and (matching_turn or ctx.request_started_at is None):
            ctx.last_timed_response, ctx.last_generation_ms = response, duration
            ctx.last_timing_uncertain = uncertain
            ctx.last_usage_at = reported_at
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
