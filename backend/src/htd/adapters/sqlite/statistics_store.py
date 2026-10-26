"""Read the usage revision and streamed rows from the same WAL snapshot."""

import json
import sqlite3
from collections import OrderedDict
from collections.abc import Generator
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from datetime import timedelta
from threading import Lock
from typing import Any, Literal
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.analysis_cache import AnalysisCache
from htd.adapters.sqlite.command_store import read_snapshot
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ledger_read import LedgerReader
from htd.adapters.sqlite.projection_snapshot import ProjectionSnapshot
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import StatisticsSnapshot, UnitOfWork
from htd.application.ports.query_control import QuerySlots, analysis_read, check_read, current_read
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.analysis import AnalysisQuery
from htd.domain.statistics.effective_cost import CostProjection
from htd.domain.statistics.model import LedgerRow
from htd.domain.usage.model import Harness


class SqliteStatisticsSnapshot:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.revision: int = db.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0]

    def costs(
        self, start: Instant, end: Instant, harness: Harness | Literal["all"], model_key: str | None
    ) -> CostProjection:
        from htd.adapters.sqlite.calibration_costs import projection

        return projection(
            self.db,
            {
                "start": format_instant(start),
                "end": format_instant(end),
                "harnesses": [] if harness == "all" else [harness.value],
                "modelKeys": [] if model_key is None else [model_key],
            },
        )

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
        self.analysis_cache = AnalysisCache()
        self.query_slots = QuerySlots()
        self._quality_lock = Lock()
        self._quality_claims: OrderedDict[tuple[str, str], int] = OrderedDict()
        self.monitor_cache = AnalysisCache(max_bytes=1024**2, capacity=2, compress=False)

    def monitor_data(
        self,
        harness: str,
        now: Instant,
        session_ref: str | None = None,
        offset_minutes: int = 0,
        cursor: str | None = None,
        chart_session_ref: str | None = None,
    ) -> dict[str, Any]:
        with analysis_read(None):
            for _ in range(3):
                try:
                    return self._monitor_data(
                        harness, now, session_ref, offset_minutes, cursor, chart_session_ref
                    )
                except DomainError as error:
                    if error.code not in ("READ_REPLAN", "READ_CANCELLED"):
                        raise
                    check_read()
            raise DomainError("READ_CHANGED", "会话数据持续变化，请重试", retryable=True)

    def _monitor_data(
        self,
        harness: str,
        now: Instant,
        session_ref: str | None,
        offset_minutes: int,
        cursor: str | None,
        chart_session_ref: str | None,
    ) -> dict[str, Any]:
        from htd.adapters.sqlite.monitor_revision import signature as monitor_signature
        from htd.adapters.sqlite.session_monitor import read

        at = now.value.timestamp()
        with read_snapshot(self.readers) as db:
            signature = monitor_signature(db, harness)
            revisions = {"usage": signature[0], "output": signature[1]}
        key = json.dumps(
            (
                harness,
                session_ref,
                chart_session_ref,
                offset_minutes,
                cursor,
                int(at // 60),
                signature,
            ),
            separators=(",", ":"),
        )
        reader = current_read.get()
        assert reader is not None
        generation = self.monitor_cache.generation
        future, owner = self.monitor_cache.claim(key, reader, at=at)
        try:
            if owner:
                token = current_read.set(self.monitor_cache.shared(future))
                try:
                    with self.query_slots.acquire(), read_snapshot(self.readers) as db:
                        current = monitor_signature(db, harness)
                        result = read(
                            db, harness, now, session_ref, offset_minutes, cursor, chart_session_ref
                        )
                        check_read()
                    deadline = (now.value.replace(second=0, microsecond=0) + timedelta(minutes=1)).timestamp()
                    for session in result.get("sessionHitRates", []):
                        boundary = parse_instant(session["lastActivityAt"]).value + timedelta(
                            minutes=20 if session["frozen"] else 10
                        )
                        deadline = min(deadline, boundary.timestamp())
                    self.monitor_cache.finish(
                        key,
                        future,
                        result,
                        generation if current == signature else -1,
                        expires_at=deadline,
                    )
                except BaseException as error:
                    self.monitor_cache.fail(key, future, error)
                    raise
                finally:
                    current_read.reset(token)
            while True:
                check_read()
                try:
                    projection = future.result(timeout=0.05)
                    if projection.expires_at is not None and at >= projection.expires_at:
                        # Followers must also respect a deadline established by an earlier owner.
                        raise DomainError("READ_REPLAN", "会话活动已跨过监控边界")
                    result = projection.materialize()
                    # Return committed envelope revisions from this reader's snapshot.
                    for name, scope in (("revision", "usage"), ("outputRevision", "output")):
                        if name in result:
                            result[name] = max(result[name], revisions.get(scope, 0))
                    return result
                except FutureTimeout:
                    continue
        finally:
            self.monitor_cache.release(future, reader)

    def analytics(self, query: AnalysisQuery, now: Instant) -> dict[str, object]:
        from htd.adapters.sqlite.analysis_read import analyze

        with read_snapshot(self.readers) as db:
            return analyze(db, query, now)

    def _explore_cached(
        self,
        query: dict[str, object],
        now: Instant,
    ) -> tuple[ProjectionSnapshot, int]:
        from htd.adapters.sqlite.explorer_read import explore
        from htd.adapters.sqlite.wallet_closing import timezone

        normalized: dict[str, Any] = dict(query)
        for name in (
            "harnesses",
            "vendorIds",
            "groupIds",
            "versionIds",
            "modelKeys",
            "sessionRefs",
            "activityBatchIds",
            "sections",
            "compareIds",
            "qualityReasons",
        ):
            if name in normalized:
                normalized[name] = sorted(set(normalized[name]))
        for name in ("queryId", "offset", "limit", "qualityOffset", "qualityStatus"):
            normalized.pop(name, None)
        with read_snapshot(self.readers) as db:
            from htd.adapters.sqlite.query_signature import signature as read_signature

            signature = read_signature(db, normalized, now)
            usage_revision = db.execute("SELECT value FROM revision WHERE scope='usage'").fetchone()[0]
            zone = timezone(db)
            clock_key = (
                int(now.value.timestamp() // 60)
                if query.get("costBasis") == "remote"
                else now.value.astimezone(ZoneInfo(zone)).date().isoformat()
            )
            key = json.dumps([normalized, signature, zone, clock_key], sort_keys=True)
        # No snapshot or heavy-read slot is held by callers joining an existing read.
        reader = current_read.get()
        assert reader is not None
        generation = self.analysis_cache.generation
        future, owner = self.analysis_cache.claim(key, reader)
        try:
            if owner:
                shared = self.analysis_cache.shared(future)
                token = current_read.set(shared)
                try:
                    with self.query_slots.acquire(), read_snapshot(self.readers) as db:
                        current_signature = read_signature(db, normalized, now)
                        full = explore(
                            db,
                            dict(normalized, offset=0, limit=5000, qualityOffset=0, internalFull=True),
                            now,
                        )
                        check_read()
                    self.analysis_cache.finish(
                        key, future, full, generation if current_signature == signature else -1
                    )
                except BaseException as error:
                    self.analysis_cache.fail(key, future, error)
                    raise
                finally:
                    current_read.reset(token)
            while True:
                check_read()
                try:
                    return future.result(timeout=0.05), usage_revision
                except FutureTimeout:
                    continue
        finally:
            self.analysis_cache.release(future, reader)

    def explore(self, query: dict[str, object], now: Instant) -> dict[str, object]:
        with analysis_read(None):
            return self._explore(query, now)

    def _explore(self, query: dict[str, object], now: Instant) -> dict[str, object]:
        while True:
            check_read()
            try:
                projection, usage_revision = self._explore_cached(query, now)
                result = projection.materialize(query)
                result["revision"] = max(usage_revision, result["revision"])
                result["snapshot"]["usage"] = result["revision"]
                break
            except DomainError as error:
                if error.code != "READ_CANCELLED":
                    raise
                # An owner's cancellation must not cancel a still-active follower.
                # Retry with a fresh snapshot; cancelled callers stop at this check.
                check_read()
        with self._quality_lock:
            for item in result["quality"]:
                identity = item["id"], item["fingerprint"]
                self._quality_claims[identity] = result["revision"]
                self._quality_claims.move_to_end(identity)
            while len(self._quality_claims) > 2000:
                self._quality_claims.popitem(last=False)
        return result

    def clear_analysis_cache(self) -> None:
        self.analysis_cache.clear()
        self.monitor_cache.clear()

    def monitor_diagnostics(self) -> dict[str, int]:
        with self.monitor_cache.lock:
            return {"entries": len(self.monitor_cache.entries), "estimatedBytes": self.monitor_cache.bytes}

    def expire_idle_cache(self) -> None:
        self.analysis_cache.expire_idle()
        self.monitor_cache.expire_idle()

    def explore_options(self, query: dict[str, object] | None = None) -> dict[str, object]:
        from htd.adapters.sqlite.analysis_options import options

        with read_snapshot(self.readers) as db:
            return options(db, query)

    def output_references(self, query: dict[str, object], now: Instant) -> dict[str, object]:
        from htd.adapters.sqlite.output_reference_read import read

        with read_snapshot(self.readers) as db:
            return read(db, query, now)

    def evidence(self, query: dict[str, object], now: Instant) -> dict[str, object]:
        from htd.adapters.sqlite.query_evidence import read

        with read_snapshot(self.readers) as db:
            return read(db, query, now)

    def acknowledge_quality(self, items: list[dict[str, Any]], now: Instant) -> dict[str, object]:
        from htd.adapters.sqlite.command_store import connection
        from htd.domain.common.errors import DomainError

        if self.writer is None:
            raise DomainError("UNAVAILABLE", "当前统计连接不可写")

        def commit(work: UnitOfWork) -> dict[str, object]:
            db = connection(work)
            acknowledged = conflicts = 0
            changed = False
            for item in items:
                with self._quality_lock:
                    issued_revision = self._quality_claims.get((item["id"], item["fingerprint"]))
                if issued_revision != work.revision("usage") or (
                    item.get("revision") is not None and item["revision"] != issued_revision
                ):
                    conflicts += 1
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO quality_ack_evidence VALUES(?,?,?)",
                    (item["id"], item["fingerprint"], format_instant(now)),
                )
                changed |= cursor.rowcount > 0
                acknowledged += 1
            if changed:
                from htd.application.ports import QualityChanged

                work.touch("quality")
                work.emit(QualityChanged(work.revision("quality")))
            return {"acknowledged": acknowledged, "conflicts": conflicts}

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
        from htd.adapters.sqlite.analysis_read import aggregate
        from htd.adapters.sqlite.wallet_closing import timezone

        with read_snapshot(self.readers) as db:
            zone = timezone(db)
            data = aggregate(
                db, AnalysisQuery(harness=harness), start, end, ZoneInfo(zone), "month", groups=False
            )
            total = data.total
            values = total.export()
            counts = total.counts
            return {
                "totals": {
                    "records": str(counts.records),
                    "input": str(counts.input),
                    "cache_read": str(counts.cache),
                    "output": str(counts.output),
                    "pico": str(counts.pico),
                    "rated_input": str(counts.rated_input),
                    "rated_cache": str(counts.rated_cache),
                    "missing_usage": str(counts.missing_usage),
                    "unknown_model": str(counts.unknown_model),
                    "estimatePico": str(total.estimate_pico),
                    "correctionPico": str(total.correction_pico),
                    "unallocatedCorrectionPico": str(total.unallocated_correction_pico),
                },
                "activeMinutes": values["activeMinutes"],
                "coveredDays": values["coveredDays"],
                "businessTimezone": zone,
                "includesWholeBoundaryDays": data.boundary,
                "missingCapabilities": sorted(total.missing),
                "basis": "本地用量与有效成本",
            }
