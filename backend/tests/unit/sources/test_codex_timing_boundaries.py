"""Submitted prompts, steering and delayed reports never invent request time."""

from datetime import timedelta

import pytest
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.sources.codex import CodexContext, parse_record
from htd.domain.usage.model import OUTPUT_TIMING_UNCERTAIN, UsageEvent

BASE = parse_instant("2026-10-20T00:00:00Z")


def stamp(second: int) -> str:
    return format_instant(Instant(BASE.value + timedelta(seconds=second)))


def context() -> CodexContext:
    return CodexContext(
        file_session_id="s",
        raw_file_session_id="s",
        saw_session_meta=True,
        originator_ok=True,
        kind="desktop",
    )


def signal(ctx: CodexContext, second: int, kind: str, **fields: object) -> None:
    parse_record({"type": "event_msg", "timestamp": stamp(second), "payload": {"type": kind, **fields}}, ctx)


def turn(ctx: CodexContext, second: int, name: str = "t") -> None:
    parse_record(
        {"type": "turn_context", "timestamp": stamp(second), "payload": {"model": "m", "turn_id": name}},
        ctx,
    )


def output(ctx: CodexContext, second: int, kind: str = "message", role: str = "assistant") -> None:
    parse_record(
        {"type": "response_item", "timestamp": stamp(second), "payload": {"type": kind, "role": role}},
        ctx,
    )


def usage(ctx: CodexContext, second: int, response: str = "r", name: str = "t") -> UsageEvent:
    event = parse_record(
        {
            "type": "token_usage_record",
            "timestamp": stamp(second),
            "payload": {"turn_id": name, "response_id": response, "usage": {"output_tokens": 1200}},
        },
        ctx,
    )
    assert isinstance(event, UsageEvent)
    return event


@pytest.mark.parametrize("explicit_task", [False, True])
def test_new_submission_excludes_idle_typing_and_context_does_not_restart_it(explicit_task: bool) -> None:
    ctx = context()
    turn(ctx, 0, "old")
    signal(ctx, 10, "task_complete", turn_id="old")
    if explicit_task:
        signal(ctx, 300, "task_started", turn_id="t")
    signal(ctx, 301, "user_message", message="PRIVATE INPUT")
    output(ctx, 301, role="user")  # The same submission has two log representations.
    turn(ctx, 302)
    turn(ctx, 310)  # Context refresh is not a new request or first-token event.
    signal(ctx, 312, "task_started", turn_id="t")  # A duplicate start cannot shorten it either.
    output(ctx, 321)
    signal(ctx, 322, "task_complete", turn_id="t")
    ctx = CodexContext.from_dict(ctx.to_dict())
    result = usage(ctx, 400)
    assert result.generation_milliseconds == 20_000
    assert OUTPUT_TIMING_UNCERTAIN not in result.issues
    assert "PRIVATE" not in repr(ctx.to_dict())


@pytest.mark.parametrize("representation", ["event", "response"])
def test_steering_keeps_original_start_and_waits_for_a_reliable_request(representation: str) -> None:
    ctx = context()
    signal(ctx, 0, "task_started", turn_id="t")
    signal(ctx, 1, "user_message")
    turn(ctx, 2)
    if representation == "event":
        signal(ctx, 180, "user_message")
    else:
        output(ctx, 180, role="user")
    assert ctx.request_started_at == stamp(1)
    ctx = CodexContext.from_dict(ctx.to_dict())
    output(ctx, 190)
    result = usage(ctx, 191)
    assert result.generation_milliseconds is None
    assert OUTPUT_TIMING_UNCERTAIN in result.issues
    assert usage(ctx, 191) == result  # Checkpoint/replay retains the exclusion.
    assert OUTPUT_TIMING_UNCERTAIN in usage(ctx, 192, "unknown-next").issues
    output(ctx, 200, kind="function_call_output")
    assert OUTPUT_TIMING_UNCERTAIN not in usage(ctx, 220, "recovered").issues
    assert ctx.last_generation_ms == 20_000


def test_role_user_replay_does_not_establish_a_submission_clock() -> None:
    ctx = context()
    output(ctx, 1, role="user")
    assert ctx.request_started_at is None
    turn(ctx, 300)
    assert usage(ctx, 320).generation_milliseconds == 20_000


