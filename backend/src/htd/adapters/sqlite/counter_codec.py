"""Versioned, bounded counter frames. All money remains decimal integer text."""

import json
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, cast

import zstandard

from htd.adapters.sqlite.usage_codec import dumps
from htd.domain.common.time import Instant, format_instant, parse_instant

MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 500
FIELDS = (
    "harness",
    "bucket",
    "vendor_id",
    "model_key",
    "model_id",
    "display_name",
    "records",
    "input",
    "cache_read",
    "output",
    "rated_input",
    "rated_cache",
    "unknown_cache",
    "missing_usage",
    "unknown_model",
    "incomplete",
    "unpriced",
    "miss_input",
    "cache_loss",
    "pico",
    "cache_write",
    "reasoning",
    "cache_bust",
    "large_miss",
)
TEXT = {"harness", "vendor_id", "model_key", "model_id", "display_name"}
FLAGS = {"incomplete", "unpriced", "cache_loss", "cache_bust", "large_miss"}


def compress(value: object) -> bytes:
    body = dumps(value).encode("utf-8")
    if len(body) > MAX_BYTES:
        raise ValueError("Counter frame exceeds decoding limit")
    return zstandard.ZstdCompressor(level=3, write_checksum=True).compress(body)


def decompress(payload: bytes) -> Any:
    if zstandard.frame_content_size(payload) > MAX_BYTES:
        raise ValueError("Counter frame exceeds decoding limit")
    return json.loads(zstandard.ZstdDecompressor().decompress(payload, max_output_size=MAX_BYTES))


def pack(rows: Sequence[dict[str, Any]]) -> bytes:
    if not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError("Invalid counter frame size")
    base = min(row["bucket"] for row in rows)[:10] + "T00:00:00.000Z"
    start = parse_instant(base).value
    dictionary: list[str] = []
    positions: dict[str, int] = {}

    def encode(name: str, value: Any) -> Any:
        if name in TEXT and value is not None:
            if value not in positions:
                positions[value] = len(dictionary)
                dictionary.append(value)
            return positions[value]
        if name in ("bucket", "latest_bucket") and value is not None:
            return int((parse_instant(value).value - start).total_seconds() * 1000)
        return value

    fields = FIELDS + (("latest_bucket",) if any("latest_bucket" in row for row in rows) else ())
    values = [[encode(name, row.get(name)) for name in fields] for row in rows]
    return compress(
        {"v": 2, "base": base, "d": dictionary, "latest": len(fields) > len(FIELDS), "rows": values}
    )


def unpack(payload: bytes) -> list[dict[str, Any]]:
    raw: object = decompress(payload)
    if not isinstance(raw, dict):
        raise ValueError("Unsupported counter frame")
    frame = cast(dict[str, Any], raw)
    if not isinstance(frame.get("rows"), list):
        raise ValueError("Unsupported counter rows")
    if not 1 <= len(cast(list[Any], frame["rows"])) <= MAX_ROWS:
        raise ValueError("Invalid counter frame size")
    if frame.get("v") == 1:
        if any(not isinstance(row, dict) for row in cast(list[Any], frame["rows"])):
            raise ValueError("Invalid legacy counter frame")
        return cast(list[dict[str, Any]], frame["rows"])
    if frame.get("v") != 2 or not isinstance(frame.get("d"), list):
        raise ValueError("Unsupported counter frame version")
    fields = FIELDS + (("latest_bucket",) if frame.get("latest") else ())
    start = parse_instant(frame["base"]).value
    dictionary = cast(list[str], frame["d"])
    stamps: dict[int, str] = {}
    result: list[dict[str, Any]] = []
    for raw_values in cast(list[Any], frame["rows"]):
        if not isinstance(raw_values, list):
            raise ValueError("Invalid counter row")
        values = cast(list[Any], raw_values)
        if len(values) != len(fields):
            raise ValueError("Invalid counter row")
        row: dict[str, Any] = {}
        for name, encoded in zip(fields, values, strict=True):
            value = encoded
            if name in TEXT and value is not None:
                if type(value) is not int or not 0 <= value < len(dictionary):
                    raise ValueError("Invalid counter dictionary index")
                value = dictionary[value]
            elif name in ("bucket", "latest_bucket") and value is not None:
                if type(value) is not int:
                    raise ValueError("Invalid counter time offset")
                if value not in stamps:
                    if 0 <= value < 86_400_000:
                        hour, rest = divmod(value, 3_600_000)
                        minute, rest = divmod(rest, 60_000)
                        second, millis = divmod(rest, 1000)
                        stamps[value] = f"{frame['base'][:10]}T{hour:02}:{minute:02}:{second:02}.{millis:03}Z"
                    else:
                        stamps[value] = format_instant(Instant(start + timedelta(milliseconds=value)))
                value = stamps[value]
            row[name] = value
        if type(row["pico"]) is not str or not row["pico"].isdigit():
            raise ValueError("Invalid exact counter amount")
        result.append(row)
    return result


def merge_minutes(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[name] for name in ("harness", "bucket", "vendor_id", "model_key"))
        old = groups.get(key)
        if old is None:
            groups[key] = dict(row)
            continue
        for name in FIELDS[6:]:
            old[name] = (
                str(int(old[name]) + int(row[name]))
                if name == "pico"
                else (int(bool(old[name] or row[name])) if name in FLAGS else old[name] + row[name])
            )
        # Equal minute/vendor/model keys keep the first presentation value,
        # matching the accumulator's strict latest-key tie rule before merging.
    return sorted(
        groups.values(), key=lambda row: (row["bucket"], row["harness"], row["vendor_id"], row["model_key"])
    )
