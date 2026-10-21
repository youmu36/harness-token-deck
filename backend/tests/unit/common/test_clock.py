from typing import cast

import pytest
from htd.domain.common.errors import CLOCK_INVALID, DomainError
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Clock, format_instant, parse_instant


def test_clock_can_advance_without_sleep_and_simulate_wall_rollback(fake_clock: FakeClock) -> None:
    clock: Clock = fake_clock
    assert clock.monotonic() == 0
    fake_clock.advance(1501)
    assert clock.monotonic() == 1.501
    assert format_instant(clock.now_utc()) == "2026-09-01T00:00:01.501Z"
    fake_clock.set_wall(parse_instant("2026-08-01T00:00:00Z"))
    assert clock.monotonic() == 1.501
    fake_clock.advance(499)
    assert clock.monotonic() == 2
    assert format_instant(clock.now_utc()) == "2026-08-01T00:00:00.499Z"


@pytest.mark.parametrize("delta", [-1, True, 1.5, 10**100])
def test_invalid_advance_does_not_partially_update_clock(fake_clock: FakeClock, delta: object) -> None:
    initial = fake_clock.now_utc()
    with pytest.raises(DomainError) as failure:
        fake_clock.advance(cast(int, delta))
    assert failure.value.code == CLOCK_INVALID
    assert fake_clock.now_utc() == initial
    assert fake_clock.monotonic() == 0
