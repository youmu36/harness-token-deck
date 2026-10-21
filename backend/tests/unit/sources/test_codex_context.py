import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
from htd.domain.common.errors import SOURCE_CONTEXT_INVALID, DomainError
from htd.domain.sources.codex import CodexContext, filter_diagnostics, parse_record
from htd.domain.usage.model import Diagnostic, Skip, UsageEvent
from hypothesis import given
from hypothesis import strategies as st

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures/codex"


@pytest.mark.parametrize("fixture", ["desktop.jsonl", "repeated-snapshots.jsonl", "fork-subagent.jsonl"])
def test_restore_at_every_record_boundary(fixture: str) -> None:
    records: list[object] = [
        json.loads(line) for line in (FIXTURES / fixture).read_text("utf-8").splitlines()
    ]
    baseline = CodexContext()
    expected = [parse_record(record, baseline) for record in records]
    for split in range(len(records) + 1):
        original = CodexContext()
        actual = [parse_record(record, original) for record in records[:split]]
        restored = CodexContext.from_dict(json.loads(json.dumps(original.to_dict())))
        assert restored == original
        assert restored is not original
        actual.extend(parse_record(record, restored) for record in records[split:])
        assert actual == expected
        assert restored == baseline


def test_C02_request_uses_persisted_context(
    codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    # P3 owns buffering a partial line; only complete decoded records reach this parser.
    restored = CodexContext.from_dict(json.loads(json.dumps(codex_context.to_dict())))
    event = parse_record(codex_request, restored)
    assert isinstance(event, UsageEvent)
    assert event.model_id == "gpt-6-astra"
    assert not codex_context.saw_request_usage
    assert restored.saw_request_usage


def test_C05_bad_line_clears_model_after_reader_resets_rewritten_file(
    codex_records: list[dict[str, object]],
) -> None:
    # Anchor detection and source_rewritten originate in the P3 reader, not this parser.
    diagnostics = [Diagnostic("source_rewritten")]
    fresh = CodexContext()
    for record in codex_records[:3]:
        parse_record(record, fresh)
    bad = parse_record(None, fresh)
    assert isinstance(bad, Diagnostic)
    assert bad == Diagnostic("invalid_json")
    diagnostics.append(bad)
    assert fresh.model_id is None
    assert fresh.turn_id == "turn-1"
    restored = CodexContext.from_dict(json.loads(json.dumps(fresh.to_dict())))
    request = codex_records[2]
    cast(dict[str, object], request["payload"])["response_id"] = "new-response"
    event = parse_record(request, restored)
    assert isinstance(event, UsageEvent)
    assert event.model_id is None
    assert event.issues == ("model_unknown",)
    assert filter_diagnostics(diagnostics, restored) == (
        Diagnostic("source_rewritten"),
        Diagnostic("invalid_json"),
    )


@pytest.mark.parametrize("record", [None, [], 12, True, "not-an-object"])
def test_bad_records_clear_only_model(record: object, codex_context: CodexContext) -> None:
    before = codex_context.to_dict()
    assert parse_record(record, codex_context) == Diagnostic("invalid_json")
    before["modelId"] = None
    assert codex_context.to_dict() == before


def test_new_turn_context_restores_model_after_bad_line(
    codex_context: CodexContext, codex_records: list[dict[str, object]]
) -> None:
    parse_record(None, codex_context)
    parse_record(codex_records[1], codex_context)
    event = parse_record(codex_records[2], codex_context)
    assert isinstance(event, UsageEvent)
    assert event.model_id == "gpt-6-astra"


@pytest.mark.parametrize("payload", [{"originator": "cli", "source": "cli"}, None, {}, []])
def test_rejected_first_meta_stays_rejected_after_restart(
    payload: object, codex_records: list[dict[str, object]]
) -> None:
    context = CodexContext()
    assert parse_record({"type": "session_meta", "payload": payload}, context) == Diagnostic(
        "desktop_source_unverified"
    )
    context = CodexContext.from_dict(json.loads(json.dumps(context.to_dict())))
    for record in codex_records:
        assert parse_record(record, context) == Skip()
    assert not context.accepted
    assert context.model_id is None
    assert not context.saw_request_usage
    assert parse_record(None, context) == Diagnostic("invalid_json")
    assert filter_diagnostics(
        [Diagnostic("desktop_source_unverified"), Diagnostic("invalid_json")], context
    ) == (Diagnostic("invalid_json"),)


def test_later_meta_cannot_reset_model_identity_or_request_flag(
    codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    parse_record(codex_request, codex_context)
    saved = codex_context.to_dict()
    assert (
        parse_record(
            {"type": "session_meta", "payload": {"id": "parent", "originator": "cli"}}, codex_context
        )
        == Skip()
    )
    assert codex_context.to_dict() == saved


def test_normal_records_before_meta_are_skipped(codex_records: list[dict[str, object]]) -> None:
    context = CodexContext()
    for record in [*codex_records[1:], {}, {"type": "future-record"}]:
        assert parse_record(record, context) == Skip()
    assert context == CodexContext()
    assert parse_record(codex_records[0], context) == Skip()
    assert context.accepted


def test_unrelated_records_do_not_reset_model(codex_context: CodexContext) -> None:
    before = codex_context.to_dict()
    for record in [
        {},
        {"type": "future"},
        {"type": "event_msg", "payload": {"type": "interrupted"}},
        {"type": "event_msg", "payload": None},
    ]:
        assert parse_record(record, codex_context) == Skip()
    assert codex_context.to_dict() == before


@pytest.mark.parametrize("use_request_session", [False, True])
def test_raw_identity_survives_restore(use_request_session: bool, codex_request: dict[str, object]) -> None:
    context = CodexContext()
    raw_file_id = ' 会话"甲" '
    parse_record(
        {
            "type": "session_meta",
            "payload": {"id": raw_file_id, "originator": "Codex Desktop", "source": "vscode"},
        },
        context,
    )
    restored = CodexContext.from_dict(json.loads(json.dumps(context.to_dict())))
    payload = cast(dict[str, object], codex_request["payload"])
    raw_request_session = '\t会话"甲"\n'
    if use_request_session:
        payload["thread_id"] = raw_request_session
    else:
        payload.pop("thread_id")
    payload["response_id"] = " 响应\\乙 "
    result = parse_record(codex_request, restored)
    assert isinstance(result, UsageEvent)
    assert result.session_id == '会话"甲"'
    assert result.request_id == "响应\\乙"
    parts = [raw_request_session if use_request_session else raw_file_id, " 响应\\乙 "]
    expected = hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert result.dedupe_key == expected
    assert parse_record(codex_request, context) == result


@pytest.mark.parametrize("field", list(CodexContext().to_dict()))
def test_restore_rejects_missing_fields(field: str, codex_context: CodexContext) -> None:
    saved = codex_context.to_dict()
    saved.pop(field)
    if field == "providerName":
        assert CodexContext.from_dict(saved).provider_name is None
        return
    if field == "contextCompacted":
        assert not CodexContext.from_dict(saved).context_compacted
        return
    if field in {
        "requestStartedAt",
        "requestOutputAt",
        "lastTimedResponse",
        "lastGenerationMs",
        "requestTurnId",
        "requestOrigin",
        "requestUncertain",
        "taskActive",
        "taskContextSeen",
        "timingLatestAt",
        "lastUserAt",
        "lastUsageAt",
        "lastTimingUncertain",
        "streamStartedAt",
        "streamCompletedAt",
        "streamHasReasoning",
    }:
        CodexContext.from_dict(saved)
        return
    with pytest.raises(DomainError) as failure:
        CodexContext.from_dict(saved)
    assert failure.value.code == SOURCE_CONTEXT_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sawSessionMeta", 1),
        ("originatorOk", "true"),
        ("sawRequestUsage", None),
        ("fileSessionId", ""),
        ("fileSessionId", "different"),
        ("modelId", []),
        ("modelId", " model "),
        ("turnId", True),
        ("turnId", "x" * 513),
        ("rawFileSessionId", None),
        ("rawFileSessionId", 12),
        ("rawFileSessionId", "other"),
        ("rawFileSessionId", "\ud800"),
        ("kind", "unknown"),
        ("kind", []),
        ("requestTurnId", []),
        ("requestOrigin", "unknown"),
        ("requestOrigin", []),
        ("requestUncertain", 1),
        ("taskActive", "true"),
        ("taskContextSeen", None),
        ("lastTimingUncertain", 1),
        ("timingLatestAt", "invalid"),
        ("lastUserAt", []),
        ("lastUsageAt", 42),
        ("originatorOk", False),
        ("sawSessionMeta", False),
    ],
)
def test_restore_rejects_corrupt_state(field: str, value: object, codex_context: CodexContext) -> None:
    saved = codex_context.to_dict()
    saved[field] = value
    with pytest.raises(DomainError) as failure:
        CodexContext.from_dict(saved)
    assert failure.value.code == SOURCE_CONTEXT_INVALID


