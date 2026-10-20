"""Timing evidence handles short bursts, long reasoning and delayed usage."""

from htu.domain.sources.codex import CodexContext
from htu.domain.sources.codex import parse_record as codex
from htu.domain.sources.dsh import DshContext
from htu.domain.sources.dsh import parse_record as dsh
from htu.domain.usage.model import UsageEvent


def test_codex_request_clock_survives_checkpoint_and_counts_long_reasoning() -> None:
    ctx = CodexContext(
        file_session_id="s",
        raw_file_session_id="s",
        saw_session_meta=True,
        originator_ok=True,
        kind="desktop",
    )
    codex(
        {
            "type": "turn_context",
            "timestamp": "2026-10-19T00:00:00Z",
            "payload": {"model": "gpt", "turn_id": "t"},
        },
        ctx,
    )
    ctx = CodexContext.from_dict(ctx.to_dict())
    event = codex(
        {
            "type": "token_usage_record",
            "timestamp": "2026-10-19T00:05:00Z",
            "payload": {
                "thread_id": "s",
                "turn_id": "t",
                "response_id": "r",
                "usage": {"output_tokens": 18000},
            },
        },
        ctx,
    )
    assert isinstance(event, UsageEvent) and event.generation_milliseconds == 300_000
    assert ctx.request_started_at is None
    # Tool execution is excluded: the new clock begins when the result arrives.
    codex(
        {
            "type": "response_item",
            "timestamp": "2026-10-19T00:08:00Z",
            "payload": {"type": "function_call_output"},
        },
        ctx,
    )
    event = codex(
        {
            "type": "token_usage_record",
            "timestamp": "2026-10-19T00:08:10Z",
            "payload": {
                "thread_id": "s",
                "turn_id": "t",
                "response_id": "r2",
                "usage": {"output_tokens": 600},
            },
        },
        ctx,
    )
    assert isinstance(event, UsageEvent) and event.generation_milliseconds == 10_000


def test_dsh_ends_at_last_delta_and_excludes_abandoned_attempt() -> None:
    ctx = DshContext(session_id="s", raw_session_id="s", model_id="m")

    def attempt(start: int) -> None:
        dsh(
            {
                "type": "assistant/attempt",
                "data": {
                    "turn": 1,
                    "step": 1,
                    "stream": [{"type": "text-chunks", "time0": start, "texts": ["a", "b"], "dt": [10000]}],
                },
            },
            ctx,
        )

    attempt(1000)
    attempt(100000)
    event = dsh(
        {
            "type": "assistant/message",
            "time": 150000,
            "data": {"turn": 1, "step": 1, "usage": {"inputTokens": 1, "outputTokens": 600}},
        },
        ctx,
    )
    assert isinstance(event, UsageEvent) and event.generation_milliseconds == 10_000
