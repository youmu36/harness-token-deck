import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import cast

import pytest
from htu.domain.common.time import from_epoch_ms
from htu.domain.sources.dsh import DshContext, parse_record
from htu.domain.usage.model import Diagnostic, Harness, Skip, Tokens, UsageEvent


@pytest.fixture
def records() -> list[dict[str, object]]:
    path = Path(__file__).resolve().parents[4] / "fixtures/dsh/session.jsonl"
    return [cast(dict[str, object], json.loads(line)) for line in path.read_text("utf-8").splitlines()]


@pytest.fixture
def context(records: list[dict[str, object]]) -> DshContext:
    context = DshContext()
    for record in records[:2]:
        parse_record(record, context)
    return context


@pytest.fixture
def message(records: list[dict[str, object]]) -> dict[str, object]:
    return records[3]


def data_of(message: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], message["data"])


def test_C12_fixture_totals_and_repeatable_replay(records: list[dict[str, object]]) -> None:
    context = DshContext()
    outcomes = [parse_record(record, context) for record in records]
    events = [event for event in outcomes if isinstance(event, UsageEvent)]
    assert len(events) == 2
    assert [event for event in outcomes if not isinstance(event, UsageEvent)] == [Skip()] * 4
    assert sum(event.tokens.input or 0 for event in events) == 21933
    assert sum(event.tokens.cache_read or 0 for event in events) == 10112
    assert sum(event.tokens.output or 0 for event in events) == 1379
    assert {event.model_id for event in events} == {"deepseek-v4-flash"}
    assert events[0].harness == Harness.DSH
    assert events[0].tokens == Tokens(9838, 0, 0, 374, 117)
    assert events[0].occurred_at == "2026-08-13T13:56:34.116Z"
    assert events[0].request_id == "1:1"
    assert events[0].status == "reported"
    assert events[0].source_kind is None
    assert events[0].issues == ()
    assert events[0].dedupe_key == hashlib.sha256(b'["synthetic-dsh",1,1]').hexdigest()
    fresh = DshContext()
    assert [parse_record(record, fresh) for record in records] == outcomes


def test_C13_derived_second_request_conversion(records: list[dict[str, object]]) -> None:
    # The fixture README specifies inputTokens=200 for C-13; zstd frames belong to P3.
    cast(dict[str, object], data_of(records[-1])["usage"])["inputTokens"] = 200
    context = DshContext()
    outcomes = [parse_record(record, context) for record in records]
    event = outcomes[-1]
    assert isinstance(event, UsageEvent)
    assert event.tokens.input == 10312
    assert event.tokens.cache_read == 10112


@pytest.mark.parametrize(
    ("uncached", "cached", "expected"),
    [
        (9838, 1024, 10862),
        (0, 50, 50),
        (0, 0, 0),
        (10, None, 10),
        (None, 10, None),
        (None, None, None),
    ],
)
def test_input_conversion_and_unknown_semantics(
    context: DshContext,
    message: dict[str, object],
    uncached: int | None,
    cached: int | None,
    expected: int | None,
) -> None:
    data_of(message)["usage"] = {"inputTokens": uncached, "cacheReadTokens": cached}
    event = parse_record(message, context)
    assert isinstance(event, UsageEvent)
    assert event.tokens == Tokens(expected, cached, 0, None, None)
    assert ("cache_unknown" in event.issues) == (cached is None)
    assert "cache_write_unknown" not in event.issues


@pytest.mark.parametrize("field", ["inputTokens", "cacheReadTokens", "outputTokens", "reasoningTokens"])
@pytest.mark.parametrize("value", [-1, True, False, 1.0, "1", 2**53, [], {}])
def test_validate_raw_counts_before_adding(
    context: DshContext, message: dict[str, object], field: str, value: object
) -> None:
    data_of(message)["usage"] = {"inputTokens": 100, "cacheReadTokens": 10, "outputTokens": 10, field: value}
    assert parse_record(message, context) == Diagnostic("invalid_token_count")


def test_combined_input_must_fit_safe_integer(context: DshContext, message: dict[str, object]) -> None:
    usage = {"inputTokens": 2**53 - 2, "cacheReadTokens": 1}
    data_of(message)["usage"] = usage
    event = parse_record(message, context)
    assert isinstance(event, UsageEvent)
    assert event.tokens.input == 2**53 - 1
    usage["cacheReadTokens"] = 2
    assert parse_record(message, context) == Diagnostic("invalid_token_count")


def test_reasoning_cannot_exceed_output(context: DshContext, message: dict[str, object]) -> None:
    data_of(message)["usage"] = {"outputTokens": 10, "reasoningTokens": 11}
    assert parse_record(message, context) == Diagnostic("reasoning_exceeds_output")


@pytest.mark.parametrize("usage", [None, [], True, 0, "{}"])
def test_non_object_usage_skips_even_without_session(usage: object) -> None:
    assert parse_record({"type": "assistant/message", "data": {"usage": usage}}, DshContext()) == Skip()


@pytest.mark.parametrize("data", [None, [], True, 12, {}, "bad"])
def test_missing_usage_skips(data: object) -> None:
    assert parse_record({"type": "assistant/message", "data": data}, DshContext()) == Skip()


def test_required_fields_in_order() -> None:
    context = DshContext()
    data: dict[str, object] = {"usage": {}}
    message: dict[str, object] = {"type": "assistant/message", "data": data}
    assert parse_record(message, context) == Diagnostic("missing_session_id")
    parse_record({"type": "session", "id": "s"}, context)
    assert parse_record(message, context) == Diagnostic("missing_turn")
    data["turn"] = 1
    assert parse_record(message, context) == Diagnostic("missing_step")
    data["step"] = 2
    assert parse_record(message, context) == Diagnostic("missing_timestamp")
    message["time"] = 0
    event = parse_record(message, context)
    assert isinstance(event, UsageEvent)
    assert event.tokens == Tokens(cache_write=0)
    assert event.issues == ("model_unknown", "usage_incomplete", "cache_unknown")


