"""Claude Code assistant usage; content blocks share one API message identity."""

from typing import cast

from htd.domain.common.errors import DomainError
from htd.domain.common.hashing import identity_key
from htd.domain.common.time import format_instant, parse_instant
from htd.domain.usage.model import Diagnostic, Harness, Result, Skip, Tokens, UsageEvent, optional_text


def parse_record(record: object) -> Result:
    if type(record) is not dict:
        return Diagnostic("invalid_json")
    item = cast(dict[str, object], record)
    if item.get("type") != "assistant":
        return Skip()
    raw = item.get("message")
    if type(raw) is not dict:
        return Skip()
    message = cast(dict[str, object], raw)
    raw_usage = message.get("usage")
    if type(raw_usage) is not dict or message.get("model") in ("<synthetic>", "<local>"):
        return Skip()
    usage = cast(dict[str, object], raw_usage)
    session, request = optional_text(item.get("sessionId")), optional_text(message.get("id"))
    if not session or not request:
        return Diagnostic("missing_request_id")
    try:
        uncached = Tokens(input=cast(int | None, usage.get("input_tokens")))
        cache = Tokens(
            cache_read=cast(int | None, usage.get("cache_read_input_tokens")),
            cache_write=cast(int | None, usage.get("cache_creation_input_tokens")),
        )
        total = uncached.input
        if total is not None:
            total += (cache.cache_read or 0) + (cache.cache_write or 0)
        return UsageEvent(
            harness=Harness.CLAUDE,
            # Anthropic message IDs identify API requests across content blocks,
            # copied session history and subagent transcript files.
            dedupe_key=identity_key([request]),
            session_id=session,
            native_session_id=cast(str, item["sessionId"]),
            request_id=request,
            model_id=optional_text(message.get("model")),
            occurred_at=format_instant(parse_instant(cast(str, item.get("timestamp")))),
            tokens=Tokens(
                total, cache.cache_read, cache.cache_write, cast(int | None, usage.get("output_tokens"))
            ),
            status="reported",
            provider_name=optional_text(item.get("providerName"))
            or optional_text(item.get("provider"))
            or optional_text(item.get("baseUrl")),
        )
    except DomainError as error:
        return Diagnostic(error.code.lower())
