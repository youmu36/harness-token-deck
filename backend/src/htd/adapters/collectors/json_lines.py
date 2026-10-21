"""Decode strict JSON without retaining source text in errors."""

import json
from typing import Never


def _reject_constant(value: str) -> Never:
    raise ValueError("Non-JSON numeric constant")


def decode(line: bytes) -> object:
    try:
        return json.loads(line, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return None
