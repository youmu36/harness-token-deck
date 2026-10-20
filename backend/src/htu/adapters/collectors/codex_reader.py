"""Snapshot-bounded Codex JSONL reads with anchors and persistent parser context."""

import hashlib
import os
import re
from typing import BinaryIO, cast

from htu.adapters.collectors.json_lines import decode
from htu.adapters.collectors.shared_file import open_shared
from htu.application.ports import CheckpointRecord, ReadBatch
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, parse_instant
from htu.domain.sources.codex import CodexContext, compaction_key, filter_diagnostics, parse_record
from htu.domain.usage.model import Diagnostic, Result, UsageEvent

# Revisit old rejected files after parser changes. Request identities still deduplicate
# the replay and the original source start prevents an implicit historical import.
PARSER_REVISION = 5


def anchor(stream: BinaryIO, offset: int) -> str:
    # SEEK_END invalidates BufferedReader's cached bytes before verifying an external rewrite.
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, offset - 512))
    result = hashlib.sha256(stream.read(min(offset, 512))).hexdigest()
    stream.seek(offset)
    return result


def _before_start(record: object, started_at: Instant) -> bool:
    if type(record) is not dict:
        return False
    stamp = cast(dict[str, object], record).get("timestamp")
    try:
        return parse_instant(cast(str, stamp)) < started_at
    except DomainError:
        # An unscoped snapshot cannot prove that it predates collection.
        return False


class CodexReader:
    def parse(self, record: object, context: CodexContext) -> Result:
        return parse_record(record, context)

    def visible_diagnostics(self, diagnostics: set[str], context: CodexContext) -> tuple[str, ...]:
        return tuple(
            d.code for d in filter_diagnostics((Diagnostic(code) for code in sorted(diagnostics)), context)
        )

    def __init__(self, *, line_limit: int = 32 * 1024**2, batch_limit: int = 64 * 1024**2) -> None:
        if not 0 < line_limit <= batch_limit:
            raise ValueError("Require 0 < line_limit <= batch_limit")
        self.line_limit = line_limit
        self.batch_limit = batch_limit

    def read(self, path: str, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch:
        try:
            with open_shared(path) as stream:
                return self._read(stream, checkpoint, started_at)
        except OSError:
            return ReadBatch(
                checkpoint.state or {}, diagnostics=("source_read_failed",), available=False, retry=True
            )

    def _read(self, stream: BinaryIO, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch:
        size = os.fstat(stream.fileno()).st_size
        snapshot_anchor = anchor(stream, size)
        offset, ctx, diagnostics = 0, CodexContext(), set(checkpoint.diagnostics)
        state = checkpoint.state
        if state is not None:
            try:
                offset = cast(int, state["offset"])
                saved_anchor = state["anchor"]
                context = state["context"]
                codes = state["diagnostics"]
                if (
                    type(offset) is not int
                    or offset < 0
                    or type(saved_anchor) is not str
                    or re.fullmatch("[0-9a-f]{64}", saved_anchor) is None
                    or type(context) is not dict
                    or type(codes) is not list
                    or any(type(code) is not str or not code for code in cast(list[object], codes))
                ):
                    raise ValueError("Invalid checkpoint")
                ctx = CodexContext.from_dict(cast(dict[str, object], context))
                diagnostics.update(cast(list[str], codes))
                if offset > size or anchor(stream, offset) != saved_anchor:
                    offset, ctx, diagnostics = 0, CodexContext(), {"source_rewritten"}
                elif state.get("parserRevision") != PARSER_REVISION:
                    offset, ctx, diagnostics = 0, CodexContext(), set[str]()
            except (KeyError, ValueError, DomainError):
                offset, ctx, diagnostics = 0, CodexContext(), {"invalid_checkpoint"}
        stream.seek(offset)
        events: list[UsageEvent] = []
        compacted: set[str] = set()
        consumed = 0
        more = False
        while offset < size:
            remaining = self.batch_limit - consumed
            # Peek one byte beyond the line limit, but never beyond the length snapshot.
            line = stream.readline(min(size - offset, self.line_limit + 1, remaining + 1))
            if len(line) > self.line_limit:
                diagnostics.add("source_line_too_large")
                break
            if len(line) > remaining:
                more = True
                break
            if not line.endswith(b"\n"):
                break
            record = decode(line)
            result = self.parse(record, ctx)
            key = compaction_key(record, ctx)
            if key is not None:
                compacted.add(key)
            if isinstance(result, UsageEvent) and parse_instant(result.occurred_at) >= started_at:
                events.append(result)
            elif isinstance(result, Diagnostic) and (
                result.code != "request_usage_format_unverified" or not _before_start(record, started_at)
            ):
                diagnostics.add(result.code)
            offset += len(line)
            consumed += len(line)
            if consumed >= self.batch_limit:
                more = offset < size
                break
        if os.fstat(stream.fileno()).st_size < size or anchor(stream, size) != snapshot_anchor:
            return ReadBatch(checkpoint.state or {}, diagnostics=("source_changed_during_read",), retry=True)
        current_anchor = anchor(stream, offset)
        new_state: dict[str, object] = {
            "parserRevision": PARSER_REVISION,
            "offset": offset,
            "anchor": current_anchor,
            "context": ctx.to_dict(),
            "diagnostics": sorted(diagnostics),
        }
        return ReadBatch(
            new_state,
            tuple(events),
            self.visible_diagnostics(diagnostics, ctx),
            more,
            compaction_keys=tuple(sorted(compacted)),
        )
