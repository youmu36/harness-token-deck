"""Encode source supplements separately from stable diagnostic codes."""

from typing import cast

from htd.adapters.sqlite.usage_codec import dumps, loads
from htd.application.ports import UnmeteredRequest
from htd.domain.common.errors import DomainError


def encode_unmetered(requests: tuple[UnmeteredRequest, ...] | None) -> str | None:
    return None if requests is None else dumps([request.to_dict() for request in requests])


def decode_unmetered(value: str | None) -> tuple[UnmeteredRequest, ...] | None:
    if value is None:
        return None
    try:
        decoded = loads(value)
        if type(decoded) is not list:
            raise ValueError("Expected a list")
        result: list[UnmeteredRequest] = []
        for item in cast(list[object], decoded):
            if type(item) is not dict:
                raise ValueError("Expected an object")
            row = cast(dict[str, object], item)
            if row.get("kind") != "title_generation":
                raise ValueError("Unsupported kind")
            result.append(UnmeteredRequest(cast(str, row["date"]), cast(int, row["count"])))
        return tuple(result)
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise DomainError("SOURCE_STATE_CORRUPT", "来源未计量请求数据损坏") from error
