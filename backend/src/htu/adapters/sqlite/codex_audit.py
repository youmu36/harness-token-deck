"""Load pricing and Codex ledger rows from one read-only snapshot without migrations."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from htu.adapters.sqlite.key_codec import key_row
from htu.adapters.sqlite.pico_codec import decode_pico
from htu.adapters.sqlite.pricing_load import load_catalog
from htu.adapters.sqlite.usage_archive import ArchiveRow, archived_rows
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.pricing.catalog import Catalog
from htu.domain.usage.model import Harness, Tokens, UsageEvent


@dataclass(frozen=True, slots=True)
class AuditLedger:
    catalog: Catalog
    rows: dict[str, tuple[UsageEvent, int]]
    started_at: Instant | None
    compacted_records: int = 0
    compacted_pico: int = 0
    compacted_tokens: dict[str, int] = field(default_factory=dict[str, int])
    last_scan_at: Instant | None = None


def read_audit_ledger(path: Path, start: Instant, end: Instant) -> AuditLedger:
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.row_factory = key_row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        catalog = load_catalog(conn)
        rows: dict[str, tuple[UsageEvent, int]] = {}
        for row in conn.execute(
            "SELECT * FROM ledger WHERE harness='codex' AND occurred_at>=? AND occurred_at<?",
            (format_instant(start), format_instant(end)),
        ):
            event = UsageEvent(
                Harness.CODEX,
                row["dedupe_key"],
                row["session_id"],
                row["request_id"],
                row["model_id"],
                row["occurred_at"],
                Tokens(row["input"], row["cache_read"], row["cache_write"], row["output"], row["reasoning"]),
                "reported",
                source_kind=row["source_kind"],
            )
            pico = decode_pico(row["pico"])
            if pico < 0:
                raise ValueError("Negative ledger consumption")
            rows[event.dedupe_key] = (event, pico)
        source = conn.execute("SELECT started_at,last_scan_at FROM source WHERE harness='codex'").fetchone()
        started_at = parse_instant(source[0]) if source and source[0] else None
        compacted: list[sqlite3.Row | ArchiveRow] = []
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='usage_rollup'").fetchone():
            compacted = conn.execute(
                "SELECT * FROM usage_rollup WHERE harness='codex' AND bucket>=? AND bucket<?",
                (format_instant(start), format_instant(end)),
            ).fetchall()
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='usage_archive'").fetchone():
            compacted.extend(archived_rows(conn, format_instant(start), format_instant(end), "codex"))
        return AuditLedger(
            catalog,
            rows,
            started_at,
            sum(row["records"] for row in compacted),
            sum(decode_pico(row["pico"]) for row in compacted),
            {
                name: sum(row[name] for row in compacted)
                for name in ("input", "cache_read", "cache_write", "output", "reasoning")
            },
            parse_instant(source[1]) if source and source[1] else None,
        )
