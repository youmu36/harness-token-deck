"""Stream ordered ledger projections and expose observed models and retained source counts."""

import heapq
import json
import re
import sqlite3
from collections.abc import Generator
from dataclasses import replace
from datetime import timedelta
from typing import Literal

from htd.adapters.sqlite.counter_codec import unpack
from htd.adapters.sqlite.usage_archive import ArchiveRow, archived_rows
from htd.application.ports import SourceCounts
from htd.domain.common.errors import PICO_INVALID, DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.model import LedgerRow, RollupCounts
from htd.domain.statistics.window import validate_range
from htd.domain.usage.model import Harness

_PICO = re.compile(r"0|[1-9][0-9]*")


def counter_row(row: sqlite3.Row | ArchiveRow) -> LedgerRow:
    return LedgerRow(
        Harness(row["harness"]),
        # The projection key is stable before and after archival.
        json.dumps([row["vendor_id"], row["model_key"]], separators=(",", ":"), ensure_ascii=False),
        None,
        parse_instant(row["bucket"]),
        row["model_id"],
        row["input"],
        row["cache_read"],
        row["output"],
        int(row["pico"]),
        bool(row["incomplete"]),
        bool(row["unpriced"]),
        row["display_name"],
        RollupCounts(
            row["records"],
            row["rated_input"],
            row["rated_cache"],
            row["unknown_cache"],
            row["missing_usage"],
            row["unknown_model"],
            row["miss_input"],
            bool(row["cache_loss"]),
            bool(row["cache_bust"]),
            bool(row["large_miss"]),
        ),
        latest_at=(
            parse_instant(row["latest_bucket"]) if isinstance(row, dict) and "latest_bucket" in row else None
        ),
    )


def _harness_filter(harness: Harness | Literal["all"]) -> tuple[str, tuple[str, ...]]:
    return ("", ()) if harness == "all" else (" AND harness=?", (Harness(harness).value,))


