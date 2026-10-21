from collections.abc import Generator
from contextlib import contextmanager

import pytest
from htd.domain.common.errors import RATE_INVALID, DomainError


def test_structured_error_supports_exception_tracebacks() -> None:
    @contextmanager
    def operation() -> Generator[None]:
        yield

    with pytest.raises(DomainError) as failure, operation():
        raise DomainError(RATE_INVALID, "价格格式无效", "rates.input")
    error = failure.value
    assert (error.code, error.message, error.field) == (RATE_INVALID, "价格格式无效", "rates.input")
    assert str(error) == "价格格式无效"
    assert error.__traceback__ is not None
    assert DomainError(RATE_INVALID, "价格格式无效").field is None
