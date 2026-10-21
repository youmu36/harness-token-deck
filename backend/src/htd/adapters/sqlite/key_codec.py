"""Full SHA-256 on disk; hexadecimal strings at application and IPC boundaries."""

import sqlite3
from typing import Any
from weakref import WeakKeyDictionary

_COLUMNS: WeakKeyDictionary[sqlite3.Cursor, tuple[object, tuple[int, ...]]] = WeakKeyDictionary()


def key_bytes(value: str | bytes) -> bytes:
    result = value if isinstance(value, bytes) else bytes.fromhex(value)
    if len(result) != 32:
        raise ValueError("Expected a full 256-bit request identity")
    return result


def key_row(cursor: sqlite3.Cursor, values: tuple[Any, ...]) -> sqlite3.Row:
    cached = _COLUMNS.get(cursor)
    if cached is None or cached[0] is not cursor.description:
        indices = tuple(
            index
            for index, column in enumerate(cursor.description)
            if column[0] in ("dedupe_key", "usage_key")
        )
        _COLUMNS[cursor] = cursor.description, indices
    else:
        indices = cached[1]
    if not indices:
        return sqlite3.Row(cursor, values)
    decoded = list(values)
    for index in indices:
        if isinstance(decoded[index], bytes):
            decoded[index] = decoded[index].hex()
    return sqlite3.Row(cursor, tuple(decoded))
