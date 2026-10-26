"""Owned in-process value snapshots, with independently decodable display pages."""

import marshal
import sys
import zlib
from dataclasses import dataclass
from typing import Any

_CHUNK = 64
_COMPRESS_MIN = 4096


def _pack(value: Any, *, compress: bool) -> bytes:
    # These bytes only encode our own completed reads and never leave this process.
    # Avoid a text/UTF-8 round trip while keeping cache ownership independent of callers.
    encoded = marshal.dumps(value, 4)
    return (
        b"\x01" + zlib.compress(encoded, level=1)
        if compress and len(encoded) >= _COMPRESS_MIN
        else b"\x00" + encoded
    )


def _unpack(value: bytes) -> Any:
    return marshal.loads(zlib.decompress(value[1:]) if value[0] else value[1:])


@dataclass(frozen=True, slots=True)
class _Rows:
    chunks: tuple[bytes, ...]
    count: int

    @classmethod
    def build(cls, rows: list[Any], *, compress: bool) -> "_Rows":
        return cls(
            tuple(
                _pack(rows[index : index + _CHUNK], compress=compress)
                for index in range(0, len(rows), _CHUNK)
            ),
            len(rows),
        )

    def page(self, offset: int, limit: int) -> list[Any]:
        end = min(self.count, offset + limit)
        if offset >= end:
            return []
        rows: list[Any] = []
        for index in range(offset // _CHUNK, (end - 1) // _CHUNK + 1):
            block = _unpack(self.chunks[index])
            start = max(0, offset - index * _CHUNK)
            rows.extend(block[start : min(_CHUNK, end - index * _CHUNK)])
        return rows

    @property
    def retained_bytes(self) -> int:
        return sys.getsizeof(self) + sys.getsizeof(self.chunks) + sum(map(sys.getsizeof, self.chunks))


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    payload: bytes
    models: _Rows | None
    quality: tuple[_Rows, _Rows, _Rows] | None
    expires_at: float | None = None

    @classmethod
    def build(
        cls, result: dict[str, Any], *, compress: bool = True, expires_at: float | None = None
    ) -> "ProjectionSnapshot":
        metadata = {key: value for key, value in result.items() if key not in ("models", "quality")}
        models = _Rows.build(result["models"], compress=compress) if "models" in result else None
        quality = None
        if "quality" in result:
            rows = result["quality"]
            all_rows = _Rows.build(rows, compress=compress)
            acknowledged = [row for row in rows if row["acknowledged"]]
            quality = (
                all_rows,
                all_rows if len(acknowledged) == len(rows) else _Rows.build(acknowledged, compress=compress),
                all_rows
                if not acknowledged
                else _Rows.build([row for row in rows if not row["acknowledged"]], compress=compress),
            )
        return cls(_pack(metadata, compress=compress), models, quality, expires_at)

    @property
    def retained_bytes(self) -> int:
        return (
            sys.getsizeof(self)
            + sys.getsizeof(self.payload)
            + (sys.getsizeof(self.expires_at) if self.expires_at is not None else 0)
            + (self.models.retained_bytes if self.models else 0)
            + (
                sys.getsizeof(self.quality)
                + sum(rows.retained_bytes for rows in {id(rows): rows for rows in self.quality}.values())
                if self.quality
                else 0
            )
        )

    def materialize(self, query: dict[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = _unpack(self.payload)
        if self.models is not None:
            offset = int(query.get("offset", 0)) if query is not None else 0
            limit = int(query.get("limit", 50)) if query is not None else self.models.count
            result["models"] = self.models.page(offset, limit)
            if query is not None:
                result["nextOffset"] = offset + limit if offset + limit < result["totalModels"] else None
        if self.quality is not None:
            status = query.get("qualityStatus", "all") if query is not None else "all"
            rows = self.quality[0 if status == "all" else 1 if status == "acknowledged" else 2]
            offset = int(query.get("qualityOffset", 0)) if query is not None else 0
            limit = 100 if query is not None else rows.count
            result["quality"] = rows.page(offset, limit)
            if query is not None:
                result["qualityTotal"] = rows.count
                result["qualityNextOffset"] = offset + limit if offset + limit < rows.count else None
        return result
