from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from htd.domain.common.errors import (
    INVALID_TIMESTAMP,
    MODEL_NAME_INVALID,
    UNKNOWN_HARNESS,
    USAGE_EVENT_INVALID,
    DomainError,
)
from htd.domain.common.hashing import identity_key
from htd.domain.common.time import format_instant, from_epoch_ms
from htd.domain.usage.model import Diagnostic, Harness, Result, Skip, Tokens, UsageEvent, optional_text
from htd.domain.usage.model_key import model_key


@pytest.fixture
def event() -> UsageEvent:
    return UsageEvent(
        harness=Harness.CODEX,
        dedupe_key=identity_key(["session", "request"]),
        session_id="session",
        request_id="request",
        model_id="fixture-model",
        occurred_at="2026-09-01T00:00:00Z",
        tokens=Tokens(input=10, cache_read=0, cache_write=0, output=5, reasoning=0),
        status="reported",
    )


def test_harness_values_and_display_order() -> None:
    assert [h.value for h in Harness] == ["codex", "zcode", "dsh", "claude"]
    for harness in Harness:
        assert Harness(harness.value) is harness


@pytest.mark.parametrize("value", ["unknown", "CODEX", " codex ", "", None, 0, True, [], {}])
def test_unknown_harness_is_structured_error(value: object, event: UsageEvent) -> None:
    with pytest.raises(DomainError) as failure:
        Harness(value)
    assert failure.value.code == UNKNOWN_HARNESS
    with pytest.raises(DomainError) as failure:
        replace(event, harness=cast(Harness, value))
    assert failure.value.code == UNKNOWN_HARNESS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, None),
        (123, None),
        ([], None),
        ({}, None),
        ("", None),
        (" \u0085\t\n", None),
        ("  model A \n", "model A"),
        ("a" * 512, "a" * 512),
        ("a" * 513, None),
        ("界" * 170 + "ab", "界" * 170 + "ab"),
        ("界" * 171, None),
        ("😀" * 128, "😀" * 128),
        ("😀" * 129, None),
        (" " + "a" * 512 + " ", "a" * 512),
        ("\ud800", None),
    ],
)
def test_optional_text(value: object, expected: str | None) -> None:
    assert optional_text(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-01T00:00:00.1239Z", "2026-09-01T00:00:00.123Z"),
        ("2026-09-01T08:00:00.1239+08:00", "2026-09-01T00:00:00.123Z"),
        (format_instant(from_epoch_ms(-1)), "1969-12-31T23:59:59.999Z"),
    ],
)
def test_event_time_normalization(event: UsageEvent, value: str, expected: str) -> None:
    assert replace(event, occurred_at=value).occurred_at == expected
    assert event.time_precision == "request_completion"


@pytest.mark.parametrize("value", ["2026-09-01T00:00:00", "2026-02-29T00:00:00Z", "", None, 12, True])
def test_event_rejects_invalid_timestamp(event: UsageEvent, value: object) -> None:
    with pytest.raises(DomainError) as failure:
        replace(event, occurred_at=cast(str, value))
    assert failure.value.code == INVALID_TIMESTAMP


@pytest.mark.parametrize("key", ["a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n", None, 0])
def test_dedupe_key_must_be_sha256(event: UsageEvent, key: object) -> None:
    with pytest.raises(DomainError) as failure:
        replace(event, dedupe_key=cast(str, key))
    assert (failure.value.code, failure.value.field) == (USAGE_EVENT_INVALID, "dedupe_key")


def test_trimmed_fields_do_not_change_identity_hash_or_model_spelling(event: UsageEvent) -> None:
    raw_session, raw_request = " session ", " request "
    dedupe_key = identity_key([raw_session, raw_request])
    result = replace(
        event,
        session_id=raw_session,
        request_id=raw_request,
        model_id=" FIXTURE- MODEL ",
        dedupe_key=dedupe_key,
    )
    assert (result.session_id, result.request_id, result.model_id) == ("session", "request", "FIXTURE- MODEL")
    assert result.model_id is not None
    assert model_key(result.model_id) == "fixture-model"
    assert result.dedupe_key == dedupe_key != event.dedupe_key


@pytest.mark.parametrize("value", [None, "", "  ", "a" * 513, 12, True])
def test_missing_text_is_unknown_or_rejected_as_required(event: UsageEvent, value: object) -> None:
    assert replace(event, model_id=cast(str, value)).issues == ("model_unknown",)
    for field in ("session_id", "request_id"):
        with pytest.raises(DomainError) as failure:
            replace(event, **{field: value})
        assert (failure.value.code, failure.value.field) == (USAGE_EVENT_INVALID, field)
    zcode = replace(event, harness=Harness.ZCODE, status="completed", session_id=cast(str, value))
    assert zcode.session_id is None


def test_empty_model_key_is_invalid(event: UsageEvent) -> None:
    with pytest.raises(DomainError) as failure:
        replace(event, model_id="\ufeff \ufeff")
    assert failure.value.code == MODEL_NAME_INVALID


