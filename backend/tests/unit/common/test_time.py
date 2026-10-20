from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest
from htu.domain.common.errors import INVALID_TIMESTAMP, DomainError
from htu.domain.common.time import Instant, format_instant, from_epoch_ms, parse_instant
from hypothesis import given
from hypothesis import strategies as st


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-09-01T00:00:00.1239Z", "2026-09-01T00:00:00.123Z"),
        ("2026-09-01T08:00:00.123999999+08:00", "2026-09-01T00:00:00.123Z"),
        ("2026-09-01T00:00:00+08:00", "2026-08-31T16:00:00.000Z"),
        ("2026-12-31T23:00:00-02:00", "2027-01-01T01:00:00.000Z"),
        ("2024-02-29t12:34:56.9z", "2024-02-29T12:34:56.900Z"),
        ("1969-12-31T23:59:59.9999Z", "1969-12-31T23:59:59.999Z"),
        ("0001-01-01T00:00:00Z", "0001-01-01T00:00:00.000Z"),
        ("9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999Z"),
    ],
)
def test_time_normalization(text: str, expected: str) -> None:
    assert format_instant(parse_instant(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-01T12:00:00",
        "2026-09-01",
        "2026-02-29T00:00:00Z",
        "2026-13-01T00:00:00Z",
        "2026-09-01T24:00:00Z",
        "2026-09-01T00:60:00Z",
        "2026-09-01T00:00:60Z",
        "2026-09-01T00:00:00+24:00",
        "2026-09-01T00:00:00+01:60",
        "2026-09-01T00:00:00+08:00:01",
        "2026-09-01T00:00:00Z\n",
        " 2026-09-01T00:00:00Z",
        "2026-09-01T00:00:00.Z",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
        "0000-01-01T00:00:00Z",
        "",
        None,
        True,
        0,
    ],
)
def test_invalid_time_is_structured_error(text: object) -> None:
    with pytest.raises(DomainError) as failure:
        parse_instant(cast(str, text))
    assert failure.value.code == INVALID_TIMESTAMP


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [
        (0, "1970-01-01T00:00:00.000Z"),
        (-1, "1969-12-31T23:59:59.999Z"),
        (1786629394116, "2026-08-13T13:56:34.116Z"),
        (-62135596800000, "0001-01-01T00:00:00.000Z"),
        (253402300799999, "9999-12-31T23:59:59.999Z"),
    ],
)
def test_epoch_ms(epoch: int, expected: str) -> None:
    assert format_instant(from_epoch_ms(epoch)) == expected


@pytest.mark.parametrize("epoch", [-62135596800001, 253402300800000, 10**100, True, 1.5, "0", None])
def test_epoch_rejects_invalid_values(epoch: object) -> None:
    with pytest.raises(DomainError) as failure:
        from_epoch_ms(cast(int, epoch))
    assert failure.value.code == INVALID_TIMESTAMP


def test_constructor_normalizes_and_value_is_immutable() -> None:
    instant = Instant(datetime(2026, 9, 1, 8, microsecond=123999, tzinfo=timezone(timedelta(hours=8))))
    assert instant.value == datetime(2026, 9, 1, microsecond=123000, tzinfo=UTC)
    assert instant == parse_instant("2026-09-01T00:00:00.123Z")
    assert instant < parse_instant("2026-09-01T00:00:00.124Z")
    with pytest.raises(FrozenInstanceError):
        instant.value = datetime(2026, 9, 2, tzinfo=UTC)  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(DomainError):
        Instant(datetime(2026, 9, 1))


@given(st.integers(min_value=-62135596800000, max_value=253402300799999))
def test_epoch_and_string_roundtrip(epoch: int) -> None:
    instant = from_epoch_ms(epoch)
    assert parse_instant(format_instant(instant)) == instant
    difference = instant.value - datetime(1970, 1, 1, tzinfo=UTC)
    assert (difference.days * 86400 + difference.seconds) * 1000 + difference.microseconds // 1000 == epoch
