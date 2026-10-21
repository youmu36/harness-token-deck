"""Read the usage revision and streamed rows from the same WAL snapshot."""

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import timedelta
from typing import Literal

from htd.adapters.sqlite.command_store import read_snapshot
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ledger_read import LedgerReader
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import StatisticsSnapshot, UnitOfWork
from htd.domain.common.time import Instant, format_instant
from htd.domain.statistics.analysis import AnalysisQuery
from htd.domain.statistics.model import LedgerRow
from htd.domain.usage.model import Harness


class SqliteStatisticsSnapshot:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.revision: int = db.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0]

    def has_daily_history(self, start: Instant, end: Instant, harness: Harness | Literal["all"]) -> bool:
        clause, args = ("", ()) if harness == "all" else (" AND harness=?", (harness.value,))
        return (
            self.db.execute(
                "SELECT 1 FROM usage_daily WHERE end_at>? AND start_at<?" + clause + " LIMIT 1",
                (format_instant(start), format_instant(end), *args),
            ).fetchone()
            is not None
        )

    def signature(
        self, start: Instant, end: Instant, harness: Harness | Literal["all"]
    ) -> tuple[tuple[str, str, int], ...]:
        clause, args = ("", ()) if harness == "all" else (" AND harness=?", (harness.value,))
        return tuple(
            tuple(row)
            for row in self.db.execute(
                "SELECT harness,day,value FROM usage_day_revision WHERE day>=? AND day<=?"
                + clause
                + " ORDER BY harness,day",
                (
                    start.value.date().isoformat(),
                    (end.value - timedelta(microseconds=1)).date().isoformat(),
                    *args,
                ),
            )
        )

    def rows(
        self,
        start: Instant,
        end: Instant,
        harness: Harness | Literal["all"],
        *,
        model_key: str | None = None,
        coarse: bool = False,
    ) -> Generator[LedgerRow, None, None]:
        if coarse:
            return LedgerReader(self.db).iter_rows(
                start, end, harness=harness, model_key=model_key, coarse=True
            )
        if model_key is None:
            return LedgerReader(self.db).iter_rows(start, end, harness=harness)
        return LedgerReader(self.db).iter_rows(start, end, harness=harness, model_key=model_key)


class SqliteStatisticsStore:
    def __init__(self, readers: ReadPool, writer: WriteExecutor | None = None) -> None:
        self.readers = readers
        self.writer = writer

    def monitor_data(self, harness: str, now: Instant) -> dict[str, list[dict[str, object]]]:
        from htd.adapters.sqlite.output_speed import live
        from htd.adapters.sqlite.session_hit_rate import read

        with read_snapshot(self.readers) as db:
            sessions = read(db, harness, now)
            return {"outputSpeeds": live(db, harness, now, sessions), "sessionHitRates": sessions}

    def analytics(self, query: AnalysisQuery, now: Instant) -> dict[str, object]:
        from htd.adapters.sqlite.analysis_read import analyze

        with read_snapshot(self.readers) as db:
            return analyze(db, query, now)

    def explore(self, query: dict[str, object], now: Instant) -> dict[str, object]:
        from htd.adapters.sqlite.explorer_read import explore

        with read_snapshot(self.readers) as db:
            return explore(db, query, now)

    def explore_options(self) -> dict[str, object]:
        from htd.adapters.sqlite.explorer_read import options

        with read_snapshot(self.readers) as db:
            return options(db)

    def output_references(self, query: dict[str, object], now: Instant) -> dict[str, object]:
        from htd.adapters.sqlite.output_reference_read import read

        with read_snapshot(self.readers) as db:
            return read(db, query, now)

    def acknowledge_quality(self, items: list[dict[str, str]], now: Instant) -> dict[str, object]:
        from htd.adapters.sqlite.command_store import connection
        from htd.domain.common.errors import DomainError

        if self.writer is None:
            raise DomainError("UNAVAILABLE", "当前统计连接不可写")

        def commit(work: UnitOfWork) -> dict[str, object]:
            db = connection(work)
            for item in items:
                db.execute(
                    "INSERT INTO quality_notice_ack VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "fingerprint=excluded.fingerprint,acknowledged_at=excluded.acknowledged_at",
                    (item["id"], item["fingerprint"], format_instant(now)),
                )
            return {"acknowledged": len(items)}

        return self.writer.execute(commit)

    @contextmanager
    def snapshot(self) -> Generator[StatisticsSnapshot, None, None]:
        with read_snapshot(self.readers) as db:
            yield SqliteStatisticsSnapshot(db)

    def auxiliary(self, start: Instant, end: Instant, harness: str) -> dict[str, object]:
        with read_snapshot(self.readers) as db:
            groups: dict[str | None, list[int]] = {}
            if harness in ("all", "codex"):
                for row in db.execute(
                    "SELECT model_id,cost_pico FROM title_request WHERE occurred_at>=? AND occurred_at<?",
                    (format_instant(start), format_instant(end)),
                ):
                    values = groups.setdefault(row["model_id"], [0, 0])
                    values[0] += 1
                    values[1] += int(row["cost_pico"])
            return {
                "costPico": str(sum(value[1] for value in groups.values())),
                "models": [
                    {
                        "modelId": model,
                        "purpose": "生成会话标题",
                        "count": value[0],
                        "costPico": str(value[1]),
                        "tokens": None,
                    }
                    for model, value in sorted(groups.items(), key=lambda item: item[0] or "")
                ],
                "revision": db.execute("SELECT value FROM revision WHERE scope='auxiliary'").fetchone()[0],
            }

    def long_term(self, start: Instant, end: Instant, harness: str) -> dict[str, object]:
        from htd.adapters.sqlite.usage_daily import exact_summary

        with read_snapshot(self.readers) as db:
            return exact_summary(db, format_instant(start), format_instant(end), harness)
