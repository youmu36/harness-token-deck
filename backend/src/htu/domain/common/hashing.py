"""Stable request identities, independent of paths and database schema."""

import hashlib
import json
from collections.abc import Sequence

from htu.domain.common.errors import IDENTITY_INVALID, DomainError


def encode_parts(parts: Sequence[str | int]) -> str:
    if isinstance(parts, (str, bytes)) or any(type(part) not in {str, int} for part in parts):
        raise DomainError(IDENTITY_INVALID, "身份字段只能包含字符串或整数", "parts")
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    try:
        encoded.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DomainError(IDENTITY_INVALID, "身份字段包含无效的 Unicode 字符", "parts") from error
    return encoded


def identity_key(parts: list[str | int]) -> str:
    return hashlib.sha256(encode_parts(parts).encode("utf-8")).hexdigest()
