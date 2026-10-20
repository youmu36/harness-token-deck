"""Page original daily spending and separate, traceable balance corrections."""

import sqlite3
from datetime import timedelta
from typing import Any, TypedDict, cast

from htu.adapters.sqlite import wallet_archive
from htu.adapters.sqlite.wallet_days import build as build_days
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.domain.common.time import Instant, format_instant

type DailySpending = dict[str, tuple[int, int | None, int | None]]


class _PageRow(TypedDict):
    seq: int
    day: str | None
    kind: str


class _ExactSpending:
    """SQLite callback keeps arbitrary-size amounts in Python integers throughout."""

    def __init__(self) -> None:
        self.amount = self.count = 0

    def step(self, amount: str, count: int) -> None:
        self.amount += int(amount)
        self.count += count

    def finalize(self) -> str:
        return f"{self.amount},{self.count}"


def daily_spending(
    db: sqlite3.Connection, vendor_id: str, offset_minutes: int, *, max_days: int = 8192
) -> DailySpending | None:
    """Rebuild display-only counters with exact integers, bounded independently of history size."""
    return {
        row["day"]: (row["seq"], int(row["original"]), row["count"])
        for row in db.execute("SELECT * FROM wallet_daily_close WHERE vendor_id=?", (vendor_id,))
    }


