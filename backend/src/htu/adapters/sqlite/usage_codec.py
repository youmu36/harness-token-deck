"""Strictly encode usage mementos and reject ambiguous or malformed persisted JSON."""

import json
from dataclasses import asdict
from typing import Never, cast

from htu.domain.usage.model import Harness, SourceKind, Tokens, UsageEvent, UsageStatus

_TOKEN_FIELDS = {"input", "cache_read", "cache_write", "output", "reasoning"}
_EVENT_FIELDS = {
    "schema",
    "harness",
    "dedupe_key",
    "session_id",
    "request_id",
    "model_id",
    "occurred_at",
    "tokens",
    "status",
    "issues",
    "source_kind",
    "provider_name",
    "time_precision",
    "generation_milliseconds",
}


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _constant(value: str) -> Never:
    raise ValueError(f"Invalid JSON constant: {value}")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def loads(text: str) -> object:
    return json.loads(text, parse_constant=_constant, object_pairs_hook=_pairs)


def object_json(text: str) -> dict[str, object]:
    value = loads(text)
    if type(value) is not dict:
        raise ValueError("JSON object required")
    return cast(dict[str, object], value)


def encode_event(event: UsageEvent) -> str:
    return dumps({"schema": 1, **asdict(event)})


def decode_event(text: str) -> UsageEvent:
    data = object_json(text)
    data.setdefault("provider_name", None)
    data.setdefault("generation_milliseconds", None)
    if (
        set(data) != _EVENT_FIELDS
        or type(data["schema"]) is not int
        or data["schema"] != 1
        or data["time_precision"] != "request_completion"
    ):
        raise ValueError("Unsupported usage memento schema")
    for name in ("harness", "dedupe_key", "request_id", "occurred_at", "status"):
        if type(data[name]) is not str:
            raise ValueError("Invalid usage text")
    for name in ("session_id", "model_id", "source_kind", "provider_name"):
        if data[name] is not None and type(data[name]) is not str:
            raise ValueError("Invalid optional usage text")
    if type(data["tokens"]) is not dict:
        raise ValueError("Usage tokens object required")
    tokens = cast(dict[str, object], data["tokens"])
    if set(tokens) != _TOKEN_FIELDS or any(
        value is not None and type(value) is not int for value in tokens.values()
    ):
        raise ValueError("Invalid token memento")
    if type(data["issues"]) is not list:
        raise ValueError("Usage issues array required")
    issues = cast(list[object], data["issues"])
    if any(type(issue) is not str for issue in issues):
        raise ValueError("Invalid usage issues")
    return UsageEvent(
        Harness(cast(str, data["harness"])),
        cast(str, data["dedupe_key"]),
        cast(str | None, data["session_id"]),
        cast(str, data["request_id"]),
        cast(str | None, data["model_id"]),
        cast(str, data["occurred_at"]),
        Tokens(**cast(dict[str, int | None], tokens)),
        cast(UsageStatus, data["status"]),
        tuple(cast(str, issue) for issue in issues),
        cast(SourceKind | None, data["source_kind"]),
        cast(str | None, data["provider_name"]),
        cast(int | None, data["generation_milliseconds"]),
    )
