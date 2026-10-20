"""Compact old requests into exact minute/vendor/model counters in one transaction."""

import sqlite3
from datetime import timedelta

from htu.adapters.sqlite.key_codec import key_bytes
from htu.adapters.sqlite.recalc_codec import compact_audits
from htu.adapters.sqlite.transaction import require_transaction
from htu.adapters.sqlite.usage_archive import archive_minutes, optimize_archives
from htu.adapters.sqlite.usage_hours import refresh_hours
from htu.adapters.sqlite.wallet_archive import compact as compact_finance
from htu.adapters.sqlite.wallet_rollup import compact_consumption
from htu.application.ports import UnitOfWork
from htu.domain.common.time import Instant, format_instant
from htu.domain.statistics.cache_warnings import cache_warnings


class RollupRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection

    def compact(self, work: UnitOfWork, *, at: Instant, limit: int = 500, publish_daily: bool = True) -> int:
        require_transaction(work, self.db)
        from htu.adapters.sqlite.wallet_closing import close_days

        close_days(work, self.db, at=at)
        cutoff = format_instant(Instant((at.value - timedelta(hours=72)).replace(second=0, microsecond=0)))
        unpriced_cutoff = format_instant(Instant(at.value - timedelta(days=30)))
        rows = self.db.execute(
            "SELECT l.*,COALESCE(w.vendor_id,'') vendor_id,a.compacted,a.cache_read detected_cache "
            "FROM ledger l LEFT JOIN usage_annotation a USING(harness,dedupe_key) LEFT JOIN wallet_entry w "
            "ON w.usage_harness=l.harness AND w.usage_key=l.dedupe_key AND w.kind='consumption' "
            "WHERE l.occurred_at<? AND (l.unpriced=0 OR l.occurred_at<?) "
            "ORDER BY l.occurred_at,l.harness,l.dedupe_key LIMIT ?",
            (cutoff, unpriced_cutoff, limit),
        ).fetchall()
        for row in rows:
            cache = row["cache_read"] if row["cache_read"] is not None else row["detected_cache"]
            input_tokens = row["input"]
            if row["cache_read"] is None and row["detected_cache"] is not None and input_tokens is not None:
                input_tokens += row["detected_cache"]
            unprobed = row["harness"] == "zcode" and "grok" in row["model_key"]
            rated = input_tokens is not None and cache is not None and (cache > 0 or not unprobed)
            bust, loss = cache_warnings(
                input_tokens,
                cache,
                session=bool(row["session_id"]),
                unprobed=unprobed,
                compacted=bool(row["compacted"]) or '"context_compacted"' in row["issues_json"],
            )
            key = (row["harness"], row["occurred_at"][:16] + ":00.000Z", row["vendor_id"], row["model_key"])
            values = [
                1,
                input_tokens or 0,
                cache or 0,
                row["output"] or 0,
                input_tokens if rated else 0,
                cache if rated else 0,
                int(input_tokens is not None and cache is None),
                int(input_tokens is None or row["output"] is None),
                int(row["model_id"] is None),
                row["incomplete"],
                row["unpriced"],
                (input_tokens or 0) if cache == 0 and not unprobed else 0,
                int(loss),
            ]
            old = self.db.execute(
                "SELECT * FROM usage_rollup WHERE harness=? AND bucket=? AND vendor_id=? AND model_key=?", key
            ).fetchone()
            columns = (
                "records",
                "input",
                "cache_read",
                "output",
                "rated_input",
                "rated_cache",
                "unknown_cache",
                "missing_usage",
                "unknown_model",
                "incomplete",
                "unpriced",
                "miss_input",
                "cache_loss",
            )
            if old:
                values = [
                    value + old[column]
                    if column not in ("incomplete", "unpriced", "cache_loss")
                    else int(bool(value or old[column]))
                    for column, value in zip(columns, values, strict=True)
                ]
            pico = str(int(row["pico"]) + (int(old["pico"]) if old else 0))
            self.db.execute(
                "INSERT OR REPLACE INTO usage_rollup VALUES (" + ",".join("?" for _ in range(24)) + ")",
                (
                    *key,
                    row["model_id"],
                    row["display_name"],
                    *values,
                    pico,
                    (row["cache_write"] or 0) + (old["cache_write"] if old else 0),
                    (row["reasoning"] or 0) + (old["reasoning"] if old else 0),
                    int(bust or (old["cache_bust"] if old else False)),
                    int(loss or (old["large_miss"] if old else False)),
                ),
            )
            compact_consumption(self.db, row["harness"], row["dedupe_key"])
            self.db.execute(
                "DELETE FROM ledger WHERE harness=? AND dedupe_key=?",
                (row["harness"], key_bytes(row["dedupe_key"])),
            )
        if rows:
            work.touch("usage")
            work.touch("sources")
        # Only active-session boundary state is needed; no request data survives here.
        self.db.execute(
            "DELETE FROM cache_anchor WHERE occurred_at<? AND NOT EXISTS "
            "(SELECT 1 FROM ledger l WHERE l.harness=cache_anchor.harness AND "
            "l.session_id=cache_anchor.session_id)",
            (format_instant(Instant(at.value - timedelta(days=6))),),
        )
        # Older retention versions could already have removed usage details while
        # leaving request vouchers. Compact those without altering financial totals.
        orphans = self.db.execute(
            "SELECT usage_harness,usage_key FROM wallet_entry w WHERE kind='consumption' "
            "AND occurred_at<? AND NOT EXISTS (SELECT 1 FROM "
            "wallet_validation_job j WHERE j.vendor_id=w.vendor_id) AND NOT "
            "EXISTS (SELECT 1 FROM ledger l "
            "WHERE l.harness=w.usage_harness AND l.dedupe_key=w.usage_key) LIMIT ?",
            (cutoff, limit - len(rows)),
        ).fetchall()
        for orphan in orphans:
            compact_consumption(self.db, orphan[0], orphan[1])
        count = len(rows) + len(orphans)
        count += archive_minutes(self.db, work, at=at, limit=limit - count) if count < limit else 0
        rebuilt = refresh_hours(self.db)
        optimized = optimize_archives(self.db)
        from htu.adapters.sqlite.usage_daily import publish

        daily = publish(self.db, work, at=at) if publish_daily else 0
        audits = compact_audits(self.db)
        finance = compact_finance(self.db, at=at, limit=limit)
        if optimized:
            work.touch("usage")
        pending = (
            self.db.execute("SELECT 1 FROM usage_hour_dirty LIMIT 1").fetchone()
            or self.db.execute(
                "SELECT 1 FROM usage_archive WHERE encoding<>2 OR model_key IS NULL LIMIT 1"
            ).fetchone()
            or self.db.execute("SELECT 1 FROM recalc_item WHERE payload IS NULL LIMIT 1").fetchone()
        )
        return 500 if pending or daily else max(count, rebuilt, optimized, audits, finance)