def wallet_entries(
    db: sqlite3.Connection,
    vendor_id: str,
    cursor: int | None,
    limit: int,
    kind: str,
    offset_minutes: int,
    *,
    daily: DailySpending | None = None,
) -> dict[str, object]:
    repo = WalletRepository(db)
    repo.vendor(vendor_id)
    if (
        daily is None
        and kind in ("all", "consumption_daily")
        and not db.execute("PRAGMA query_only").fetchone()[0]
    ):
        while not build_days(db, offset_minutes):
            pass
        daily = daily_spending(db, vendor_id, offset_minutes)
    # sqlite3's stub restricts callbacks to int -> int; SQLite also accepts text results.
    db.create_aggregate("htu_exact_spending", 2, cast(Any, _ExactSpending))
    offset = f"{offset_minutes:+d} minutes"
    cold = (
        {row["seq"]: row for row in wallet_archive.corrections(db, vendor_id, cursor or 2**63 - 1, limit + 1)}
        if kind in ("all", "adjustment")
        else {}
    )
    if daily is None and kind not in ("all", "consumption_daily"):
        daily = {}
    if daily is not None:
        rows: list[_PageRow] = [
            {"seq": seq, "day": day, "kind": "consumption_daily"}
            for day, (seq, _, _) in daily.items()
            if seq < (cursor or 2**63 - 1) and kind in ("all", "consumption_daily")
        ]
        rows.extend(
            _PageRow(seq=row["seq"], day=None, kind=row["kind"])
            for row in db.execute(
                "SELECT seq,NULL day,kind FROM wallet_activity WHERE vendor_id=? AND kind<>'consumption' "
                "AND seq<? AND (?='all' OR kind=?) ORDER BY seq DESC LIMIT ?",
                (vendor_id, cursor or 2**63 - 1, kind, kind, limit + 1),
            )
        )
        rows = sorted(rows, key=lambda row: row["seq"], reverse=True)[: limit + 1]
    else:
        selected = db.execute(
            "WITH entries AS (SELECT MIN(seq) seq,date(occurred_at,:offset) day,'consumption_daily' "
            "kind FROM wallet_activity "
            "WHERE vendor_id=:vendor AND kind='consumption' GROUP BY day UNION ALL "
            "SELECT seq,NULL,kind FROM wallet_activity WHERE vendor_id=:vendor AND kind<>'consumption') "
            "SELECT * FROM entries WHERE seq<:cursor AND (:kind='all' OR kind=:kind) ORDER BY seq "
            "DESC LIMIT :limit",
            {
                "vendor": vendor_id,
                "offset": offset,
                "cursor": cursor or 2**63 - 1,
                "kind": kind,
                "limit": limit + 1,
            },
        ).fetchall()
        rows = [_PageRow(seq=row["seq"], day=row["day"], kind=row["kind"]) for row in selected]
    daily_corrections = (
        {
            item["seq"]: item
            for item in db.execute(
                "SELECT * FROM wallet_daily_correction WHERE vendor_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
                (vendor_id, cursor or 2**63 - 1, limit + 1),
            )
        }
        if kind in ("all", "adjustment")
        else {}
    )
    rows.extend(_PageRow(seq=seq, day=None, kind="adjustment") for seq in daily_corrections)
    rows.extend(_PageRow(seq=seq, day=None, kind=row["kind"]) for seq, row in cold.items())
    rows = sorted(rows, key=lambda row: row["seq"], reverse=True)[: limit + 1]
    items: list[dict[str, object]] = []
    for row in rows[:limit]:
        if row["day"] is not None:
            from htu.adapters.sqlite.wallet_closing import bounds, timezone

            start, end = bounds(row["day"], timezone(db))
            # Indexed time bounds, exact Python integers, and minute projections:
            # pagination never rescans request vouchers for every selected day.
            if daily is not None and daily[row["day"]][1] is not None:
                _, amount, count = daily[row["day"]]
            else:
                total = db.execute(
                    "SELECT htu_exact_spending(amount_pico,count) FROM wallet_activity WHERE vendor_id=? AND "
                    "kind='consumption' AND occurred_at>=? AND occurred_at<?",
                    (vendor_id, format_instant(start), format_instant(end)),
                ).fetchone()[0]
                amount, count = map(int, (total or "0,0").split(","))
                if daily is not None:
                    daily[row["day"]] = row["seq"], amount, count
            assert amount is not None and count is not None
            corrected = db.execute(
                "SELECT COUNT(*) FROM wallet_activity WHERE vendor_id=? AND kind='adjustment' "
                "AND occurred_at>=? AND occurred_at<? AND note IN "
                "('calibration','recalc','late_offset') AND (note<>'calibration' OR seq>?)",
                (vendor_id, format_instant(start), format_instant(end), row["seq"]),
            ).fetchone()[0]
            corrected = corrected or wallet_archive.corrected(
                db, vendor_id, format_instant(start), format_instant(end)
            )
            items.append(
                {
                    "id": "local-day:" + vendor_id + ":" + row["day"],
                    "kind": "consumption_daily",
                    "seq": row["seq"],
                    "amountPico": str(amount),
                    "occurredAt": format_instant(Instant(end.value - timedelta(minutes=1))),
                    "count": count,
                    "note": "本地日估算（业务日 " + row["day"] + "，23:59 为展示标签；原额保留）",  # noqa: RUF001
                    "noteDetails": None,
                    "calibration": None,
                }
            )
        elif row["seq"] in daily_corrections:
            correction = daily_corrections[row["seq"]]
            items.append(
                {
                    "id": "local-day-correction:" + str(correction["id"]),
                    "kind": "adjustment",
                    "seq": row["seq"],
                    "amountPico": str(int(correction["before_pico"]) - int(correction["after_pico"])),
                    "occurredAt": correction["posted_at"],
                    "count": 1,
                    "note": (
                        f"本地日估算更正 {correction['day']}："  # noqa: RUF001
                        f"{correction['before_pico']} → {correction['after_pico']} pico（原额保留）"  # noqa: RUF001
                    ),
                    "noteDetails": None,
                    "calibration": None,
                }
            )
        else:
            entry = (
                cold.get(row["seq"])
                or db.execute("SELECT * FROM wallet_activity WHERE seq=?", (row["seq"],)).fetchone()
            )
            audit = db.execute("SELECT * FROM calibration WHERE entry_id=?", (entry["source_id"],)).fetchone()
            note = entry["note"]
            if entry["related_seq"] is not None:
                note = (
                    "历史费用重算修正"
                    if note == "recalc"
                    else "已校准消费的迟到抵消"
                    if note == "late_offset"
                    else note
                ) + f"(关联原消费凭证 #{entry['related_seq']}，原额保留)"
            items.append(
                {
                    "id": entry["source_id"],
                    "kind": entry["kind"],
                    "seq": entry["seq"],
                    "amountPico": entry["amount_pico"],
                    "occurredAt": entry["occurred_at"],
                    "count": entry["count"],
                    "note": note,
                    "calibration": None
                    if audit is None
                    else {
                        "targetPico": audit["target_pico"],
                        "observedBalancePico": audit["before_pico"],
                        "watermarkSeq": audit["watermark_seq"],
                    },
                }
            )
    return {
        "entries": items,
        "nextCursor": rows[limit - 1]["seq"] if len(rows) > limit else None,
        "revision": repo.totals(vendor_id).revision,
    }
