"""Timezone-aware UTC instants with millisecond precision."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from htu.domain.common.errors import INVALID_TIMESTAMP, DomainError

_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True, order=True)
class Instant:
    value: datetime

    def __post_init__(self) -> None:
        try:
            if type(self.value) is not datetime or self.value.utcoffset() is None:
                raise ValueError("Timezone required")
            utc = self.value.astimezone(UTC)
            object.__setattr__(self, "value", utc.replace(microsecond=utc.microsecond // 1000 * 1000))
        except (ValueError, OverflowError) as error:
            raise DomainError(
                INVALID_TIMESTAMP, "时间无效，须包含时区且在有效日期范围内", "timestamp"
            ) from error


def parse_instant(text: str) -> Instant:
    if type(text) is not str or _TIMESTAMP.fullmatch(text) is None:
        raise DomainError(INVALID_TIMESTAMP, "时间格式无效，须包含日期、时间和时区", "timestamp")
    try:
        return Instant(datetime.fromisoformat(text.upper()))
    except ValueError as error:
        raise DomainError(INVALID_TIMESTAMP, "日期或时间无效", "timestamp") from error


def format_instant(instant: Instant) -> str:
    return instant.value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def from_epoch_ms(milliseconds: int) -> Instant:
    if type(milliseconds) is not int:
        raise DomainError(INVALID_TIMESTAMP, "时间戳必须为整数毫秒", "timestamp")
    try:
        return Instant(_EPOCH + timedelta(milliseconds=milliseconds))
    except OverflowError as error:
        raise DomainError(INVALID_TIMESTAMP, "时间戳超出有效日期范围", "timestamp") from error


class Clock(Protocol):
    def now_utc(self) -> Instant: ...

    def monotonic(self) -> float: ...
