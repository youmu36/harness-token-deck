"""Account-wide C and live E comparison; never change financial vouchers on reads."""

import sqlite3
from bisect import bisect_left
from collections import defaultdict
from typing import Any

from htd.adapters.sqlite.token_activity import QUIET, account_state
from htd.adapters.sqlite.wallet_archive import unpack
from htd.adapters.sqlite.wallet_closing import timezone
from htd.application.ports.query_control import check_read
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.remote.accounting import CENT
from htd.domain.statistics.explorer import ExploreTotals


def remote_explore(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, Any]:
    if any(query.get(name) for name in ("harnesses", "groupIds", "versionIds", "modelKeys", "compareIds")):
        raise DomainError("SCOPE_UNSUPPORTED", "远程消费仅支持供应商与账户区间，请清除来源、模型及价格组筛选")
    message = "远程消费按完整账户区间记在结束日期，独立更正按发生日期展示，不分摊到来源、模型或倍率版本。"
    snapshot = dict(
        db.execute("SELECT scope,value FROM revision WHERE scope IN ('usage','account','quality','pricing')")
    )
    return {
        "start": query["start"],
        "end": query["end"],
        "generatedAt": format_instant(at),
        "businessTimezone": timezone(db),
        "grain": query.get("grain", "day"),
        "totals": ExploreTotals().export(),
        "models": [],
        "trend": [],
        "breakdowns": [],
        "comparisons": [],
        "totalModels": 0,
        "nextOffset": None,
        "missingVersionRecords": "0",
        "notices": [message],
        "coverage": [{"code": "account_scope", "message": message}],
        "revision": snapshot["usage"],
        "snapshot": snapshot,
        "costBasis": "remote",
        "quality": [],
        "qualityTotal": 0,
        "qualityUnacknowledged": 0,
        "qualityNextOffset": None,
        **read_accounts(db, query, at),
    }


def current_estimates(db: sqlite3.Connection, intervals: list[sqlite3.Row]) -> dict[str, tuple[int, bool]]:
    by_vendor: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in intervals:
        by_vendor[row["vendor_id"]].append(row)
    result: dict[str, tuple[int, bool]] = {}
    for vendor, rows in by_vendor.items():
        boundaries = sorted({row[field] for row in rows for field in ("start_at", "end_at")})
        totals, incomplete = [0] * len(boundaries), [0] * len(boundaries)

        def add(
            stamp: str,
            amount: str | int,
            bad: bool,
            boundaries: list[str] = boundaries,
            totals: list[int] = totals,
            incomplete: list[int] = incomplete,
        ) -> None:
            check_read()
            index = bisect_left(boundaries, stamp)
            if index < len(boundaries):
                totals[index] += int(amount)
                incomplete[index] += int(bad)

        args = vendor, boundaries[0], boundaries[-1]
        for row in db.execute(
            "SELECT w.occurred_at,w.amount_pico,l.unpriced,l.incomplete "
            "FROM wallet_entry w LEFT JOIN ledger l ON l.harness=w.usage_harness "
            "AND l.dedupe_key=w.usage_key WHERE w.vendor_id=? AND w.occurred_at>? "
            "AND w.occurred_at<=? AND w.kind IN ('consumption','consumption_delta')",
            args,
        ):
            add(row[0], row[1], bool(row[2] or row[3]))
        for row in db.execute(
            "SELECT bucket,consumption,unpriced_count,incomplete_count FROM wallet_rollup "
            "WHERE vendor_id=? AND bucket>? AND bucket<=?",
            args,
        ):
            add(row[0], row[1], bool(row[2] or row[3]))
        for block in db.execute(
            "SELECT * FROM wallet_archive WHERE vendor_id=? AND end_at>? AND start_at<=? AND rollup_count>0",
            args,
        ):
            for row in unpack(block)["rollup"]:
                if boundaries[0] < row["bucket"] <= boundaries[-1]:
                    add(
                        row["bucket"],
                        row["consumption"],
                        bool(row["unpriced_count"] or row["incomplete_count"]),
                    )
        for index in range(1, len(boundaries)):
            totals[index] += totals[index - 1]
            incomplete[index] += incomplete[index - 1]
        for row in rows:
            begin, end = (bisect_left(boundaries, row[field]) for field in ("start_at", "end_at"))
            amount = totals[end] - totals[begin]
            result[row["id"]] = amount, amount > 0 and incomplete[end] == incomplete[begin]
    return result


