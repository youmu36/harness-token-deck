"""Stream plain or concatenated-frame DSH transcripts with bounded decoded bytes."""

import os
from typing import BinaryIO, cast

import zstandard

from htu.adapters.collectors.json_lines import decode
from htu.adapters.collectors.shared_file import open_shared
from htu.application.ports import CheckpointRecord, ReadBatch
from htu.domain.common.time import Instant
from htu.domain.sources.dsh import DshContext, parse_record
from htu.domain.usage.model import Diagnostic, UsageEvent

_MAGIC = b"\x28\xb5\x2f\xfd"


class _SnapshotReader:
    def __init__(self, stream: BinaryIO, length: int) -> None:
        self.stream = stream
        self.remaining = length

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(self.remaining if size < 0 else min(size, self.remaining))
        self.remaining -= len(data)
        return data


class DshReader:
    def __init__(self, *, byte_limit: int = 256 * 1024**2, container_limit: int = 256 * 1024**2) -> None:
        if byte_limit <= 0 or container_limit <= 0:
            raise ValueError("Read limits must be positive")
        self.byte_limit = byte_limit
        self.container_limit = container_limit

    def read(self, path: str, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch:
        try:
            with open_shared(path) as source:
                before = os.fstat(source.fileno())
                size = before.st_size
                compressed = source.read(4) == _MAGIC
                source.seek(0)
                if compressed and size > self.container_limit:
                    return ReadBatch({}, diagnostics=("source_file_too_large",))
                bounded = cast(BinaryIO, _SnapshotReader(source, size))
                if compressed:
                    with zstandard.ZstdDecompressor().stream_reader(
                        bounded, read_across_frames=True, closefd=False
                    ) as decoded:
                        result = self._consume(cast(BinaryIO, decoded), started_at)
                else:
                    result = self._consume(bounded, started_at)
                after = os.fstat(source.fileno())
                if after.st_size < size or (
                    after.st_size == size and after.st_mtime_ns != before.st_mtime_ns
                ):
                    return ReadBatch({}, diagnostics=("source_changed_during_read",), retry=True)
                return ReadBatch(
                    result.checkpoint,
                    result.events,
                    tuple(sorted(set(result.diagnostics) | set(checkpoint.diagnostics))),
                )
        except zstandard.ZstdError:
            # Discard a partial result on a broken container; retry when its signature changes.
            return ReadBatch({}, diagnostics=("source_decode_failed",))
        except OSError:
            return ReadBatch({}, diagnostics=("source_read_failed",), available=False, retry=True)

    def _consume(self, stream: BinaryIO, started_at: Instant) -> ReadBatch:
        context = DshContext()
        events: list[UsageEvent] = []
        diagnostics: set[str] = set()
        buffer = b""
        consumed = 0

        def consume(line: bytes) -> None:
            result = parse_record(decode(line), context, started_at)
            if isinstance(result, UsageEvent):
                events.append(result)
            elif isinstance(result, Diagnostic):
                diagnostics.add(result.code)

        while consumed < self.byte_limit:
            chunk = stream.read(min(64 * 1024, self.byte_limit - consumed))
            if not chunk:
                # DSH full-file reads accept a complete JSON value without a trailing newline.
                if buffer:
                    consume(buffer)
                break
            consumed += len(chunk)
            lines = (buffer + chunk).split(b"\n")
            buffer = lines.pop()
            for line in lines:
                consume(line)
        if consumed == self.byte_limit:
            diagnostics.add("source_truncated")
        return ReadBatch({}, tuple(events), tuple(sorted(diagnostics)))
