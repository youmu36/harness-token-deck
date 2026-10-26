"""Exact multiplier parsing and six-decimal half-up price rounding."""

import re
from dataclasses import dataclass

from htd.domain.common.errors import MULTIPLIER_INVALID, RATE_OVERFLOW, DomainError
from htd.domain.common.money import MAX_SCALED_RATE, Rate, parse_rate

_MULTIPLIER = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,4})?")


@dataclass(frozen=True, slots=True)
class Multiplier:
    ten_thousandths: int

    def __post_init__(self) -> None:
        if type(self.ten_thousandths) is not int or not 0 <= self.ten_thousandths <= 9_999_999_999_999:
            raise DomainError(
                MULTIPLIER_INVALID, "倍率须为非负数，整数最多 9 位、小数最多 4 位", "multiplier"
            )

    @property
    def normalized(self) -> str:
        whole, remainder = divmod(self.ten_thousandths, 10000)
        fraction = f"{remainder:04d}".rstrip("0")
        return f"{whole}.{fraction}" if fraction else str(whole)


def parse_multiplier(text: str) -> Multiplier:
    if type(text) is not str or _MULTIPLIER.fullmatch(text) is None:
        raise DomainError(MULTIPLIER_INVALID, "倍率格式无效，最多保留四位小数", "multiplier")
    whole, _, fraction = text.partition(".")
    return Multiplier(int(whole) * 10000 + int(fraction.ljust(4, "0")))


def multiply_rate(rate: str | None, multiplier: str | Multiplier) -> str | None:
    factor = multiplier if isinstance(multiplier, Multiplier) else parse_multiplier(multiplier)
    if rate is None:
        return None
    scaled = (parse_rate(rate).scaled * factor.ten_thousandths + 5000) // 10000
    if scaled > MAX_SCALED_RATE:
        raise DomainError(RATE_OVERFLOW, "倍率计算后的价格超出范围", "rate")
    return Rate(scaled).normalized
