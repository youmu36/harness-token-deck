import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
from htu.domain.sources.codex import CodexContext, classify_source, filter_diagnostics, parse_record
from htu.domain.usage.model import Diagnostic, Harness, Skip, Tokens, UsageEvent

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures/codex"
SOURCE_CASES = cast(list[dict[str, object]], json.loads((FIXTURES / "source-cases.json").read_text("utf-8")))


def payload_of(record: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], record["payload"])


@pytest.mark.parametrize(
    "marker", [{"type": "compacted"}, {"type": "event_msg", "payload": {"type": "context_compacted"}}]
)
def test_explicit_compaction_applies_once_and_survives_checkpoint(
    marker: dict[str, object], codex_request: dict[str, object]
) -> None:
    ctx = CodexContext(
        file_session_id="synthetic-desktop",
        raw_file_session_id="synthetic-desktop",
        saw_session_meta=True,
        originator_ok=True,
        kind="desktop",
    )
    # Use the actual fixture identity so request validation stays independent of the marker.
    payload = payload_of(codex_request)
    ctx.file_session_id = ctx.raw_file_session_id = cast(str, payload.get("thread_id", "synthetic-desktop"))
    parse_record(marker, ctx)
    restored = CodexContext.from_dict(ctx.to_dict())
    event = parse_record(codex_request, restored)
    assert isinstance(event, UsageEvent)
    assert "context_compacted" in event.issues
    following = parse_record(codex_request, restored)
    assert isinstance(following, UsageEvent)
    assert "context_compacted" not in following.issues


@pytest.mark.parametrize("case", SOURCE_CASES, ids=[str(case["name"]) for case in SOURCE_CASES])
def test_source_cases(case: dict[str, object], codex_request: dict[str, object]) -> None:
    context = CodexContext()
    meta = {
        "type": "session_meta",
        "payload": {
            "id": "synthetic-desktop",
            "originator": case.get("originator"),
            "source": case.get("source"),
        },
    }
    classified = classify_source(case.get("originator"), case.get("source"))
    assert classified == case.get("sourceKind")
    first = parse_record(meta, context)
    assert context.accepted == case["accepted"]
    assert context.kind == classified
    outcome = parse_record(codex_request, context)
    if case["accepted"]:
        assert first == Skip()
        assert isinstance(outcome, UsageEvent)
        assert outcome.source_kind == case["sourceKind"]
    else:
        assert isinstance(first, Diagnostic)
        assert first == Diagnostic("desktop_source_unverified")
        assert outcome == Skip()
        assert filter_diagnostics([first], context) == ()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (None, None),
        ([], None),
        (True, None),
        ({}, None),
        ({"subagent": "thread_spawn"}, None),
        ({"subagent": []}, None),
        ({"subagent": {}}, None),
        ({"subagent": {"other": {}}}, None),
        ({"subagent.thread_spawn": {}}, None),
        ({"subagent": {"thread_spawn": {}}}, "desktop_subagent"),
        ({"subagent": {"thread_spawn": None}}, "desktop_subagent"),
        ("VSCODE", None),
        (" vscode ", None),
    ],
)
def test_source_classification_uses_exact_structure(source: object, expected: object) -> None:
    assert classify_source("Codex Desktop", source) == expected
    assert classify_source("codex_work_desktop", source) == expected
    assert classify_source("codex_vscode", source) is None


def test_desktop_fixture_request_fields() -> None:
    context = CodexContext()
    outcomes = [
        parse_record(json.loads(line), context)
        for line in (FIXTURES / "desktop.jsonl").read_text("utf-8").splitlines()
    ]
    event = outcomes[2]
    assert isinstance(event, UsageEvent)
    assert event.harness == Harness.CODEX
    assert (event.session_id, event.request_id) == ("synthetic-desktop", "response-1")
    assert event.model_id == "gpt-6-astra"
    assert event.tokens == Tokens(input=100, cache_read=40, cache_write=0, output=10, reasoning=0)
    assert event.occurred_at == "2026-09-01T00:00:01.000Z"
    assert event.time_precision == "request_completion"
    assert event.status == "reported"
    assert event.source_kind == "desktop"
    assert event.issues == ()
    assert event.dedupe_key == hashlib.sha256(b'["synthetic-desktop","response-1"]').hexdigest()
    assert [result for result in outcomes if not isinstance(result, UsageEvent)] == [Skip(), Skip(), Skip()]


