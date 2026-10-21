"""Use observed model streaming clocks, retaining inclusive reasoning and safe fallbacks."""

import json
from datetime import timedelta

import pytest
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.sources.codex import CodexContext, parse_record
from htd.domain.usage.model import OUTPUT_TIMING_UNCERTAIN, UsageEvent

BASE = parse_instant("2026-10-21T00:00:00Z")
EPOCH_MS = 1792540800000


def stamp(second: int) -> str:
    return format_instant(Instant(BASE.value + timedelta(seconds=second)))


def context() -> CodexContext:
    ctx = CodexContext(
        file_session_id="s",
        raw_file_session_id="s",
        saw_session_meta=True,
        originator_ok=True,
        kind="desktop",
    )
    parse_record(
        {"type": "turn_context", "timestamp": stamp(0), "payload": {"model": "m", "turn_id": "t"}},
        ctx,
    )
    return ctx


def streamed(start: int = 30, end: int = 50, kind: str = "Reasoning") -> dict[str, object]:
    return {
        "type": "item_completed",
        "thread_id": "s",
        "turn_id": "t",
        "started_at_ms": EPOCH_MS + start * 1000,
        "completed_at_ms": EPOCH_MS + end * 1000,
        "item": {"type": kind, "raw_content": ["PRIVATE REASONING"], "content": ["PRIVATE ANSWER"]},
    }


def item(ctx: CodexContext, payload: dict[str, object], at: int = 60) -> None:
    parse_record({"type": "event_msg", "timestamp": stamp(at), "payload": payload}, ctx)


def complete(ctx: CodexContext, second: int = 65, kind: str = "function_call") -> None:
    parse_record(
        {"type": "response_item", "timestamp": stamp(second), "payload": {"type": kind, "role": "assistant"}},
        ctx,
    )


def usage(ctx: CodexContext, *, reasoning: int | None = 800, response: str = "r") -> UsageEvent:
    result = parse_record(
        {
            "type": "token_usage_record",
            "timestamp": stamp(130),
            "payload": {
                "thread_id": "s",
                "turn_id": "t",
                "response_id": response,
                "usage": {"output_tokens": 1400, "reasoning_output_tokens": reasoning},
            },
        },
        ctx,
    )
    assert isinstance(result, UsageEvent)
    return result


def test_stream_start_excludes_prefill_and_preserves_reasoning_text_and_tool_arguments() -> None:
    ctx = context()
    item(ctx, streamed())
    ctx = CodexContext.from_dict(json.loads(json.dumps(ctx.to_dict())))
    item(ctx, streamed(50, 60, "AgentMessage"))
    complete(ctx)
    # Tool execution can finish before the delayed usage report. Its timing must
    # not become the end of generation, or the beginning of the next response.
    item(ctx, streamed(65, 120, "CommandExecution"), at=120)
    result = usage(ctx)
    assert result.tokens.output == 1400 and result.tokens.reasoning == 800
    assert result.generation_milliseconds == 35_000
    assert "PRIVATE" not in repr(ctx.to_dict())
    assert ctx.stream_started_at is None and not ctx.stream_has_reasoning
    assert usage(ctx).generation_milliseconds == result.generation_milliseconds


@pytest.mark.parametrize("reasoning,expected", [(800, 65_000), (None, 65_000), (0, 15_000)])
def test_answer_only_clock_cannot_drop_unobserved_reasoning(reasoning: int | None, expected: int) -> None:
    ctx = context()
    item(ctx, streamed(50, 60, "AgentMessage"))
    complete(ctx)
    assert usage(ctx, reasoning=reasoning).generation_milliseconds == expected


@pytest.mark.parametrize(
    "patch",
    [
        {"thread_id": "other"},
        {"turn_id": "other"},
        {"turn_id": None},
        {"started_at_ms": True},
        {"started_at_ms": str(EPOCH_MS)},
        {"started_at_ms": EPOCH_MS - 1},
        {"started_at_ms": EPOCH_MS + 70_000},
        {"completed_at_ms": EPOCH_MS + 61_000},
        {"completed_at_ms": 10**30},
        {"item": {"type": "CommandExecution"}},
        {"item": {"type": "FileChange"}},
        {"item": {"type": "UserMessage"}},
    ],
)
def test_invalid_foreign_and_tool_clocks_do_not_shorten_the_request(patch: dict[str, object]) -> None:
    ctx = context()
    item(ctx, {**streamed(), **patch})
    complete(ctx)
    assert usage(ctx).generation_milliseconds == 65_000


def test_steering_excludes_even_a_valid_stream_clock_and_new_tool_result_clears_it() -> None:
    ctx = context()
    item(ctx, streamed())
    parse_record({"type": "event_msg", "timestamp": stamp(60), "payload": {"type": "user_message"}}, ctx)
    complete(ctx)
    assert OUTPUT_TIMING_UNCERTAIN in usage(ctx).issues
    complete(ctx, 150, "function_call_output")
    assert ctx.stream_started_at is None
    item(ctx, streamed(160, 170), at=170)
    complete(ctx, 175)
    ctx = CodexContext.from_dict(ctx.to_dict())
    result = parse_record(
        {
            "type": "token_usage_record",
            "timestamp": stamp(180),
            "payload": {
                "thread_id": "s",
                "turn_id": "t",
                "response_id": "next",
                "usage": {"output_tokens": 600, "reasoning_output_tokens": 400},
            },
        },
        ctx,
    )
    assert isinstance(result, UsageEvent) and result.generation_milliseconds == 15_000