@pytest.mark.parametrize("value", [None, [], "bad"])
def test_restore_rejects_non_object(value: object) -> None:
    with pytest.raises(DomainError) as failure:
        CodexContext.from_dict(cast(dict[str, object], value))
    assert failure.value.code == SOURCE_CONTEXT_INVALID


def test_rejected_context_cannot_claim_request_usage() -> None:
    saved = CodexContext().to_dict()
    saved.update(sawSessionMeta=True, sawRequestUsage=True)
    with pytest.raises(DomainError) as failure:
        CodexContext.from_dict(saved)
    assert failure.value.code == SOURCE_CONTEXT_INVALID


JSON_VALUES: st.SearchStrategy[object] = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=30),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4), st.dictionaries(st.text(max_size=20), children, max_size=4)
    ),
    max_leaves=12,
)


@given(
    st.lists(
        st.one_of(
            JSON_VALUES,
            st.fixed_dictionaries(
                {
                    "type": st.sampled_from(
                        ["session_meta", "turn_context", "token_usage_record", "event_msg"]
                    ),
                    "payload": JSON_VALUES,
                    "timestamp": JSON_VALUES,
                }
            ),
        ),
        max_size=20,
    )
)
def test_arbitrary_json_returns_results_and_checkpoint_roundtrips(records: list[object]) -> None:
    context = CodexContext()
    parse_record(
        {"type": "session_meta", "payload": {"id": "s", "originator": "Codex Desktop", "source": "vscode"}},
        context,
    )
    for record in records:
        result = parse_record(record, context)
        assert isinstance(result, (UsageEvent, Skip, Diagnostic))
        restored = CodexContext.from_dict(json.loads(json.dumps(context.to_dict())))
        assert restored == context