class LedgerReader:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def _details(
        self,
        start: Instant,
        end: Instant,
        *,
        harness: Harness | Literal["all"] = "all",
        model_key: str | None = None,
        validate: bool = True,
    ) -> Generator[LedgerRow, None, None]:
        if validate:
            validate_range(start, end)
        clause, args = _harness_filter(harness)
        if model_key is not None:
            clause += " AND model_key=?"
            args += (model_key,)
        cursor = self.connection.execute(
            "SELECT harness,dedupe_key,session_id,occurred_at,model_id,"
            "display_name,CASE WHEN ledger.cache_read IS NULL AND a.cache_read IS NOT NULL "
            "THEN ledger.input+a.cache_read ELSE ledger.input END input,"
            "COALESCE(ledger.cache_read,a.cache_read) cache_read,output,pico,incomplete,unpriced,"
            "issues_json,COALESCE(a.compacted,0) compacted FROM ledger "
            "LEFT JOIN usage_annotation a USING(harness,dedupe_key) "
            "WHERE occurred_at>=? AND occurred_at<?"
            + clause
            + " ORDER BY harness,COALESCE(session_id,''),occurred_at,dedupe_key",
            (format_instant(start), format_instant(end), *args),
        )
        # Bulk statistics only need this one identity decoded. Keep the other
        # columns on SQLite's native row path instead of copying every row.
        cursor.row_factory = sqlite3.Row
        try:
            for row in cursor:
                pico = row["pico"]
                if _PICO.fullmatch(pico) is None or len(pico) > 40:
                    raise DomainError(PICO_INVALID, "账本金额格式损坏", "pico")
                yield LedgerRow(
                    Harness(row["harness"]),
                    row["dedupe_key"].hex() if isinstance(row["dedupe_key"], bytes) else row["dedupe_key"],
                    row["session_id"],
                    parse_instant(row["occurred_at"]),
                    row["model_id"],
                    row["input"],
                    row["cache_read"],
                    row["output"],
                    int(pico),
                    bool(row["incomplete"]),
                    bool(row["unpriced"]),
                    row["display_name"],
                    context_compacted=bool(row["compacted"]) or '"context_compacted"' in row["issues_json"],
                )
        finally:
            cursor.close()

    def _rollups(
        self,
        start: Instant,
        end: Instant,
        harness: Harness | Literal["all"],
        model_key: str | None = None,
        coarse: bool = False,
    ) -> Generator[LedgerRow, None, None]:
        clause, args = _harness_filter(harness)
        if model_key is not None:
            clause += " AND model_key=?"
            args += (model_key,)
        ready = ""
        first = start.value.replace(minute=0, second=0, microsecond=0)
        if first < start.value:
            first += timedelta(hours=1)
        hour_start = format_instant(Instant(first))
        hour_end = format_instant(Instant(end.value.replace(minute=0, second=0, microsecond=0)))
        if coarse:
            ready = (
                " AND NOT EXISTS(SELECT 1 FROM usage_hour h WHERE h.harness=usage_rollup.harness "
                "AND h.bucket=substr(usage_rollup.bucket,1,13)||':00:00.000Z' AND h.bucket>=? "
                "AND h.bucket<? AND NOT EXISTS "
                "(SELECT 1 FROM usage_hour_dirty d WHERE d.harness=h.harness AND d.bucket=h.bucket))"
            )
        cursor = self.connection.execute(
            "SELECT *,json_array(vendor_id,model_key) synthetic_key FROM "
            "usage_rollup WHERE bucket>=? AND bucket<?"
            + clause
            + ready
            + " ORDER BY harness,bucket,synthetic_key",
            (
                format_instant(start),
                format_instant(end),
                *args,
                *((hour_start, hour_end) if coarse else ()),
            ),
        )
        cursor.row_factory = sqlite3.Row
        try:
            for row in cursor:
                yield counter_row(row)
        finally:
            cursor.close()
        if coarse:
            hour_clause, hour_args = _harness_filter(harness)
            for block in self.connection.execute(
                "SELECT payload FROM usage_hour h WHERE bucket>=? AND bucket<?"
                + hour_clause
                + " AND payload<>''"
                + " AND NOT EXISTS(SELECT 1 FROM usage_hour_dirty d WHERE "
                "d.harness=h.harness AND d.bucket=h.bucket) ORDER BY harness,bucket",
                (hour_start, hour_end, *hour_args),
            ):
                for row in json.loads(block[0]):
                    if model_key is None or row["model_key"] == model_key:
                        yield counter_row(row)
            if model_key is not None:
                hour_clause += " AND model_key=?"
                hour_args += (model_key,)
            for block in self.connection.execute(
                "SELECT payload FROM usage_hour_model h WHERE bucket>=? AND bucket<?"
                + hour_clause
                + " AND NOT EXISTS(SELECT 1 FROM usage_hour_dirty d WHERE "
                "d.harness=h.harness AND d.bucket=h.bucket) ORDER BY harness,bucket,model_key,part",
                (hour_start, hour_end, *hour_args),
            ):
                for row in unpack(block[0]):
                    yield counter_row(row)

    def analysis_rows(
        self, start: Instant, end: Instant, harness: Harness | Literal["all"], model_key: str | None
    ) -> Generator[LedgerRow, None, None]:
        """Fine minute evidence for the separately validated long-range analysis contract."""
        yield from self._details(start, end, harness=harness, model_key=model_key, validate=False)
        yield from self._rollups(start, end, harness, model_key)
        for counter in archived_rows(
            self.connection, format_instant(start), format_instant(end), str(harness), model_key=model_key
        ):
            yield counter_row(counter)

    def iter_rows(
        self,
        start: Instant,
        end: Instant,
        *,
        harness: Harness | Literal["all"] = "all",
        model_key: str | None = None,
        coarse: bool = False,
    ) -> Generator[LedgerRow, None, None]:
        details = self._details(start, end, harness=harness, model_key=model_key)
        rollups = self._rollups(start, end, harness, model_key, coarse)
        try:
            yield from heapq.merge(details, rollups, key=lambda row: row.order_key)
            for row in archived_rows(
                self.connection,
                format_instant(start),
                format_instant(end),
                str(harness),
                model_key=model_key,
                coarse=coarse,
            ):
                yield counter_row(row)
            from htd.adapters.sqlite.usage_daily import rows as daily_rows

            for _, counter in daily_rows(
                self.connection, format_instant(start), format_instant(end), str(harness), model_key
            ):
                projected = counter_row(counter)
                yield replace(projected, occurred_at=max(start, projected.occurred_at), daily_history=True)
        finally:
            details.close()
            rollups.close()

    def observed_models(self, *, harness: Harness | Literal["all"] = "all") -> Generator[str, None, None]:
        clause, args = _harness_filter(harness)
        cursor = self.connection.execute(
            "SELECT model_id FROM (SELECT model_id,model_key,ROW_NUMBER() OVER "
            "(PARTITION BY model_key ORDER BY occurred_at DESC,harness DESC,COALESCE(session_id,'') DESC,"
            "dedupe_key DESC) n FROM (SELECT "
            "harness,model_id,model_key,occurred_at,session_id,dedupe_key FROM ledger "
            "UNION ALL SELECT "
            "harness,model_id,model_key,bucket,NULL,json_array(vendor_id,model_key) FROM usage_rollup "
            "UNION ALL SELECT a.harness,json_extract(j.value,'$.model_id'),"
            "json_extract(j.value,'$.model_key'),json_extract(j.value,'$.bucket'),NULL,"
            "COALESCE(json_extract(j.value,'$.tie_key'),CAST(a.id AS TEXT)) "
            "FROM usage_archive a,json_each(a.models_json) j UNION ALL SELECT "
            "harness,model_id,model_key,start_at,NULL,model_key FROM usage_daily) "
            "WHERE model_id IS NOT NULL" + clause + ") WHERE n=1 ORDER BY model_key",
            args,
        )
        try:
            for row in cursor:
                yield row[0]
        finally:
            cursor.close()

    def source_counts(self) -> tuple[SourceCounts, ...]:
        rows = self.connection.execute(
            "WITH h(harness) AS (VALUES('claude'),('codex'),('zcode'),('dsh')) "
            "SELECT h.harness,(SELECT COUNT(*) FROM identity d WHERE d.harness=h.harness) identities,"
            "(SELECT COUNT(*) FROM identity d WHERE d.harness=h.harness AND billed=1) processed,"
            "(SELECT COUNT(*) FROM inbox i WHERE i.harness=h.harness) pending,"
            "(SELECT COUNT(*) FROM ledger l WHERE l.harness=h.harness) + "
            "COALESCE((SELECT SUM(records) FROM usage_rollup r WHERE r.harness=h.harness),0) + "
            "COALESCE((SELECT SUM(records) FROM usage_archive a WHERE a.harness=h.harness),0) ledger FROM h"
        ).fetchall()
        counts = {
            row["harness"]: SourceCounts(
                Harness(row["harness"]), row["identities"], row["processed"], row["pending"], row["ledger"]
            )
            for row in rows
        }
        for record in self.connection.execute("SELECT harness,records FROM usage_daily"):
            h = Harness(record["harness"])
            previous = counts[h.value]
            counts[h.value] = SourceCounts(
                h,
                previous.identity_count,
                previous.processed_count,
                previous.inbox_count,
                previous.ledger_count + int(record["records"]),
            )
        return tuple(counts[h.value] for h in Harness)
