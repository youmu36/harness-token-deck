"""The IPC primitives reject coercion, unsafe counts and noncanonical timestamps."""

import pytest
from htd.ipc.dto.base import InstantStr, PicoStr, RateStr, SafeInt
from pydantic import TypeAdapter, ValidationError


def test_wire_primitives_preserve_exact_values() -> None:
    assert (
        TypeAdapter(PicoStr).validate_python("-999999999999999999999999999999")
        == "-999999999999999999999999999999"
    )
    assert TypeAdapter(RateStr).validate_python("999999999.999999") == "999999999.999999"
    assert TypeAdapter(InstantStr).validate_python("2026-10-01T00:00:00.123Z") == "2026-10-01T00:00:00.123Z"
    assert TypeAdapter(SafeInt).validate_python(2**53 - 1) == 2**53 - 1


@pytest.mark.parametrize("value", [12, "01", "-0", "1.2", "1e3", "+1", "1\n"])
def test_pico_rejects_noncanonical_decimal(value: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(PicoStr).validate_python(value)


@pytest.mark.parametrize("value", [1.2, "-1", "1000000000", "1.1234567", "1e-6"])
def test_rate_rejects_non_decimal_or_out_of_range(value: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(RateStr).validate_python(value)


@pytest.mark.parametrize("value", [True, "1", 1.0, -1, 2**53])
def test_count_rejects_coercion_and_unsafe_number(value: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SafeInt).validate_python(value)


@pytest.mark.parametrize(
    "value", ["2026-02-30T00:00:00.000Z", "2026-10-01T08:00:00.000+08:00", "2026-10-01T00:00:00Z"]
)
def test_instant_requires_valid_utc_milliseconds(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(InstantStr).validate_python(value)
