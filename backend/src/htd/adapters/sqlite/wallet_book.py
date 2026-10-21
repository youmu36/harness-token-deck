"""Monthly reading of existing daily closes and remote interval facts, no token scans."""

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.wallet_closing import timezone
from htd.adapters.sqlite.wallet_inheritance import movements, seed
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant


def read(db: sqlite3.Connection, vendor: str, scope: str, month: str, at: Instant) -> dict[str, Any]:
    try:
        first = datetime.fromisoformat(month + "-01").replace(tzinfo=ZoneInfo(timezone(db)))
        last = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    except (ValueError, OverflowError) as error:
        raise DomainError("ACCOUNT_MONTH_INVALID", "记账月份无效") from error
    start, end = format_instant(Instant(first.astimezone(UTC))), format_instant(Instant(last.astimezone(UTC)))
    zone = first.tzinfo
    today = at.value.astimezone(zone).date().isoformat()
    days: dict[str, list[int]] = {}

    def add(stamp: str, r: str, c: str, a: str) -> None:
        day = parse_instant(stamp).value.astimezone(zone).date().isoformat()
        if day[:7] != month or day >= today:
            return
        if not (int(r) or int(c)):
            return
        target = days.setdefault(day, [0, 0, 0])
        for index, value in enumerate((r, c, a)):
            if index < 2:
                target[index] += int(value)

    inherited = seed(db, vendor)
    if inherited:
        query, args = movements(db, vendor, scope)
        for row in db.execute(
            "SELECT occurred_at,r,c,a FROM (" + query + ") WHERE occurred_at>=? AND occurred_at<?",
            (*args, start, end),
        ):
            add(row["occurred_at"], row["r"], row["c"], row["a"])
    elif scope == "local":
        # Closes are already indexed by vendor/day. Dirty closes are refreshed in
        # bounded maintenance batches; current-day estimates remain provisional.
        for row in db.execute(
            "SELECT day,current FROM wallet_daily_close WHERE vendor_id=? AND day>=? "
            "AND day<? AND timezone=?",
            (vendor, month + "-01", last.date().isoformat(), timezone(db)),
        ):
            if row["day"] < today:
                days.setdefault(row["day"], [0, 0, 0])[1] += int(row["current"])
        for row in db.execute(
            "SELECT occurred_at,CASE WHEN kind='top_up' THEN amount_pico ELSE '0' END r,"
            "'0' c,CASE WHEN kind='adjustment' THEN amount_pico ELSE '0' END a FROM wallet_entry "
            "WHERE vendor_id=? AND occurred_at>=? AND occurred_at<? AND kind IN ('top_up','adjustment') "
            "UNION ALL SELECT occurred_at,r,c,a FROM local_wallet_posting WHERE vendor_id=? "
            "AND occurred_at>=? AND occurred_at<?",
            (vendor, start, end, vendor, start, end),
        ):
            add(row["occurred_at"], row["r"], row["c"], row["a"])
    else:
        # Keep cross-day intervals whole on their ending business day. This is
        # explicit booking, not fabricated daily provider bills or token shares.
        for row in db.execute(
            "SELECT i.*,e.observed_at FROM account_interval i JOIN account_segment s ON s.id=i.segment_id "
            "JOIN account_endpoint e ON e.id=i.end_endpoint WHERE s.vendor_id=? AND s.kind='remote' "
            "AND e.observed_at>=? AND e.observed_at<?",
            (vendor, start, end),
        ):
            add(row["observed_at"], row["r"], row["c"], row["a"])
        for row in db.execute(
            "SELECT p.* FROM account_posting p JOIN account_segment s ON s.id=p.segment_id "
            "WHERE p.vendor_id=? AND s.kind='remote' AND p.occurred_at>=? AND p.occurred_at<? AND "
            "(p.kind IN ('correction','consumption_correction','balance_correction') OR "
            "(p.kind='manual_top_up' AND p.interval_id IS NULL))",
            (vendor, start, end),
        ):
            add(row["occurred_at"], row["r"], row["c"], row["a"])
    pending = scope == "local" and (
        db.execute(
            "SELECT 1 FROM wallet_daily_dirty WHERE vendor_id=? AND day>=? AND day<? AND day<? LIMIT 1",
            (vendor, month + "-01", last.date().isoformat(), today),
        ).fetchone()
        is not None
        or not db.execute("SELECT ready FROM wallet_daily_backfill WHERE id=1").fetchone()[0]
    )
    hint = (
        db.execute(
            "SELECT h.* FROM remote_usage_hint h JOIN remote_account r ON r.id=h.account_id "
            "WHERE r.vendor_id=?",
            (vendor,),
        ).fetchone()
        if scope == "remote"
        else None
    )
    return {
        "days": [
            {"day": day, **dict(zip("RCA", map(str, values), strict=True))}
            for day, values in sorted(days.items(), reverse=True)
        ],
        **dict(zip("RCA", (str(sum(v[i] for v in days.values())) for i in range(3)), strict=True)),
        "pending": bool(pending),
        "notice": "仅汇总已结束自然日的有效充值与消费，校准计回原日期。远程跨日区间记在结束日。"
        if scope == "remote"
        else "仅汇总已结束自然日的有效充值与消费，含重算和校准后的金额。未结或待回填日期可稍后更新。",
        "revision": db.execute("SELECT value FROM revision WHERE scope='wallet'").fetchone()[0],
        "providerTodayPico": hint["today_pico"] if hint else None,
        "providerTotalPico": hint["total_pico"] if hint else None,
        "providerObservedAt": hint["observed_at"] if hint else None,
    }
