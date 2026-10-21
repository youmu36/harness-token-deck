"""Read-only Zcode database snapshots; source SQL stays in the SQLite adapter."""

import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import cast

from htd.application.ports import CheckpointRecord, ReadBatch
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant
from htd.domain.sources.zcode import ZcodeCheckpoint, ZcodeRow, advance, classify_status, normalize_row
from htd.domain.usage.model import Diagnostic

_BASE = (
    "id",
    "session_id",
    "logical_request_id",
    "attempt_index",
    "model_id",
    "status",
    "completed_at",
    "raw_usage_json",
)
_NATIVE = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "provider_id",
    "started_at",
    "first_token_at",
    "duration_ms",
    "time_to_first_token_ms",
)


def read_zcode(path: str, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch:
    state = ZcodeCheckpoint()
    diagnostics = set(checkpoint.diagnostics)
    if checkpoint.state is not None:
        try:
            state = ZcodeCheckpoint.from_dict(checkpoint.state)
        except DomainError:
            diagnostics.add("invalid_checkpoint")
    try:
        uri = Path(path).absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=2)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=2000")
            connection.execute("BEGIN")
            try:
                probe = connection.execute("SELECT rowid,* FROM model_usage LIMIT 0")
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                return ReadBatch(
                    checkpoint.state or {},
                    diagnostics=("source_schema_unsupported",),
                    available=False,
                    retry=True,
                )
            columns = {column[0] for column in probe.description}
            if not set(("rowid", *_BASE)) <= columns:
                return ReadBatch(
                    checkpoint.state or {},
                    diagnostics=("source_schema_unsupported",),
                    available=False,
                    retry=True,
                )
            selected = "rowid," + ",".join((*_BASE, *(c for c in _NATIVE if c in columns)))

            def row_value(row: sqlite3.Row) -> ZcodeRow:
                values = {column: row[column] for column in (*_BASE, *_NATIVE) if column in columns}
                return ZcodeRow(row["rowid"], cast(str, values.pop("id")), **values)

            if state.row_id:
                tail = connection.execute(
                    "SELECT id FROM model_usage WHERE rowid=?", (state.row_id,)
                ).fetchone()
                if tail is None or tail[0] != state.tail_id:
                    state = ZcodeCheckpoint()
                    diagnostics = {"source_rewritten"}
            saved = checkpoint.state or {}
            raw_quarantined = saved.get("quarantined", [])
            if not isinstance(raw_quarantined, list) or any(
                type(value) is not int or value <= 0 for value in cast(list[object], raw_quarantined)
            ):
                raise ValueError("Invalid quarantined checkpoint")
            quarantined = set(cast(list[int], raw_quarantined))
            source_path = Path(path)
            signatures: list[list[int] | None] = []
            for candidate in (source_path, Path(str(source_path) + "-wal")):
                try:
                    stat = candidate.stat()
                    signatures.append([stat.st_size, stat.st_mtime_ns])
                except OSError:
                    signatures.append(None)
            raw_retry = saved.get("quarantineRetryAt", 0)
            retry_at = float(raw_retry) if isinstance(raw_retry, (int, float)) else 0.0
            recheck = signatures != saved.get("quarantineSignature") and time.time() >= retry_at
            if recheck:
                state = replace(state, pending=tuple(sorted(set(state.pending) | quarantined)))
            pending: dict[int, ZcodeRow | None] = {}
            for rowid in state.pending:
                row = connection.execute(
                    f"SELECT {selected} FROM model_usage WHERE rowid=?", (rowid,)
                ).fetchone()
                pending[rowid] = row_value(row) if row is not None else None
            rows = connection.execute(
                f"SELECT {selected} FROM model_usage WHERE rowid>? ORDER BY rowid LIMIT 500", (state.row_id,)
            ).fetchall()
            batch = advance(state, pending, [row_value(row) for row in rows], started_at)
            diagnostics.update(d.code for d in batch.diagnostics)
            for row in [*pending.values(), *(row_value(row) for row in rows)]:
                if row is None:
                    continue
                classification = classify_status(row.status, row.completed_at)
                if classification == "unsupported" or (
                    classification == "terminal" and isinstance(normalize_row(row), Diagnostic)
                ):
                    quarantined.add(row.rowid)
                else:
                    quarantined.discard(row.rowid)
            if len(quarantined) > 2000:
                diagnostics.add("source_quarantine_limit")
            output = batch.checkpoint.to_dict()
            output.update(
                quarantined=sorted(quarantined)[:2000],
                quarantineSignature=signatures if recheck else saved.get("quarantineSignature"),
                quarantineRetryAt=time.time() + 60 if recheck else retry_at,
            )
            return ReadBatch(output, batch.events, tuple(sorted(diagnostics)), batch.more)
    except sqlite3.Error as error:
        code = (
            "source_database_busy"
            if getattr(error, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            else "source_database_unavailable"
        )
        return ReadBatch(checkpoint.state or {}, diagnostics=(code,), available=False, retry=True)
    except (DomainError, ValueError, OSError):
        return ReadBatch(
            checkpoint.state or {}, diagnostics=("source_schema_unsupported",), available=False, retry=True
        )
