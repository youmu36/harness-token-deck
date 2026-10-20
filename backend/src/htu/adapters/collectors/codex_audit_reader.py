"""Stream a bounded source snapshot for audits using the production Codex parser."""

import os
from collections.abc import Generator
from dataclasses import dataclass
from typing import cast

from htu.adapters.collectors.codex_reader import anchor
from htu.adapters.collectors.json_lines import decode
from htu.adapters.collectors.shared_file import open_shared
from htu.domain.common.errors import DomainError
from htu.domain.common.time import Instant, parse_instant
from htu.domain.sources.codex import CodexContext, parse_record
from htu.domain.usage.model import Diagnostic, UsageEvent


@dataclass(frozen=True, slots=True)
class AuditItem:
    kind: str
    model: str | None
    reason: str
    event: UsageEvent | None = None


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if type(value) is dict else {}


def read_audit_file(
    path: str, start: Instant, end: Instant, *, line_limit: int = 32 * 1024**2
) -> Generator[AuditItem, None, None]:
    with open_shared(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        snapshot_anchor = anchor(stream, size)
        stream.seek(0)
        context, candidate = CodexContext(), CodexContext()
        rejection = "missing_session_meta"
        while stream.tell() < size:
            line = stream.readline(min(size - stream.tell(), line_limit + 1))
            if len(line) > line_limit:
                raise ValueError("source_line_too_large")
            if not line.endswith(b"\n"):
                break
            decoded = decode(line)
            record = _object(decoded)
            payload = _object(record.get("payload"))
            if type(decoded) is not dict:
                parse_record(decoded, context)
                parse_record(decoded, candidate)
                yield AuditItem(context.kind or "rejected", None, "invalid_json")
                continue
            first_meta = record.get("type") == "session_meta" and not context.saw_session_meta
            result = parse_record(record, context)
            if first_meta:
                rejection = "originator_unverified" if not context.originator_ok else "source_unverified"
                candidate = CodexContext.from_dict(context.to_dict())
                # Validate excluded usage for reporting; acceptance still comes from the real context.
                candidate.originator_ok, candidate.kind = True, "desktop"
            elif not context.accepted:
                if not candidate.saw_session_meta:
                    candidate.saw_session_meta = True
                    candidate.originator_ok, candidate.kind = True, "desktop"
                result = parse_record(record, candidate)
            kind = context.kind or "rejected"
            stamp = record.get("timestamp")
            try:
                in_range = start <= parse_instant(cast(str, stamp)) < end
            except DomainError:
                if record.get("type") == "token_usage_record":
                    yield AuditItem(kind, context.model_id or candidate.model_id, "timestamp_unscoped")
                continue
            if not in_range:
                continue
            if isinstance(result, Diagnostic) and record.get("type") != "token_usage_record":
                yield AuditItem(kind, context.model_id, result.code)
            if record.get("type") == "token_usage_record":
                if isinstance(result, UsageEvent):
                    reason = "accepted" if context.accepted else rejection
                    yield AuditItem(kind, result.model_id, reason, result)
                else:
                    reason = result.code if isinstance(result, Diagnostic) else rejection
                    yield AuditItem(kind, context.model_id or candidate.model_id, reason)
            elif record.get("type") == "event_msg" and payload.get("type") == "token_count":
                yield AuditItem(kind, context.model_id, "snapshot")
            elif record.get("type") == "event_msg" and payload.get("type") == "turn_aborted":
                yield AuditItem(kind, context.model_id, "interrupted_turn")
        if os.fstat(stream.fileno()).st_size < size or anchor(stream, size) != snapshot_anchor:
            raise ValueError("source_changed_during_read")
