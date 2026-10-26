"""Read-only range reconciliation. Remote refresh times never establish local ownership."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.session_activity import complete_evidence
from htd.adapters.sqlite.usage_codec import object_json
from htd.adapters.sqlite.wallet_closing import timezone
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.activity import QUIET


def member_rows(
    db: sqlite3.Connection, query: dict[str, Any], *, with_owner: bool = False
) -> Iterator[sqlite3.Row]:
    selected = query.get("localBatchIds", [])
    clause = " AND m.batch_id IN (" + ",".join("?" for _ in selected) + ")" if selected else ""
    intervals = query.get("intervalIds", [])
    receipt_ids: list[str] = []
    if intervals:
        scopes = db.execute(
            "SELECT interval_id,start_at,end_at FROM reconciliation_interval_scope WHERE interval_id IN ("
            + ",".join("?" for _ in intervals)
            + ") ORDER BY start_at",
            intervals,
        ).fetchall()
        if len(scopes) == len(intervals) and scopes[0][1] == query["start"] and scopes[-1][2] == query["end"]:
            receipt_ids = list(intervals)
            clause += (
                " AND EXISTS(SELECT 1 FROM reconciliation_interval_member r WHERE r.harness=m.harness "
                "AND r.dedupe_key=m.dedupe_key AND r.interval_id IN ("
                + ",".join("?" for _ in receipt_ids)
                + "))"
            )
    frozen = query.get("memberTargetId")
    if frozen:
        clause += (
            " AND EXISTS(SELECT 1 FROM calibration_member c WHERE c.target_id=? "
            "AND c.harness=m.harness AND c.dedupe_key=m.dedupe_key)"
        )
    legacy = query.get("legacyFlowId")
    if legacy:
        clause += (
            " AND EXISTS(SELECT 1 FROM local_wallet_flow_override f WHERE f.flow_id=? "
            "AND m.voucher_seq BETWEEN f.source_first AND f.source_last AND (NOT EXISTS "
            "(SELECT 1 FROM wallet_flow_member w WHERE w.flow_id=f.flow_id) OR EXISTS "
            "(SELECT 1 FROM wallet_flow_member w WHERE w.flow_id=f.flow_id AND w.source_seq=m.voucher_seq)))"
        )
    source = (
        "SELECT m.*,o.target_id active_target FROM reconciliation_member m "
        "LEFT JOIN calibration_owner o USING(harness,dedupe_key)"
        if with_owner
        else "SELECT m.* FROM reconciliation_member m"
    )
    return iter(
        db.execute(
            source
            + " WHERE m.vendor_id=? AND m.occurred_at>=? AND m.occurred_at<?"
            + clause
            + " ORDER BY m.occurred_at,m.harness,m.dedupe_key",
            (
                query["vendorId"],
                query["start"],
                query["end"],
                *selected,
                *receipt_ids,
                *([frozen] if frozen else []),
                *([legacy] if legacy else []),
            ),
        )
    )


def remote_rows(db: sqlite3.Connection, vendor: str, ids: list[str]) -> list[sqlite3.Row]:
    if not ids:
        return []
    rows = db.execute(
        "SELECT i.*,s.vendor_id,s.identity,b.observed_at start_at,e.observed_at end_at,"
        "b.b start_b,e.b end_b,b.u start_u,e.u end_u,b.unit_version start_unit,"
        "e.unit_version end_unit FROM account_interval i JOIN account_segment s ON s.id=i.segment_id "
        "JOIN account_endpoint b ON b.id=i.start_endpoint JOIN account_endpoint e ON e.id=i.end_endpoint "
        "WHERE s.vendor_id=? AND i.id IN (" + ",".join("?" for _ in ids) + ") ORDER BY b.observed_at,i.id",
        (vendor, *ids),
    ).fetchall()
    if len(rows) != len(set(ids)):
        raise DomainError("INTERVAL_NOT_FOUND", "核对区间不存在或不属于所选供应商")
    return rows


def _remote(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> tuple[dict[str, Any], list[tuple[Any, ...]]]:
    postings = (
        [
            tuple(row)
            for row in db.execute(
                "SELECT id,interval_id,c,revision FROM account_posting WHERE interval_id IN ("
                + ",".join("?" for _ in rows)
                + ") ORDER BY id",
                tuple(row["id"] for row in rows),
            )
        ]
        if rows
        else []
    )
    valid = bool(rows) and len({row["identity"] for row in rows}) == 1
    valid &= len({row["start_unit"] for row in rows} | {row["end_unit"] for row in rows}) == 1
    used = 0
    amounts: dict[str, int] = {}
    for posting in postings:
        amounts[posting[1]] = amounts.get(posting[1], 0) + int(posting[2])
    local: dict[str, tuple[str | None, str | None, int]] = {}
    scopes: dict[str, tuple[str | None, str]] = {}
    estimates: dict[str, int] = {}
    if rows:
        local = {
            row[0]: (row[1], row[2], row[3])
            for row in db.execute(
                "SELECT i.interval_id,MIN(m.occurred_at),MAX(m.received_at),COUNT(DISTINCT m.session_ref) "
                "FROM reconciliation_interval_member i JOIN reconciliation_member m "
                "USING(harness,dedupe_key) "
                "WHERE i.interval_id IN (" + ",".join("?" for _ in rows) + ") GROUP BY i.interval_id",
                tuple(row["id"] for row in rows),
            )
        }
        scopes = {
            row[0]: (row[1], row[2])
            for row in db.execute(
                "SELECT interval_id,start_at,end_at FROM reconciliation_interval_scope WHERE interval_id IN ("
                + ",".join("?" for _ in rows)
                + ")",
                tuple(row["id"] for row in rows),
            )
        }
        for member in db.execute(
            "SELECT r.interval_id,m.estimate_pico FROM reconciliation_interval_member r "
            "JOIN reconciliation_member m USING(harness,dedupe_key) WHERE r.interval_id IN ("
            + ",".join("?" for _ in rows)
            + ")",
            tuple(row["id"] for row in rows),
        ):
            estimates[member[0]] = estimates.get(member[0], 0) + int(member[1])
    reliable = valid
    for row in rows:
        usable = row["start_u"] is not None and row["end_u"] is not None
        delta = int(row["end_u"]) - int(row["start_u"]) if usable else 0
        reliable &= usable and delta >= 0 and row["method"] == "remote_usage"
        if usable and delta < 0:
            valid = False
        used += delta
    return {
        "remoteTargetPico": str(sum(int(row[2]) for row in postings)) if rows else None,
        "remoteUsedDeltaPico": str(used) if reliable else None,
        "bookedEstimatePico": str(sum(int(row["e"]) for row in rows)),
        "reliability": "missing"
        if not rows
        else "invalid"
        if not valid
        else "reliable_usage"
        if reliable
        else "balance_estimate",
        "remoteIntervals": [
            {
                "id": row["id"],
                "start": row["start_at"],
                "end": row["end_at"],
                "revision": row["revision"],
                "method": row["method"],
                "unitVersion": row["end_unit"],
                "consumptionPico": str(amounts.get(row["id"], 0)),
                "estimatePico": str(estimates.get(row["id"], 0)) if row["id"] in scopes else row["e"],
                "localStart": scopes[row["id"]][0]
                if row["id"] in scopes
                else local.get(row["id"], (None, None, 0))[0],
                "localEnd": scopes[row["id"]][1]
                if row["id"] in scopes
                else format_instant(Instant(parse_instant(str(local[row["id"]][1])).value + QUIET))
                if row["id"] in local and local[row["id"]][1]
                else None,
                "sessionCount": local.get(row["id"], (None, None, 0))[2],
            }
            for row in rows
        ],
    }, postings


def preview(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, Any]:
    vendor = query["vendorId"]
    start, end = query["start"], query["end"]
    if parse_instant(end) <= parse_instant(start):
        raise DomainError("RANGE_INVALID", "核对结束时间须晚于开始时间")
    if (
        not query.get("memberTargetId")
        and not query.get("legacyFlowId")
        and parse_instant(start).value < at.value - timedelta(days=3)
    ):
        raise DomainError("CALIBRATION_EXPIRED", "仅支持校准过去三天的消费事务")
    if vendor and db.execute("SELECT 1 FROM vendor WHERE id=?", (vendor,)).fetchone() is None:
        raise DomainError("VENDOR_NOT_FOUND", "供应商不存在")
    selected_batches = set(query.get("localBatchIds", []))
    if selected_batches and db.execute(
        "SELECT COUNT(*) FROM reconciliation_local_batch WHERE vendor_id=? AND id IN ("
        + ",".join("?" for _ in selected_batches)
        + ")",
        (vendor, *selected_batches),
    ).fetchone()[0] != len(selected_batches):
        raise DomainError("BATCH_NOT_FOUND", "本地批次不存在或不属于所选供应商")
    digest = hashlib.sha256()
    count = original = estimate = effective = incomplete = unassigned = 0
    sources: set[str] = set()
    models: set[str] = set()
    groups: set[str] = set()
    batches: set[str] = set()
    sessions: set[tuple[str, str]] = set()
    zone = ZoneInfo(timezone(db))
    days: set[str] = set()
    last_received = ""
    for row in member_rows(db, query):
        days.add(parse_instant(row["occurred_at"]).value.astimezone(zone).date().isoformat())
        count += 1
        original += int(row["original_pico"])
        estimate += int(row["estimate_pico"])
        effective += int(row["effective_pico"])
        incomplete += bool(row["incomplete"] or row["unpriced"])
        sources.add(row["harness"])
        models.add(row["model_key"])
        groups.add(row["group_id"])
        batches.add(row["batch_id"])
        if row["session_ref"]:
            sessions.add((row["harness"], row["session_ref"]))
        else:
            unassigned += 1
        last_received = max(last_received, row["received_at"])
        digest.update(json.dumps(tuple(row), ensure_ascii=True).encode())
    member_digest = digest.hexdigest()
    pending = 0
    if sources:
        pending = db.execute(
            "SELECT COUNT(*) FROM session_request WHERE state='accepted' AND occurred_at>=? "
            "AND occurred_at<? AND harness IN (" + ",".join("?" for _ in sources) + ")",
            (start, end, *sources),
        ).fetchone()[0]
    backfill = db.execute("SELECT value FROM runtime_state WHERE key='reconciliation_backfill'").fetchone()
    backfill_complete = bool(backfill and object_json(backfill[0])["done"])
    complete = (
        bool(count)
        and not unassigned
        and backfill_complete
        and pending == 0
        and incomplete == 0
        and all(complete_evidence(db, harness, ref) for harness, ref in sessions)
    )
    quiet = bool(last_received) and at.value - parse_instant(last_received).value >= QUIET
    # Old manual flows are independent evidence; never divide them among member models.
    overrides: list[tuple[Any, ...]] = []
    partial_override = False
    zone = ZoneInfo(timezone(db))
    first_day = parse_instant(start).value.astimezone(zone).date().isoformat()
    last_day = parse_instant(end).value.astimezone(zone).date().isoformat()
    for row in db.execute(
        "SELECT * FROM local_wallet_flow_override o WHERE vendor_id=? AND day>=? AND day<=? "
        "AND NOT EXISTS(SELECT 1 FROM calibration_legacy_replacement r WHERE r.flow_id=o.flow_id)",
        (vendor, first_day, last_day),
    ):
        flow = db.execute("SELECT c,estimate_c FROM wallet_flow WHERE id=?", (row["flow_id"],)).fetchone()
        candidates = [
            item
            for item in db.execute(
                "SELECT batch_id,occurred_at,estimate_pico FROM reconciliation_member "
                "WHERE vendor_id=? AND voucher_seq BETWEEN ? AND ? AND (NOT EXISTS "
                "(SELECT 1 FROM wallet_flow_member f WHERE f.flow_id=?) OR EXISTS "
                "(SELECT 1 FROM wallet_flow_member f WHERE f.flow_id=? AND f.source_seq=voucher_seq))",
                (vendor, row["source_first"], row["source_last"], row["flow_id"], row["flow_id"]),
            )
            if parse_instant(item["occurred_at"]).value.astimezone(zone).date().isoformat() == row["day"]
        ]
        included = [
            item
            for item in candidates
            if start <= item["occurred_at"] < end
            and (not selected_batches or item["batch_id"] in selected_batches)
        ]
        if not included:
            continue
        overrides.append(
            (row["flow_id"], row["source_first"], row["source_last"], tuple(flow) if flow else None)
        )
        if flow and len(included) == len(candidates):
            effective += int(flow[0]) - sum(int(item["estimate_pico"]) for item in candidates)
        else:
            partial_override = True
    remote = remote_rows(db, vendor, query.get("intervalIds", []))
    comparison, postings = _remote(db, remote)
    shifted = (
        bool(remote)
        and start
        == format_instant(Instant(parse_instant(remote[0]["start_at"]).value + timedelta(milliseconds=1)))
        and end
        == format_instant(Instant(parse_instant(remote[-1]["end_at"]).value + timedelta(milliseconds=1)))
    )
    aligned = bool(remote) and ((remote[0]["start_at"] == start and remote[-1]["end_at"] == end) or shifted)
    aligned &= all(before["end_at"] == after["start_at"] for before, after in pairwise(remote))
    receipts = comparison["remoteIntervals"]
    receipt_aligned = (
        bool(receipts) and receipts[0]["localStart"] == start and receipts[-1]["localEnd"] == end
    )
    if receipt_aligned:
        aligned = all(before["localEnd"] == after["localStart"] for before, after in pairwise(receipts))
    account_members = db.execute(
        "SELECT COUNT(*) FROM reconciliation_member WHERE vendor_id=? AND occurred_at>=? AND occurred_at<? "
        "AND received_at<=?",
        (vendor, start, end, end),
    ).fetchone()[0]
    omitted = account_members - count
    if omitted:
        aligned = False
    edge = db.execute(
        "SELECT 1 FROM reconciliation_member WHERE vendor_id=? AND occurred_at IN (?,?) LIMIT 1",
        (vendor, start, end),
    ).fetchone()
    if edge is not None and not shifted and not receipt_aligned:
        aligned = False  # Account endpoints are (start,end]; local selection is [start,end).
    independent = [
        tuple(row)
        for row in db.execute(
            "SELECT p.id,p.c,p.revision FROM account_posting p JOIN account_segment s ON s.id=p.segment_id "
            "WHERE p.vendor_id=? AND s.kind='remote' AND p.interval_id IS NULL AND p.c<>'0' "
            "AND p.occurred_at>=? AND p.occurred_at<? ORDER BY p.id",
            (vendor, start, end),
        )
    ]
    snapshot = dict(
        db.execute("SELECT scope,value FROM revision WHERE scope IN ('usage','wallet','account','sources')")
    )
    payload = [
        query,
        member_digest,
        [tuple(row) for row in remote],
        postings,
        independent,
        overrides,
        complete,
        quiet,
    ]
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    notices: list[str] = []
    if not complete:
        notices.append("本地输入、质量或来源覆盖尚未完整，不能据此证明漏采。")
    if not backfill_complete:
        notices.append("旧账本成员正在有界回填，当前覆盖尚未完整。")
    if not quiet:
        notices.append("所选本地成员尚未静默十分钟。")
    if not aligned:
        notices.append("远程区间与所选本地成员尚未证明完整覆盖，时间接近不代表同一笔消费。")
    if comparison["reliability"] == "balance_estimate":
        notices.append("远程消费借助余额和本地估算推算，不是独立核实金额。")
    if overrides:
        notices.append("范围涉及已有人工流水校准，采用新目标前须明确替换范围。")
    if partial_override:
        notices.append("部分覆盖旧人工金额或原流水已退出页面，当前局部费用仅显示可确认估算，校准未分配。")
    from htd.adapters.sqlite.calibration_costs import adjustments

    overlay = adjustments(db, query)
    effective += int(overlay["deltaPico"])
    return {
        "vendorId": vendor,
        "start": start,
        "end": end,
        "businessTimezone": timezone(db),
        "memberDays": sorted(days),
        "memberCount": str(count),
        "memberDigest": member_digest,
        "batchIds": sorted(batches),
        "originalEstimatePico": str(original),
        "currentEstimatePico": str(estimate),
        "currentEffectivePico": str(effective),
        "existingCorrectionPico": str(effective - estimate),
        "pendingCount": str(pending),
        "incompleteCount": str(incomplete),
        "harnesses": sorted(sources),
        "modelKeys": sorted(models),
        "groupIds": sorted(groups),
        "localComplete": complete,
        "quiet": quiet,
        "coverageAligned": aligned,
        "omittedMemberCount": str(omitted),
        "auxiliaryIncluded": False,
        "externalUsagePossible": True,
        "legacyOverrides": len(overrides),
        "legacyFlowIds": [row[0] for row in overrides],
        "legacyAllocationIncomplete": partial_override,
        "unallocatedCorrectionPico": overlay["unallocatedDeltaPico"],
        "allocationIncomplete": overlay["allocationIncomplete"],
        "independentCorrectionPico": str(sum(int(row[1]) for row in independent)),
        "fingerprint": fingerprint,
        "snapshot": snapshot,
        "notices": notices,
        **comparison,
    }


def batches(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, Any]:
    vendor, start, end = query["vendorId"], query["start"], query["end"]
    if parse_instant(end) <= parse_instant(start):
        raise DomainError("RANGE_INVALID", "核对结束时间须晚于开始时间")
    remote_offset = query.get("remoteOffset", 0)
    start = max(start, format_instant(Instant(at.value - timedelta(days=3))))
    remote = db.execute(
        "SELECT i.id FROM account_interval i JOIN account_segment s ON s.id=i.segment_id "
        "JOIN reconciliation_interval_scope c ON c.interval_id=i.id "
        "WHERE s.vendor_id=? AND c.completed_at>=? AND c.completed_at<=? "
        "AND (c.start_at IS NULL OR c.start_at>=?) AND EXISTS(SELECT 1 FROM account_posting p "
        "JOIN wallet_flow f ON f.vendor_id=p.vendor_id AND f.scope='remote' AND f.kind='consumption' "
        "AND p.rowid BETWEEN f.source_first AND f.source_last WHERE p.interval_id=i.id) "
        "ORDER BY COALESCE(c.start_at,c.completed_at) DESC,c.completed_at DESC,i.id DESC LIMIT 101 OFFSET ?",
        (vendor, start, end, start, remote_offset),
    ).fetchall()
    intervals, _ = _remote(db, remote_rows(db, vendor, [row[0] for row in remote[:100]]))
    return {
        "batches": [],
        "nextOffset": None,
        "remoteNextOffset": remote_offset + 100 if len(remote) > 100 else None,
        "remoteIntervals": sorted(
            intervals["remoteIntervals"],
            key=lambda row: (row["localStart"] or row["start"], row["end"], row["id"]),
            reverse=True,
        ),
        "generatedAt": format_instant(at),
    }