def read_accounts(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, Any]:
    vendors = sorted(set(query.get("vendorIds", [])))
    clause = " AND s.vendor_id IN (" + ",".join("?" for _ in vendors) + ")" if vendors else ""
    args = query["start"], query["end"], *vendors
    base = (
        " FROM account_interval i JOIN account_segment s ON s.id=i.segment_id "
        "JOIN account_endpoint b ON b.id=i.start_endpoint JOIN account_endpoint t ON t.id=i.end_endpoint "
        "WHERE s.kind='remote' AND t.observed_at>=? AND t.observed_at<?" + clause
    )
    count = db.execute("SELECT COUNT(*)" + base, args).fetchone()[0]
    total = 0
    # Posting C is the effective contribution, including explicit corrections.
    for row in db.execute(
        "SELECT p.c FROM account_posting p JOIN account_interval i ON i.id=p.interval_id "
        "JOIN account_segment s ON s.id=i.segment_id "
        "JOIN account_endpoint t ON t.id=i.end_endpoint "
        "WHERE s.kind='remote' AND t.observed_at>=? AND t.observed_at<?" + clause,
        args,
    ):
        check_read()
        total += int(row[0])
    offset = query.get("accountOffset", 0)
    intervals = db.execute(
        "SELECT i.*,s.vendor_id,b.observed_at start_at,t.observed_at end_at"
        + base
        + " ORDER BY t.observed_at DESC,i.id DESC LIMIT 100 OFFSET ?",
        (*args, offset),
    ).fetchall()
    estimates = current_estimates(db, intervals)
    effective_costs: dict[str, int] = defaultdict(int)
    if intervals:
        for row in db.execute(
            "SELECT interval_id,c FROM account_posting WHERE interval_id IN ("
            + ",".join("?" for _ in intervals)
            + ")",
            [row["id"] for row in intervals],
        ):
            effective_costs[row[0]] += int(row[1])
    states: dict[str, tuple[str | None, bool]] = {}
    result: list[dict[str, Any]] = []
    for row in intervals:
        check_read()
        vendor = row["vendor_id"]
        current, complete = estimates[row["id"]]
        effective = effective_costs[row["id"]]
        if vendor not in states:
            states[vendor] = account_state(db, vendor)
        last, busy = states[vendor]
        stable = not busy and (last is None or at.value - parse_instant(last).value >= QUIET)
        difference = effective - current
        status = (
            "collecting"
            if not stable
            else "estimated"
            if row["method"] != "remote_usage"
            else "coverage_incomplete"
            if not complete
            else "review"
            if difference > CENT
            else "matched"
        )
        result.append(
            {
                "id": row["id"],
                "vendorId": vendor,
                "start": row["start_at"],
                "end": row["end_at"],
                "method": row["method"],
                "originalCostPico": row["c"],
                "costPico": str(effective),
                "bookedEstimatePico": row["e"],
                "currentEstimatePico": str(current),
                "differencePico": str(difference),
                "status": status,
                "coverageComplete": complete,
            }
        )
    correction_clause = clause.replace("s.vendor_id", "p.vendor_id")
    correction_base = (
        " FROM account_posting p JOIN account_segment s ON s.id=p.segment_id "
        "WHERE s.kind='remote' AND p.interval_id IS NULL AND p.c<>'0' "
        "AND p.occurred_at>=? AND p.occurred_at<?" + correction_clause
    )
    correction_count = db.execute("SELECT COUNT(*)" + correction_base, args).fetchone()[0]
    correction_total = 0
    for row in db.execute("SELECT p.c" + correction_base, args):
        check_read()
        correction_total += int(row[0])
    correction_offset = query.get("accountCorrectionOffset", 0)
    corrections = [
        {
            "id": row["id"],
            "vendorId": row["vendor_id"],
            "occurredAt": row["occurred_at"],
            "kind": row["kind"],
            "costPico": row["c"],
            "note": row["note"],
        }
        for row in db.execute(
            "SELECT p.*" + correction_base + " ORDER BY p.occurred_at DESC,p.id DESC LIMIT 100 OFFSET ?",
            (*args, correction_offset),
        )
    ]
    return {
        "accounts": result,
        "accountTotal": count,
        "remoteCostPico": str(total + correction_total),
        "intervalCostPico": str(total),
        "unallocatedCostPico": str(correction_total),
        "accountCorrections": corrections,
        "accountCorrectionTotal": correction_count,
        "accountCorrectionNextOffset": correction_offset + 100
        if correction_offset + 100 < correction_count
        else None,
        "accountNextOffset": offset + 100 if offset + 100 < count else None,
    }
