"""Explicitly advanced clock for deterministic domain and scheduler tests."""

from dataclasses import dataclass, field
from datetime import timedelta

from htd.domain.common.errors import CLOCK_INVALID, DomainError
from htd.domain.common.time import Instant, parse_instant


@dataclass(slots=True)
class FakeClock:
    instant: Instant = field(default_factory=lambda: parse_instant("2026-09-01T00:00:00.000Z"))
    _elapsed_ms: int = field(default=0, init=False, repr=False)

    def now_utc(self) -> Instant:
        return self.instant

    def monotonic(self) -> float:
        return self._elapsed_ms / 1000

    def advance(self, milliseconds: int) -> None:
        if type(milliseconds) is not int or milliseconds < 0:
            raise DomainError(CLOCK_INVALID, "测试时钟只能推进非负整数毫秒", "milliseconds")
        try:
            instant = Instant(self.instant.value + timedelta(milliseconds=milliseconds))
        except OverflowError as error:
            raise DomainError(CLOCK_INVALID, "测试时钟超出有效日期范围", "milliseconds") from error
        self.instant = instant
        self._elapsed_ms += milliseconds

    def set_wall(self, instant: Instant) -> None:
        self.instant = instant