@pytest.mark.parametrize("harness", list(Harness))
@pytest.mark.parametrize("status", ["reported", "completed", "error", "cancelled", "pending", "", None])
def test_status_matches_source(event: UsageEvent, harness: Harness, status: object) -> None:
    valid = (
        status in ("completed", "error", "cancelled") if harness == Harness.ZCODE else status == "reported"
    )
    if valid:
        assert replace(event, harness=harness, status=status).status == status
    else:
        with pytest.raises(DomainError) as failure:
            replace(event, harness=harness, status=status)
        assert (failure.value.code, failure.value.field) == (USAGE_EVENT_INVALID, "status")


def test_dsh_also_requires_a_session(event: UsageEvent) -> None:
    with pytest.raises(DomainError) as failure:
        replace(event, harness=Harness.DSH, session_id=None)
    assert failure.value.field == "session_id"


@pytest.mark.parametrize("tokens", [None, {}, [], 0])
def test_event_requires_validated_tokens(event: UsageEvent, tokens: object) -> None:
    with pytest.raises(DomainError) as failure:
        replace(event, tokens=cast(Tokens, tokens))
    assert (failure.value.code, failure.value.field) == (USAGE_EVENT_INVALID, "tokens")


def test_quality_flags_order_and_unknown_values(event: UsageEvent) -> None:
    unknown = replace(event, model_id=None, tokens=Tokens(), issues=("source_b", "source_a"))
    assert unknown.issues == (
        "source_b",
        "source_a",
        "model_unknown",
        "usage_incomplete",
        "cache_unknown",
        "cache_write_unknown",
    )
    assert unknown.tokens == Tokens()
    writing = replace(unknown, tokens=Tokens(cache_write=1))
    assert writing.issues == (
        "source_b",
        "source_a",
        "model_unknown",
        "usage_incomplete",
        "cache_unknown",
    )
    assert replace(writing, model_id=event.model_id, tokens=event.tokens).issues == ("source_b", "source_a")
    assert replace(unknown).issues == unknown.issues


@pytest.mark.parametrize(("input", "output"), [(None, None), (None, 0), (0, None), (0, 0)])
def test_incomplete_only_depends_on_input_and_output(
    event: UsageEvent, input: int | None, output: int | None
) -> None:
    result = replace(event, tokens=Tokens(input=input, output=output, cache_read=0, cache_write=0))
    assert result.issues == (() if input is not None and output is not None else ("usage_incomplete",))


def test_known_zero_cache_is_distinct_from_unknown(event: UsageEvent) -> None:
    assert event.issues == ()
    assert replace(event, tokens=replace(event.tokens, cache_read=None)).issues == ("cache_unknown",)
    assert replace(event, tokens=replace(event.tokens, cache_write=None)).issues == ("cache_write_unknown",)
    assert replace(event, tokens=replace(event.tokens, cache_write=1)).issues == ()


@pytest.mark.parametrize("issues", ["flag", None, (None,), (1,), ("",), (" ",)])
def test_invalid_issue_sequences(event: UsageEvent, issues: object) -> None:
    with pytest.raises(DomainError) as failure:
        replace(event, issues=cast(tuple[str, ...], issues))
    assert (failure.value.code, failure.value.field) == (USAGE_EVENT_INVALID, "issues")


def test_event_and_results_are_frozen_slotted_values(event: UsageEvent) -> None:
    source_flags = ["source_flag"]
    result = replace(event, issues=cast(tuple[str, ...], source_flags))
    source_flags.append("later_mutation")
    assert result.issues == ("source_flag",)
    with pytest.raises(FrozenInstanceError):
        result.status = "error"  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(FrozenInstanceError):
        result.time_precision = "request_completion"  # pyright: ignore[reportAttributeAccessIssue]
    outcomes: tuple[Result, ...] = (result, Skip(), Diagnostic("invalid_token_count"))
    for outcome in outcomes:
        assert not hasattr(outcome, "__dict__")
    assert len(set(outcomes)) == 3
    assert Diagnostic("invalid_token_count").code == "invalid_token_count"
    with pytest.raises(FrozenInstanceError):
        outcomes[2].code = "changed"  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize("code", ["", " ", None, 12])
def test_diagnostic_requires_a_code(code: object) -> None:
    with pytest.raises(DomainError) as failure:
        Diagnostic(cast(str, code))
    assert failure.value.field == "code"


@pytest.mark.parametrize("kind", ["desktop", "desktop_subagent", None])
def test_codex_source_kind(event: UsageEvent, kind: object) -> None:
    assert replace(event, source_kind=kind).source_kind == kind


@pytest.mark.parametrize("kind", ["unknown", "", [], True])
def test_unknown_source_kind_is_rejected(event: UsageEvent, kind: object) -> None:
    with pytest.raises(DomainError) as failure:
        replace(event, source_kind=kind)
    assert (failure.value.code, failure.value.field) == (USAGE_EVENT_INVALID, "source_kind")


@pytest.mark.parametrize("harness", [Harness.ZCODE, Harness.DSH])
def test_other_harness_cannot_claim_codex_source_kind(event: UsageEvent, harness: Harness) -> None:
    with pytest.raises(DomainError) as failure:
        replace(
            event,
            harness=harness,
            status="completed" if harness == Harness.ZCODE else "reported",
            source_kind="desktop",
        )
    assert failure.value.field == "source_kind"