def test_C04_repeated_snapshots_produce_only_one_event() -> None:
    context = CodexContext()
    outcomes = [
        parse_record(json.loads(line), context)
        for line in (FIXTURES / "repeated-snapshots.jsonl").read_text("utf-8").splitlines()
    ]
    events = [result for result in outcomes if isinstance(result, UsageEvent)]
    assert len(events) == 1
    assert events[0].tokens == Tokens(100, 40, 0, 10, 0)
    assert outcomes[-2:] == [Skip(), Skip()]


def test_C14_fork_keeps_child_identity_and_source_kind() -> None:
    context = CodexContext()
    outcomes = [
        parse_record(json.loads(line), context)
        for line in (FIXTURES / "fork-subagent.jsonl").read_text("utf-8").splitlines()
    ]
    assert outcomes[:3] == [Skip(), Skip(), Skip()]
    event = outcomes[3]
    assert isinstance(event, UsageEvent)
    assert event.session_id == context.file_session_id == "synthetic-child"
    assert event.source_kind == context.kind == "desktop_subagent"
    assert event.model_id == "gpt-6-astra"
    assert event.dedupe_key == hashlib.sha256(b'["synthetic-child","child-response-1"]').hexdigest()


@pytest.mark.parametrize("thread", [None, "", " ", 1, True, "x" * 513, "\ud800"])
def test_missing_thread_falls_back_to_file_session(
    thread: object, codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    payload_of(codex_request)["thread_id"] = thread
    event = parse_record(codex_request, codex_context)
    assert isinstance(event, UsageEvent)
    assert event.session_id == "synthetic-desktop"


def test_missing_file_id_can_use_request_thread(codex_request: dict[str, object]) -> None:
    context = CodexContext()
    parse_record(
        {"type": "session_meta", "payload": {"originator": "Codex Desktop", "source": "vscode"}}, context
    )
    event = parse_record(codex_request, context)
    assert isinstance(event, UsageEvent)
    assert event.session_id == "synthetic-desktop"
    payload_of(codex_request).pop("thread_id")
    assert parse_record(codex_request, context) == Diagnostic("missing_session_id")


def test_mismatched_session_is_rejected(
    codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    payload_of(codex_request)["thread_id"] = "another-session"
    assert parse_record(codex_request, codex_context) == Diagnostic("session_identity_mismatch")


@pytest.mark.parametrize("field", ["response_id", "usage"])
@pytest.mark.parametrize("value", [None, "", " ", True, 12, [], "x" * 513])
def test_required_request_fields(
    field: str, value: object, codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    payload_of(codex_request)[field] = value
    assert parse_record(codex_request, codex_context) == Diagnostic(
        "missing_response_id" if field == "response_id" else "missing_usage"
    )
    payload_of(codex_request).pop(field)
    assert parse_record(codex_request, codex_context) == Diagnostic(
        "missing_response_id" if field == "response_id" else "missing_usage"
    )


@pytest.mark.parametrize("value", [None, "", " ", 1, "wrong-turn", "x" * 513])
def test_request_turn_must_match_context(
    value: object, codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    payload_of(codex_request)["turn_id"] = value
    event = parse_record(codex_request, codex_context)
    assert isinstance(event, UsageEvent)
    assert event.model_id is None
    assert event.issues == ("model_unknown",)
    payload_of(codex_request).pop("turn_id")
    assert parse_record(codex_request, codex_context) == event


@pytest.mark.parametrize("payload", [{}, {"model": "other"}, {"turn_id": "turn-1"}, None, [], "bad"])
def test_turn_context_replaces_both_fields(
    payload: object, codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    assert parse_record({"type": "turn_context", "payload": payload}, codex_context) == Skip()
    event = parse_record(codex_request, codex_context)
    assert isinstance(event, UsageEvent)
    assert event.model_id is None
    assert "model_unknown" in event.issues


def test_turn_text_uses_optional_text(codex_context: CodexContext, codex_request: dict[str, object]) -> None:
    parse_record(
        {"type": "turn_context", "payload": {"model": " MODEL A ", "turn_id": " turn-1 "}}, codex_context
    )
    payload_of(codex_request)["turn_id"] = " turn-1 "
    event = parse_record(codex_request, codex_context)
    assert isinstance(event, UsageEvent)
    assert event.model_id == "MODEL A"


@pytest.mark.parametrize(
    "timestamp", [None, "", "2026-09-01T00:00:00", "2026-02-29T00:00:00Z", 1, True, {}, []]
)
def test_invalid_timestamps_are_diagnostics(
    timestamp: object, codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    codex_request["timestamp"] = timestamp
    expected = "missing_timestamp" if timestamp is None else "invalid_timestamp"
    assert parse_record(codex_request, codex_context) == Diagnostic(expected)
    codex_request.pop("timestamp")
    assert parse_record(codex_request, codex_context) == Diagnostic("missing_timestamp")


def test_completion_time_is_normalized(codex_context: CodexContext, codex_request: dict[str, object]) -> None:
    codex_request["timestamp"] = "2026-09-01T08:00:01.1239+08:00"
    payload_of(codex_request)["timestamp"] = "ignored"
    event = parse_record(codex_request, codex_context)
    assert isinstance(event, UsageEvent)
    assert event.occurred_at == "2026-09-01T00:00:01.123Z"


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


@pytest.mark.parametrize("field", TOKEN_FIELDS)
@pytest.mark.parametrize("value", [12.0, True, False, "12", -1, 2**53, {}, []])
def test_invalid_tokens_become_diagnostics(
    field: str, value: object, codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    cast(dict[str, object], payload_of(codex_request)["usage"])[field] = value
    assert parse_record(codex_request, codex_context) == Diagnostic("invalid_token_count")


@pytest.mark.parametrize(
    ("usage", "code"),
    [
        ({"input_tokens": 10, "cached_input_tokens": 11}, "cache_exceeds_input"),
        ({"input_tokens": 10, "cache_write_input_tokens": 11}, "cache_exceeds_input"),
        (
            {"input_tokens": 10, "cached_input_tokens": 6, "cache_write_input_tokens": 5},
            "cache_exceeds_input",
        ),
        ({"output_tokens": 10, "reasoning_output_tokens": 11}, "reasoning_exceeds_output"),
    ],
)
def test_invalid_token_relations_become_diagnostics(
    usage: dict[str, int], code: str, codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    payload_of(codex_request)["usage"] = usage
    assert parse_record(codex_request, codex_context) == Diagnostic(code)


def test_usage_unknown_fields_and_quality_order(
    codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    payload = payload_of(codex_request)
    payload["usage"] = {field: None for field in TOKEN_FIELDS}
    payload.pop("turn_id")
    unknown = parse_record(codex_request, codex_context)
    assert isinstance(unknown, UsageEvent)
    assert unknown.tokens == Tokens()
    assert unknown.issues == ("model_unknown", "usage_incomplete", "cache_unknown", "cache_write_unknown")
    payload["usage"] = {"total_tokens": -1, "future_field": {"input_tokens": "bad"}}
    assert parse_record(codex_request, codex_context) == unknown
    payload["usage"] = {"cache_write_input_tokens": 1}
    writing = parse_record(codex_request, codex_context)
    assert isinstance(writing, UsageEvent)
    assert writing.issues == (
        "model_unknown",
        "usage_incomplete",
        "cache_unknown",
    )


def test_safe_integer_boundary_and_cache_sum(
    codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    payload_of(codex_request)["usage"] = {
        "input_tokens": 2**53 - 1,
        "cached_input_tokens": 2**53 - 2,
        "cache_write_input_tokens": 1,
        "output_tokens": 2**53 - 1,
        "reasoning_output_tokens": 2**53 - 1,
    }
    event = parse_record(codex_request, codex_context)
    assert isinstance(event, UsageEvent)
    assert event.tokens == Tokens(2**53 - 1, 2**53 - 2, 1, 2**53 - 1, 2**53 - 1)
    assert event.issues == ()


def test_invalid_model_key_returns_diagnostic(
    codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    parse_record({"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "\ufeff"}}, codex_context)
    assert parse_record(codex_request, codex_context) == Diagnostic("model_name_invalid")


@pytest.mark.parametrize("payload", [None, [], True, "bad", {}])
def test_bad_request_payload_is_missing_response(
    payload: object, codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    codex_request["payload"] = payload
    assert parse_record(codex_request, codex_context) == Diagnostic("missing_response_id")


def test_parsing_does_not_mutate_record(
    codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    before = json.dumps(codex_request)
    first = parse_record(codex_request, codex_context)
    assert json.dumps(codex_request) == before
    assert parse_record(codex_request, codex_context) == first  # Deduplication belongs to persistence.