@pytest.mark.parametrize("completed_output", [False, True])
def test_interaction_before_missing_context_cannot_restart_an_existing_request(
    completed_output: bool,
) -> None:
    ctx = context()
    signal(ctx, 0, "task_started", turn_id="t")
    signal(ctx, 1, "user_message")
    if completed_output:
        output(ctx, 20)
    signal(ctx, 180, "user_message")
    assert ctx.request_started_at == stamp(1)
    turn(ctx, 181)
    assert OUTPUT_TIMING_UNCERTAIN in usage(ctx, 190).issues


def test_late_completion_cannot_end_a_new_task_before_its_context_arrives() -> None:
    ctx = context()
    turn(ctx, 0, "old")
    usage(ctx, 20, "old", "old")
    signal(ctx, 21, "task_complete", turn_id="old")
    signal(ctx, 300, "task_started")
    signal(ctx, 301, "task_complete", turn_id="old")
    assert ctx.task_active
    signal(ctx, 302, "user_message")
    turn(ctx, 303)
    assert usage(ctx, 322).generation_milliseconds == 20_000


def test_duplicate_and_late_old_turn_usage_do_not_consume_new_clock() -> None:
    ctx = context()
    turn(ctx, 0, "old")
    first = usage(ctx, 20, "old-response", "old")
    signal(ctx, 21, "task_complete", turn_id="old")
    signal(ctx, 300, "task_started", turn_id="t")
    turn(ctx, 301)
    replay = usage(ctx, 20, "old-response", "old")
    assert replay.generation_milliseconds == first.generation_milliseconds
    assert OUTPUT_TIMING_UNCERTAIN not in replay.issues
    late = usage(ctx, 400, "another-old-response", "old")
    assert OUTPUT_TIMING_UNCERTAIN in late.issues
    assert ctx.request_started_at == stamp(300)
    assert ctx.last_usage_at == stamp(20)
    # A historical context and completion must not roll the active turn back.
    turn(ctx, 1, "old")
    signal(ctx, 401, "task_complete", turn_id="old")
    assert ctx.turn_id == "t" and ctx.task_active
    assert usage(ctx, 320, "new-response").generation_milliseconds == 20_000


def test_same_turn_delayed_usage_across_tool_boundary_is_excluded() -> None:
    ctx = context()
    turn(ctx, 0)
    output(ctx, 20, kind="function_call")
    output(ctx, 180, kind="function_call_output")
    assert OUTPUT_TIMING_UNCERTAIN in usage(ctx, 190, "late").issues
    output(ctx, 210)
    assert OUTPUT_TIMING_UNCERTAIN in usage(ctx, 211, "ambiguous").issues
    output(ctx, 250, kind="custom_tool_call_output")
    assert usage(ctx, 270, "recovered").generation_milliseconds == 20_000


def test_old_usage_timestamp_and_abort_cannot_provide_time_for_a_new_request() -> None:
    ctx = context()
    turn(ctx, 100)
    assert OUTPUT_TIMING_UNCERTAIN in usage(ctx, 20, "old").issues
    assert ctx.request_started_at == stamp(100)
    signal(ctx, 110, "turn_aborted", turn_id="t")
    assert OUTPUT_TIMING_UNCERTAIN in usage(ctx, 120, "aborted").issues
    signal(ctx, 300, "user_message")
    turn(ctx, 301, "new")
    assert usage(ctx, 320, "new", "new").generation_milliseconds == 20_000


def test_new_timing_fields_are_optional_for_old_checkpoints() -> None:
    ctx = context()
    turn(ctx, 1)
    previous_keys = {
        "fileSessionId",
        "rawFileSessionId",
        "sawSessionMeta",
        "originatorOk",
        "kind",
        "modelId",
        "turnId",
        "sawRequestUsage",
        "providerName",
        "contextCompacted",
        "requestStartedAt",
        "requestOutputAt",
        "lastTimedResponse",
        "lastGenerationMs",
    }
    old = {key: value for key, value in ctx.to_dict().items() if key in previous_keys}
    restored = CodexContext.from_dict(old)
    assert usage(restored, 21).generation_milliseconds == 20_000
