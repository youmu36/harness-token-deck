import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from htd.domain.sources.zcode import ZcodeRow, classify_status, normalize_row
from htd.domain.usage.model import Diagnostic, Harness, Tokens, UsageEvent

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures/zcode"


@pytest.fixture
def row() -> ZcodeRow:
    return ZcodeRow(
        rowid=1,
        id="row-1",
        session_id="s",
        logical_request_id="r",
        model_id="MODEL A",
        status="completed",
        completed_at=1788220801000,
        raw_usage_json=(FIXTURES / "usage.json").read_text("utf-8"),
    )


@pytest.mark.parametrize(
    "status", ["running", "completed", "error", "cancelled", "retry", "future", None, [], "COMPLETED"]
)
@pytest.mark.parametrize("completed_at", [None, 0, "invalid-time"])
def test_status_classification(status: object, completed_at: object) -> None:
    expected = (
        "pending"
        if status == "running" or completed_at is None
        else ("terminal" if status in ("completed", "error", "cancelled") else "unsupported")
    )
    assert classify_status(status, completed_at) == expected


@pytest.mark.parametrize("status", ["completed", "error", "cancelled"])
def test_terminal_events_keep_status(row: ZcodeRow, status: str) -> None:
    event = normalize_row(replace(row, status=status))
    assert isinstance(event, UsageEvent)
    assert event.harness == Harness.ZCODE
    assert (event.session_id, event.request_id, event.model_id) == ("s", "r", "MODEL A")
    assert event.tokens == Tokens(100, 40, 0, 10, None)
    assert event.status == status
    assert event.source_kind is None
    assert event.occurred_at == "2026-09-01T00:00:01.000Z"
    assert event.dedupe_key == hashlib.sha256(b'["s","r",0]').hexdigest()


def test_native_timing_excludes_first_token_wait_and_keeps_reasoning_in_output(row: ZcodeRow) -> None:
    end = row.completed_at
    assert type(end) is int
    result = normalize_row(
        replace(
            row,
            started_at=end - 60_000,
            first_token_at=end - 40_000,
            raw_usage_json='{"outputTokens":1400,"reasoningTokens":800}',
        )
    )
    assert isinstance(result, UsageEvent)
    assert result.generation_milliseconds == 40_000
    assert result.tokens.output == 1400 and result.tokens.reasoning == 800


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"duration_ms": 60_000, "time_to_first_token_ms": 20_000}, 40_000),
        ({"first_token_at": True}, None),
        ({"first_token_at": 0}, None),
        ({"started_at": -1, "first_token_at": 1788220800000}, None),
        ({"first_token_at": "1788220800000"}, None),
        ({"first_token_at": 1788220801001}, None),
        ({"started_at": 1788220800000, "first_token_at": 1788220799999}, None),
        ({"duration_ms": 20_000, "time_to_first_token_ms": 20_000}, None),
        ({"duration_ms": 20_000, "time_to_first_token_ms": -1}, None),
        ({"duration_ms": True, "time_to_first_token_ms": 0}, None),
        ({"started_at": 1788220800000, "duration_ms": 60_000, "time_to_first_token_ms": 1}, None),
    ],
)
def test_timing_is_optional_and_invalid_metrics_never_block_usage(
    row: ZcodeRow, fields: dict[str, object], expected: int | None
) -> None:
    result = normalize_row(replace(row, **fields))
    assert isinstance(result, UsageEvent) and result.generation_milliseconds == expected
    assert result.tokens == Tokens(100, 40, 0, 10, None)


def test_C11_native_column_fallback(row: ZcodeRow) -> None:
    event = normalize_row(
        replace(
            row,
            raw_usage_json=None,
            input_tokens=80,
            output_tokens=7,
            reasoning_tokens=2,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=30,
        )
    )
    assert isinstance(event, UsageEvent)
    assert event.tokens == Tokens(80, 30, 0, 7, 2)
    assert event.issues == ()


FIELDS = [
    ("inputTokens", "input_tokens", "input"),
    ("outputTokens", "output_tokens", "output"),
    ("cacheReadTokens", "cache_read_input_tokens", "cache_read"),
    ("cacheWriteTokens", "cache_creation_input_tokens", "cache_write"),
    ("reasoningTokens", "reasoning_tokens", "reasoning"),
]


@pytest.mark.parametrize(("key", "native", "token"), FIELDS)
@pytest.mark.parametrize("raw", [None, "{}", "null-key"])
def test_missing_or_null_json_falls_back_per_field(
    row: ZcodeRow, key: str, native: str, token: str, raw: str | None
) -> None:
    event = normalize_row(
        replace(row, raw_usage_json=json.dumps({key: None}) if raw == "null-key" else raw, **{native: 7})
    )
    assert isinstance(event, UsageEvent)
    assert getattr(event.tokens, token) == 7


@pytest.mark.parametrize(("key", "native", "token"), FIELDS)
def test_explicit_zero_overrides_invalid_native(row: ZcodeRow, key: str, native: str, token: str) -> None:
    event = normalize_row(replace(row, raw_usage_json=json.dumps({key: 0}), **{native: "bad"}))
    assert isinstance(event, UsageEvent)
    assert getattr(event.tokens, token) == 0


