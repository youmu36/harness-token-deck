from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest
from htu.domain.common.errors import (
    CACHE_EXCEEDS_INPUT,
    INVALID_TOKEN_COUNT,
    REASONING_EXCEEDS_OUTPUT,
    DomainError,
)
from htu.domain.usage.model import MAX_TOKEN_COUNT, Tokens
from hypothesis import given
from hypothesis import strategies as st

FIELDS = ("input", "cache_read", "cache_write", "output", "reasoning")


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [-1, 2**53, 12.0, True, False, "12", [], {}, float("nan"), float("inf")])
def test_each_token_field_rejects_invalid_counts(field: str, value: object) -> None:
    with pytest.raises(DomainError) as failure:
        Tokens(**{field: cast(int, value)})
    assert (failure.value.code, failure.value.field) == (INVALID_TOKEN_COUNT, field)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [None, 0, MAX_TOKEN_COUNT])
def test_each_token_field_accepts_boundaries(field: str, value: int | None) -> None:
    tokens = Tokens(**{field: value})
    assert getattr(tokens, field) == value


@pytest.mark.parametrize(
    ("counts", "field"),
    [
        ({"input": 10, "cache_read": 11}, "cache_read"),
        ({"input": 10, "cache_write": 11}, "cache_write"),
        ({"input": 10, "cache_read": 6, "cache_write": 5}, "input"),
        ({"input": 0, "cache_read": 1}, "cache_read"),
        ({"input": 0, "cache_write": 1}, "cache_write"),
        ({"input": MAX_TOKEN_COUNT, "cache_read": MAX_TOKEN_COUNT, "cache_write": 1}, "input"),
    ],
)
def test_cache_cannot_exceed_input(counts: dict[str, int], field: str) -> None:
    with pytest.raises(DomainError) as failure:
        Tokens(**counts)
    assert (failure.value.code, failure.value.field) == (CACHE_EXCEEDS_INPUT, field)


@pytest.mark.parametrize(("output", "reasoning"), [(0, 1), (10, 11), (MAX_TOKEN_COUNT - 1, MAX_TOKEN_COUNT)])
def test_reasoning_cannot_exceed_output(output: int, reasoning: int) -> None:
    with pytest.raises(DomainError) as failure:
        Tokens(output=output, reasoning=reasoning)
    assert (failure.value.code, failure.value.field) == (REASONING_EXCEEDS_OUTPUT, "reasoning")


def test_unknown_components_stay_unknown_and_relations_only_check_known_values() -> None:
    assert Tokens() == Tokens(None, None, None, None, None)
    assert Tokens(cache_read=MAX_TOKEN_COUNT, cache_write=MAX_TOKEN_COUNT).input is None
    assert Tokens(reasoning=MAX_TOKEN_COUNT).output is None
    assert Tokens(input=10, cache_read=10).cache_write is None
    assert Tokens(input=10, cache_write=10).cache_read is None
    assert Tokens(output=10).reasoning is None
    assert Tokens(0, 0, 0, 0, 0).input == 0
    assert Tokens(output=MAX_TOKEN_COUNT, reasoning=MAX_TOKEN_COUNT).reasoning == MAX_TOKEN_COUNT


@given(st.integers(min_value=0, max_value=MAX_TOKEN_COUNT), st.data())
def test_cache_sum_equal_to_input_is_valid(total: int, data: st.DataObject) -> None:
    read = data.draw(st.integers(min_value=0, max_value=total))
    tokens = Tokens(input=total, cache_read=read, cache_write=total - read)
    assert tokens.cache_read == read
    assert tokens.cache_write == total - read
    with pytest.raises(DomainError) as failure:
        replace(tokens, cache_write=total - read + 1)
    assert failure.value.code in (CACHE_EXCEEDS_INPUT, INVALID_TOKEN_COUNT)


def test_tokens_are_frozen_and_slotted() -> None:
    tokens = Tokens(input=1)
    assert not hasattr(tokens, "__dict__")
    with pytest.raises(FrozenInstanceError):
        tokens.input = 2  # pyright: ignore[reportAttributeAccessIssue]
