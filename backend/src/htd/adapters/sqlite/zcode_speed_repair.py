"""One bounded upgrade repair of recent Zcode speed evidence; never rebill usage."""

import json
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from htd.adapters.sqlite.command_store import connection, read_snapshot
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.session_state import session_key
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import OutputChanged, UnitOfWork
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.sources.zcode import ZcodeRow, normalize_row
from htd.domain.usage.model import OUTPUT_TIMING_UNCERTAIN, UsageEvent
from htd.domain.usage.model_key import model_key

KEY = "zcode_speed_timing_repair_043"


def install(db: sqlite3.Connection, at: Instant | None = None) -> None:
    if db.execute("SELECT 1 FROM runtime_state WHERE key=?", (KEY,)).fetchone():
        return
    now = at or Instant(datetime.now(UTC))
    cutoff = format_instant(Instant(now.value - timedelta(hours=72)))
    sessions = [
        r[0]
        for r in db.execute(
            "SELECT DISTINCT session_key FROM output_session WHERE harness='zcode' AND started_at>=?",
            (cutoff,),
        )
    ]
    last = db.execute("SELECT MAX(billed_at),MAX(occurred_at) FROM ledger WHERE harness='zcode'").fetchone()
    for hashed in sessions:
        reset_session(db, hashed)
    state = {
        "done": not bool(sessions and last[0]),
        "cursor": 0,
        "reset": sessions,
        "cutoff": cutoff,
        "billedCutoff": last[0],
        "end": last[1],
    }
    db.execute("INSERT INTO runtime_state VALUES(?,?)", (KEY, json.dumps(state)))


def reset_session(db: sqlite3.Connection, hashed: str) -> None:
    from htd.adapters.sqlite.output_day import withdraw

    for row in db.execute(
        "SELECT * FROM output_session_day WHERE harness='zcode' AND session_key=?", (hashed,)
    ).fetchall():
        withdraw(db, row)
    for table in ("output_session", "output_session_day", "output_session_scope"):
        db.execute(f"DELETE FROM {table} WHERE harness='zcode' AND session_key=?", (hashed,))


def repair(readers: ReadPool, writer: WriteExecutor, at: Instant) -> int:
    with read_snapshot(readers) as db:
        saved = db.execute("SELECT value FROM runtime_state WHERE key=?", (KEY,)).fetchone()
        if saved is None:
            return 0
        state = json.loads(saved[0])
        if state["done"]:
            return 0
        source = db.execute("SELECT path FROM source WHERE harness='zcode'").fetchone()
        if source is None:
            return 0
        path = source[0]
    try:
        with closing(
            sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        ) as native:
            native.row_factory = sqlite3.Row
            native.execute("PRAGMA query_only=ON")
            native.execute("BEGIN")
            deadline = time.monotonic() + 0.5
            native.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            cutoff = parse_instant(state["cutoff"]).value
            until = parse_instant(state["end"]).value
            fields = set(ZcodeRow.__dataclass_fields__)
            available = {r[1] for r in native.execute("PRAGMA table_info(model_usage)")}
            columns = "rowid," + ",".join(sorted((fields & available) - {"rowid"}))
            rows = native.execute(
                "SELECT " + columns + " FROM model_usage WHERE rowid>? AND completed_at>=? AND "
                "completed_at<=? ORDER BY rowid LIMIT 500",
                (state["cursor"], int(cutoff.timestamp() * 1000), int(until.timestamp() * 1000)),
            ).fetchall()
            fields = set(ZcodeRow.__dataclass_fields__)
            events: list[UsageEvent] = []
            for row in rows:
                values = {key: value for key, value in dict(row).items() if key in fields}
                result = normalize_row(ZcodeRow(**values))
                if isinstance(result, UsageEvent):
                    events.append(result)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return 0

    def commit(work: UnitOfWork) -> int:
        db = connection(work)
        current = db.execute("SELECT value FROM runtime_state WHERE key=?", (KEY,)).fetchone()
        if current is None or current[0] != saved[0]:
            return 0
        reset = set(state["reset"])
        samples: dict[str, list[tuple[str, int | None, int | None, str | None]]] = defaultdict(list)
        display: dict[str, str] = {}
        accepted = 0
        for event in sorted(events, key=lambda item: item.occurred_at):
            old = db.execute(
                "SELECT * FROM ledger WHERE harness='zcode' AND dedupe_key=?", (key_bytes(event.dedupe_key),)
            ).fetchone()
            if (
                old is None
                or old["billed_at"] > state["billedCutoff"]
                or old["occurred_at"] != event.occurred_at
                or old["model_id"] != event.model_id
                or any(
                    old[column] != getattr(event.tokens, field)
                    for column, field in (
                        ("input", "input"),
                        ("cache_read", "cache_read"),
                        ("cache_write", "cache_write"),
                        ("output", "output"),
                        ("reasoning", "reasoning"),
                    )
                )
            ):
                continue
            assert event.session_id is not None
            hashed = session_key(event.session_id)
            if hashed not in reset:
                continue
            key = model_key(event.model_id) if event.model_id else ""
            display[key] = event.model_id or "模型未知"
            samples[key].append(
                (
                    event.occurred_at,
                    None if OUTPUT_TIMING_UNCERTAIN in event.issues else event.tokens.output,
                    event.generation_milliseconds,
                    event.session_id,
                )
            )
            accepted += 1
        from htd.adapters.sqlite.output_speed import record

        for key, values in samples.items():
            record(
                db,
                harness="zcode",
                model_key=key,
                display_name=display[key],
                output=None,
                occurred_at=values[-1][0],
                at=at,
                samples=[(a, b, c) for a, b, c, _ in values],
                session_ids=[sid for _, _, _, sid in values],
            )
        state.update(cursor=rows[-1]["rowid"] if rows else state["cursor"], done=len(rows) < 500)
        if state["done"]:
            state["reset"] = []
        db.execute("UPDATE runtime_state SET value=? WHERE key=?", (json.dumps(state), KEY))
        if accepted:
            work.touch("output")
            work.emit(OutputChanged(work.revision("output")))
        return accepted

    return writer.execute(commit)
