import json
from typing import cast

import pytest
from htd.domain.sources.codex import CodexContext, filter_diagnostics, parse_record
from htd.domain.usage.model import Diagnostic, Skip, UsageEvent


@pytest.mark.parametrize("info", [{}, {"total_token_usage": {"input_tokens": 100}}, {"unexpected": True}])
def test_snapshots_warn_before_any_request(
    info: dict[str, object], codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    snapshot = {"type": "event_msg", "payload": {"type": "token_count", "info": info}}
    warning = parse_record(snapshot, codex_context)
    assert isinstance(warning, Diagnostic)
    assert warning == Diagnostic("request_usage_format_unverified")
    assert not codex_context.saw_request_usage
    assert filter_diagnostics([warning], codex_context) == (warning,)
    assert isinstance(parse_record(codex_request, codex_context), UsageEvent)
    restored = CodexContext.from_dict(json.loads(json.dumps(codex_context.to_dict())))
    assert parse_record(snapshot, restored) == Skip()
    assert filter_diagnostics([warning], restored) == ()


@pytest.mark.parametrize("info", [None, [], "bad", True, 12])
def test_non_object_snapshot_info_never_warns(info: object, codex_context: CodexContext) -> None:
    assert (
        parse_record({"type": "event_msg", "payload": {"type": "token_count", "info": info}}, codex_context)
        == Skip()
    )


def test_invalid_request_still_proves_request_format(
    codex_context: CodexContext, codex_request: dict[str, object]
) -> None:
    cast(dict[str, object], codex_request["payload"]).pop("response_id")
    assert parse_record(codex_request, codex_context) == Diagnostic("missing_response_id")
    assert codex_context.saw_request_usage
    assert (
        parse_record({"type": "event_msg", "payload": {"type": "token_count", "info": {}}}, codex_context)
        == Skip()
    )


@pytest.mark.parametrize("state", ["empty", "rejected", "accepted-no-request", "accepted-request"])
def test_file_diagnostic_filter_is_ordered_and_non_mutating(
    state: str, codex_records: list[dict[str, object]]
) -> None:
    context = CodexContext()
    if state == "rejected":
        parse_record({"type": "session_meta", "payload": {}}, context)
    elif state.startswith("accepted"):
        parse_record(codex_records[0], context)
        if state == "accepted-request":
            parse_record(codex_records[2], context)
    diagnostics = [
        Diagnostic("invalid_json"),
        Diagnostic("desktop_source_unverified"),
        Diagnostic("source_rewritten"),
        Diagnostic("request_usage_format_unverified"),
        Diagnostic("missing_usage"),
        Diagnostic("invalid_json"),
    ]
    saved_diagnostics = list(diagnostics)
    saved_context = context.to_dict()
    indices = (0, 2, 3, 4, 5) if state == "accepted-no-request" else (0, 2, 4, 5)
    expected = tuple(diagnostics[index] for index in indices)
    assert filter_diagnostics(iter(diagnostics), context) == expected
    assert diagnostics == saved_diagnostics
    assert context.to_dict() == saved_context
