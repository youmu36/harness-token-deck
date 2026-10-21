"""Canonical decimal TEXT codecs for arbitrary-precision persisted amounts."""

import re

from htd.domain.common.errors import PICO_INVALID, DomainError

_PICO = re.compile(r"-?(0|[1-9][0-9]*)")


def decode_pico(value: str) -> int:
    if type(value) is not str or len(value) > 40 or _PICO.fullmatch(value) is None or value == "-0":
        raise DomainError(PICO_INVALID, "存储金额须为规范十进制整数", "pico")
    return int(value)


def encode_pico(value: int) -> str:
    if type(value) is not int:
        raise DomainError(PICO_INVALID, "金额须为整数 pico", "pico")
    text = str(value)
    decode_pico(text)
    return text
