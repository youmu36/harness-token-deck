"""Parse decoded Deepseek Harness records without retaining conversation content."""

from dataclasses import dataclass
from typing import cast

from htd.domain.common.errors import DomainError
from htd.domain.common.hashing import identity_key
from htd.domain.common.time import Instant, format_instant, from_epoch_ms
from htd.domain.usage.model import Diagnostic, Harness, Result, Skip, Tokens, UsageEvent, optional_text


def _object(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if type(value) is dict else None


def _first_in_row(stream: object) -> int | None:
    """Read timing and nonempty delta presence without retaining conversation text."""
    if type(stream) is not list:
        return None
    for raw in cast(list[object], stream):
        row = _object(raw)
        if row is None:
            continue
        kind = row.get("type")
        if kind in ("text-chunks", "reasoning-chunks", "tool-call-chunks"):
            start = row.get("time0")
            if type(start) is not int or start < 0:
                continue
            if kind == "tool-call-chunks" and type(row.get("name")) is str:
                return start
            fragments, gaps = row.get("args" if kind == "tool-call-chunks" else "texts"), row.get("dt")
            if type(fragments) is not list or type(gaps) is not list:
                continue
            fragments, gaps = cast(list[object], fragments), cast(list[object], gaps)
            for index, fragment in enumerate(fragments):
                if index:
                    if (
                        index > len(gaps)
                        or type(gaps[index - 1]) is not int
                        or cast(int, gaps[index - 1]) < 0
                    ):
                        break
                    start += cast(int, gaps[index - 1])
                if type(fragment) is str and fragment:
                    return start
        elif kind == "chunk":
            chunk = _object(row.get("chunk")) or {}
            text, arguments, name = chunk.get("text"), chunk.get("argumentsDelta"), chunk.get("name")
            token = (
                chunk.get("type") in ("text-delta", "reasoning-delta") and type(text) is str and bool(text)
            ) or (
                chunk.get("type") == "tool-call-delta"
                and ((type(arguments) is str and bool(arguments)) or type(name) is str)
            )
            stamp = row.get("time")
            if token and type(stamp) is int:
                return stamp
    return None


def first_token_time(stream: object) -> int | None:
    # Stream sections may be grouped by kind instead of chronological order.
    # In particular, reasoning can precede a text section stored before it.
    if type(stream) is not list:
        return None
    return min(
        (stamp for row in cast(list[object], stream) if (stamp := _first_in_row([row])) is not None),
        default=None,
    )


def last_token_time(stream: object) -> int | None:
    """End at the last token delta, excluding delayed usage/finish bookkeeping."""
    if type(stream) is not list:
        return None
    latest = None
    for raw in cast(list[object], stream):
        row = _object(raw) or {}
        first = _first_in_row([row])
        if first is None:
            continue
        latest = max(latest if latest is not None else first, first)
        stamp = row.get("time")
        if row.get("type") in ("text-chunks", "reasoning-chunks", "tool-call-chunks"):
            stamp = row.get("time0")
            fragments = row.get("args" if row.get("type") == "tool-call-chunks" else "texts")
            gaps = row.get("dt")
            if type(stamp) is not int or type(fragments) is not list or type(gaps) is not list:
                continue
            gaps = cast(list[object], gaps)
            for index, fragment in enumerate(cast(list[object], fragments)):
                if index:
                    if (
                        index > len(gaps)
                        or type(gaps[index - 1]) is not int
                        or cast(int, gaps[index - 1]) < 0
                    ):
                        break
                    stamp += cast(int, gaps[index - 1])
                if type(fragment) is str and fragment:
                    latest = max(latest or first, stamp)
        elif type(stamp) is int:
            latest = max(latest or first, stamp)
    return latest


@dataclass(slots=True)
class DshContext:
    session_id: str | None = None
    model_id: str | None = None
    raw_session_id: str | None = None
    provider_name: str | None = None
    attempt: tuple[int, int, int, int | None] | None = None


def parse_record(record: object, ctx: DshContext, started_at: Instant | None = None) -> Result:
    item = _object(record)
    if item is None:
        ctx.model_id = None
        return Diagnostic("invalid_json")
    record_type = item.get("type")
    if record_type == "session":
        ctx.attempt = None
        ctx.session_id = optional_text(item.get("id"))
        ctx.raw_session_id = cast(str, item["id"]) if ctx.session_id is not None else None
        return Skip()
    data = _object(item.get("data")) or {}
    if record_type == "model/selection":
        ctx.model_id = optional_text(data.get("model"))
        ctx.provider_name = optional_text(data.get("provider"))
        return Skip()
    if record_type in ("step/start", "step/end", "turn/end"):
        ctx.attempt = None
        return Skip()
    if record_type == "assistant/attempt":
        first = first_token_time(data.get("stream"))
        turn, step = data.get("turn"), data.get("step")
        if type(turn) is int and type(step) is int and first is not None:
            ctx.attempt = (turn, step, first, last_token_time(data.get("stream")))
        return Skip()
    if record_type == "request/header":
        header = _object(data.get("header")) or {}
        config = _object(header.get("config")) or {}
        ctx.model_id = optional_text(config.get("model"))
        ctx.provider_name = optional_text(config.get("provider")) or optional_text(
            config.get("provider_name")
        )
        return Skip()
    if record_type != "assistant/message":
        return Skip()
    usage = _object(data.get("usage"))
    if usage is None:
        return Skip()
    if ctx.session_id is None or ctx.raw_session_id is None:
        return Diagnostic("missing_session_id")
    turn, step, timestamp = data.get("turn"), data.get("step"), item.get("time")
    if type(turn) is not int:
        return Diagnostic("missing_turn")
    if type(step) is not int:
        return Diagnostic("missing_step")
    if type(timestamp) is not int:
        return Diagnostic("missing_timestamp")
    try:
        # Validate uncached input independently: adding cache must not hide invalid values.
        uncached = Tokens(
            input=cast(int | None, usage.get("inputTokens")),
            output=cast(int | None, usage.get("outputTokens")),
            reasoning=cast(int | None, usage.get("reasoningTokens")),
        )
        cache_read = Tokens(cache_read=cast(int | None, usage.get("cacheReadTokens"))).cache_read
        # Some DSH providers omit zero cache counts. Only an exact total proves
        # the missing count; an absent cache is never blindly treated as zero.
        total = usage.get("totalTokens")
        if (
            cache_read is None
            and uncached.input is not None
            and uncached.output is not None
            and type(total) is int
            and 0 <= total <= 2**53 - 1
            and total >= uncached.input + uncached.output
        ):
            cache_read = total - uncached.input - uncached.output
        total_input = uncached.input
        if total_input is not None and cache_read is not None:
            total_input += cache_read
        tokens = Tokens(total_input, cache_read, 0, uncached.output, uncached.reasoning)
        occurred_at = from_epoch_ms(timestamp)
        first = first_token_time(data.get("stream"))
        last = last_token_time(data.get("stream"))
        if ctx.attempt is not None and ctx.attempt[:2] == (turn, step):
            first = min(first if first is not None else ctx.attempt[2], ctx.attempt[2])
            last = max(last or first, ctx.attempt[3] or first)
        end = last if first is not None and last is not None and first < last <= timestamp else timestamp
        duration = end - first if first is not None and 0 <= first < end else None
        event = UsageEvent(
            harness=Harness.DSH,
            dedupe_key=identity_key([ctx.raw_session_id, turn, step]),
            session_id=ctx.session_id,
            native_session_id=ctx.raw_session_id,
            request_id=f"{turn}:{step}",
            model_id=ctx.model_id,
            occurred_at=format_instant(occurred_at),
            tokens=tokens,
            status="reported",
            provider_name=ctx.provider_name,
            generation_milliseconds=duration,
        )
        return Skip() if started_at is not None and occurred_at < started_at else event
    except DomainError as error:
        return Diagnostic(error.code.lower())
