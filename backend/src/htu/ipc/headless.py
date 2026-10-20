"""A single request and envelope per line; logs and diagnostics never use stdout."""

import json
import math
from typing import TextIO

from pydantic import ValidationError

from htu.domain.common.errors import DomainError
from htu.ipc.bridge import Bridge
from htu.ipc.dto.base import StrictModel
from htu.ipc.errors import exception_envelope, invalid_request


class CallRequest(StrictModel):
    command: str
    payload: dict[str, object]


def _reject_constant(value: str) -> object:
    raise ValueError("Non-finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def serve(bridge: Bridge, source: TextIO, destination: TextIO) -> None:
    for line in source:
        try:
            raw: object = json.loads(
                line,
                parse_constant=_reject_constant,
                parse_float=_finite_float,
                object_pairs_hook=_unique_object,
            )
        except (ValueError, RecursionError):
            response = exception_envelope(
                DomainError("INVALID_REQUEST", "请求须为有效的 JSON 对象", "payload")
            )
        else:
            try:
                request = CallRequest.model_validate(raw)
            except ValidationError as error:
                response = invalid_request(error)
            else:
                response = bridge.call(request.command, request.payload)
        destination.write(
            json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
        )
        destination.flush()