@pytest.mark.parametrize(
    ("field", "code"), [("turn", "missing_turn"), ("step", "missing_step"), ("time", "missing_timestamp")]
)
@pytest.mark.parametrize("value", [None, True, False, 1.0, "1", [], {}])
def test_required_integer_fields(
    context: DshContext, message: dict[str, object], field: str, code: str, value: object
) -> None:
    target = message if field == "time" else data_of(message)
    target[field] = value
    assert parse_record(message, context) == Diagnostic(code)
    target.pop(field)
    assert parse_record(message, context) == Diagnostic(code)


@pytest.mark.parametrize("timestamp", [-62135596800001, 253402300800000])
def test_out_of_range_epoch(context: DshContext, message: dict[str, object], timestamp: int) -> None:
    message["time"] = timestamp
    assert parse_record(message, context) == Diagnostic("invalid_timestamp")


def test_integer_identity_preserves_zero_and_negative_values(
    context: DshContext, message: dict[str, object]
) -> None:
    message["time"] = -1
    data_of(message).update(turn=0, step=-1)
    event = parse_record(message, context)
    assert isinstance(event, UsageEvent)
    assert event.request_id == "0:-1"
    assert event.occurred_at == "1969-12-31T23:59:59.999Z"
    assert event.dedupe_key == hashlib.sha256(b'["synthetic-dsh",0,-1]').hexdigest()


@pytest.mark.parametrize("invalid", [None, "", " ", True, [], "x" * 513, "\ud800"])
def test_session_record_replaces_old_identity(
    context: DshContext, message: dict[str, object], invalid: object
) -> None:
    parse_record({"type": "session", "id": invalid}, context)
    assert context.session_id is None
    assert context.raw_session_id is None
    assert parse_record(message, context) == Diagnostic("missing_session_id")


def test_session_hash_uses_raw_text(context: DshContext, message: dict[str, object]) -> None:
    raw_id = ' 会话"甲" '
    parse_record({"type": "session", "id": raw_id}, context)
    event = parse_record(message, context)
    assert isinstance(event, UsageEvent)
    assert event.session_id == '会话"甲"'
    expected = json.dumps([raw_id, 1, 1], ensure_ascii=False, separators=(",", ":"))
    assert event.dedupe_key == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    data_of(message)["step"] = 2
    second = parse_record(message, context)
    assert isinstance(second, UsageEvent)
    assert second.dedupe_key != event.dedupe_key


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [],
        {"header": None},
        {"header": {"config": []}},
        {"header": {"config": {}}},
        {"header": {"config": {"model": " "}}},
    ],
)
def test_invalid_header_clears_model(context: DshContext, message: dict[str, object], data: object) -> None:
    assert parse_record({"type": "request/header", "data": data}, context) == Skip()
    event = parse_record(message, context)
    assert isinstance(event, UsageEvent)
    assert event.model_id is None
    assert event.issues == ("model_unknown",)


@pytest.mark.parametrize("record", [None, [], True, 0, "bad"])
def test_bad_line_clears_only_model(context: DshContext, message: dict[str, object], record: object) -> None:
    assert parse_record(record, context) == Diagnostic("invalid_json")
    assert context.session_id == "synthetic-dsh"
    event = parse_record(message, context)
    assert isinstance(event, UsageEvent)
    assert event.model_id is None
    parse_record(
        {"type": "request/header", "data": {"header": {"config": {"model": " NEW MODEL "}}}}, context
    )
    updated = parse_record(message, context)
    assert isinstance(updated, UsageEvent)
    assert updated.model_id == "NEW MODEL"


def test_model_empty_key_is_diagnostic(context: DshContext, message: dict[str, object]) -> None:
    parse_record({"type": "request/header", "data": {"header": {"config": {"model": "\ufeff"}}}}, context)
    assert parse_record(message, context) == Diagnostic("model_name_invalid")


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_start_boundary(context: DshContext, message: dict[str, object], offset: int) -> None:
    started_at = from_epoch_ms(cast(int, message["time"]) + offset)
    event = parse_record(message, context, started_at)
    assert (event == Skip()) if offset > 0 else isinstance(event, UsageEvent)
    assert context.model_id == "deepseek-v4-flash"


def test_old_invalid_event_still_reports_diagnostic(context: DshContext, message: dict[str, object]) -> None:
    data_of(message)["usage"] = {"inputTokens": -1}
    assert parse_record(message, context, from_epoch_ms(1788220800000)) == Diagnostic("invalid_token_count")


def test_whitelist_does_not_retain_content_or_unknown_token_fields(
    context: DshContext, message: dict[str, object]
) -> None:
    original = parse_record(message, context)
    data_of(message)["body"] = {"system": "PRIVATE PROMPT", "reasoning": "PRIVATE REASONING"}
    usage = cast(dict[str, object], data_of(message)["usage"])
    usage.update(cacheWriteTokens=-10, totalTokens="invalid but ignored", secret="PRIVATE SECRET")
    before = json.dumps(message)
    event = parse_record(message, context)
    assert event == original
    assert isinstance(event, UsageEvent)
    assert "PRIVATE" not in repr(asdict(context)) + repr(asdict(event))
    assert json.dumps(message) == before
    prior = asdict(context)
    assert parse_record({"type": "unknown", "data": {"model": "ignored"}}, context) == Skip()
    assert asdict(context) == prior