@pytest.mark.parametrize(("key", "native", "token"), FIELDS)
@pytest.mark.parametrize("value", [True, False, 12.0, "12", -1, 2**53, []])
def test_invalid_raw_or_native_counts_are_not_coerced(
    row: ZcodeRow, key: str, native: str, token: str, value: object
) -> None:
    assert normalize_row(replace(row, raw_usage_json=json.dumps({key: value}), **{native: 0})) == Diagnostic(
        "invalid_token_count"
    )
    assert normalize_row(replace(row, raw_usage_json=None, **{native: value})) == Diagnostic(
        "invalid_token_count"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        "null",
        "[]",
        '"text"',
        "12",
        "true",
        '{"inputTokens":NaN}',
        '{"extra":Infinity}',
        12,
        True,
        {},
        b"{}",
    ],
)
def test_invalid_usage_json(row: ZcodeRow, raw: object) -> None:
    assert normalize_row(replace(row, raw_usage_json=raw, input_tokens=80)) == Diagnostic(
        "invalid_usage_json"
    )


def test_null_raw_and_absent_native_mean_unknown(row: ZcodeRow) -> None:
    event = normalize_row(replace(row, raw_usage_json=None, model_id=None, status="cancelled"))
    assert isinstance(event, UsageEvent)
    assert event.tokens == Tokens()
    assert event.issues == ("model_unknown", "usage_incomplete", "cache_unknown", "cache_write_unknown")
    assert (
        normalize_row(
            replace(row, raw_usage_json='{"totalTokens":"ignored"}', model_id=None, status="cancelled")
        )
        == event
    )


@pytest.mark.parametrize(
    ("field", "code"), [("session_id", "missing_session_id"), ("logical_request_id", "missing_request_id")]
)
@pytest.mark.parametrize("value", [None, "", " ", True, 12, "x" * 513, "\ud800"])
def test_required_identity_text(row: ZcodeRow, field: str, code: str, value: object) -> None:
    assert normalize_row(replace(row, **{field: value})) == Diagnostic(code)


@pytest.mark.parametrize("attempt", [None, True, -1, 1.0, "1", []])
def test_invalid_attempt(row: ZcodeRow, attempt: object) -> None:
    assert normalize_row(replace(row, attempt_index=attempt)) == Diagnostic("invalid_attempt_index")


def test_identity_uses_raw_text_and_attempt_but_not_row_location(row: ZcodeRow) -> None:
    row = replace(row, session_id=' 会话"甲" ', logical_request_id=" 请求 ", attempt_index=1)
    event = normalize_row(row)
    assert isinstance(event, UsageEvent)
    expected = json.dumps([' 会话"甲" ', " 请求 ", 1], ensure_ascii=False, separators=(",", ":"))
    assert event.dedupe_key == hashlib.sha256(expected.encode("utf-8")).hexdigest()
    assert (event.session_id, event.request_id) == ('会话"甲"', "请求")
    assert normalize_row(replace(row, rowid=2, id="new-row")) == event
    other_attempt = normalize_row(replace(row, attempt_index=2))
    assert isinstance(other_attempt, UsageEvent)
    assert other_attempt.dedupe_key != event.dedupe_key


@pytest.mark.parametrize("timestamp", [None, True, 1.0, "0", -62135596800001, 253402300800000])
def test_invalid_completion_time(row: ZcodeRow, timestamp: object) -> None:
    assert normalize_row(replace(row, completed_at=timestamp)) == Diagnostic("invalid_timestamp")


def test_negative_epoch_is_valid(row: ZcodeRow) -> None:
    event = normalize_row(replace(row, completed_at=-1))
    assert isinstance(event, UsageEvent)
    assert event.occurred_at == "1969-12-31T23:59:59.999Z"


@pytest.mark.parametrize(
    ("usage", "code"),
    [
        ({"inputTokens": 10, "cacheReadTokens": 11}, "cache_exceeds_input"),
        ({"inputTokens": 10, "cacheWriteTokens": 11}, "cache_exceeds_input"),
        ({"inputTokens": 10, "cacheReadTokens": 6, "cacheWriteTokens": 5}, "cache_exceeds_input"),
        ({"outputTokens": 1, "reasoningTokens": 2}, "reasoning_exceeds_output"),
    ],
)
def test_token_relations(row: ZcodeRow, usage: dict[str, int], code: str) -> None:
    assert normalize_row(replace(row, raw_usage_json=json.dumps(usage))) == Diagnostic(code)


def test_model_name_normalization_and_empty_key(row: ZcodeRow) -> None:
    event = normalize_row(replace(row, model_id=" MODEL A "))
    assert isinstance(event, UsageEvent)
    assert event.model_id == "MODEL A"
    assert normalize_row(replace(row, model_id="\ufeff")) == Diagnostic("model_name_invalid")
    assert normalize_row(replace(row, status="running")) == Diagnostic("source_status_unsupported")
