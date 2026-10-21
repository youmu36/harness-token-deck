"""Exact pico-USD parsing and display using integer arithmetic only."""

import re
from dataclasses import dataclass
from typing import Final, Literal

from htd.domain.common.errors import (
    AMOUNT_INVALID,
    MONEY_FORMAT_INVALID,
    PICO_INVALID,
    RATE_INVALID,
    DomainError,
)

PICO_PER_USD: Final = 10**12
RATE_SCALE: Final = 10**6
MAX_SCALED_RATE: Final = 999_999_999_999_999
type MoneyFormat = Literal["usd4", "trunc2", "full6"]
_RATE = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,6})?")
_AMOUNT = re.compile(r"-?[0-9]{1,9}(?:\.[0-9]{1,6})?")


@dataclass(frozen=True, slots=True)
class Rate:
    scaled: int

    def __post_init__(self) -> None:
        if type(self.scaled) is not int or not 0 <= self.scaled <= MAX_SCALED_RATE:
            raise DomainError(RATE_INVALID, "价格须为非负数，整数最多 9 位、小数最多 6 位", "rate")

    @property
    def normalized(self) -> str:
        whole, fraction = divmod(self.scaled, RATE_SCALE)
        suffix = f"{fraction:06d}".rstrip("0")
        return f"{whole}.{suffix}" if suffix else str(whole)


def _scaled_unsigned(text: str, digits: int) -> int:
    whole, _, fraction = text.partition(".")
    return int(whole) * 10**digits + int(fraction.ljust(digits, "0"))


def parse_rate(text: str) -> Rate:
    if type(text) is not str or _RATE.fullmatch(text) is None:
        raise DomainError(RATE_INVALID, "价格须为非负数，整数最多 9 位、小数最多 6 位", "rate")
    return Rate(_scaled_unsigned(text, 6))


def parse_usd_amount(text: str, allow_negative: bool = False) -> int:
    if (
        type(text) is not str
        or _AMOUNT.fullmatch(text) is None
        or (text.startswith("-") and not allow_negative)
    ):
        raise DomainError(AMOUNT_INVALID, "金额格式无效，请输入整数最多 9 位、小数最多 6 位的金额", "amount")
    negative = text.startswith("-")
    absolute = _scaled_unsigned(text[1:] if negative else text, 12)
    return -absolute if negative else absolute


def format_pico(pico: int, mode: MoneyFormat) -> str:
    if type(pico) is not int:
        raise DomainError(PICO_INVALID, "金额必须为整数 pico", "pico")
    if mode not in {"usd4", "trunc2", "full6"}:
        raise DomainError(MONEY_FORMAT_INVALID, "不支持的金额显示格式", "mode")
    digits = {"usd4": 4, "trunc2": 2, "full6": 6}[mode]
    quantum = 10 ** (12 - digits)
    rounded = (abs(pico) + (0 if mode == "trunc2" else quantum // 2)) // quantum
    whole, fraction = divmod(rounded, 10**digits)
    sign = "-" if pico < 0 and rounded else ""
    if mode == "usd4":
        return f"{sign}${whole:,}.{fraction:04d}"
    number = f"{sign}{whole}.{fraction:0{digits}d}"
    return f"{number} USD" if mode == "full6" else number
