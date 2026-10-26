"""One frozen-target projection for statistics, reconciliation and effective financial costs."""

import json
import sqlite3
from typing import Any

from htd.application.ports.query_control import check_read
from htd.domain.common.errors import DomainError
from htd.domain.statistics.effective_cost import CostBucket, CostProjection


def projection(db: sqlite3.Connection, query: dict[str, Any]) -> CostProjection:
    query = dict(query)
    if query.get("activityBatchIds"):
        identities = query["activityBatchIds"]
        query["localBatchIds"] = [
            row[0]
            for row in db.execute(
                "SELECT id FROM reconciliation_local_batch WHERE activity_id IN ("
                + ",".join("?" for _ in identities)
                + ")",
                identities,
            )
        ] or ["missing-activity-coverage"]
    vendors = query.get("vendorIds", []) or ([query["vendorId"]] if query.get("vendorId") else [])
    clause = " AND t.vendor_id IN (" + ",".join("?" for _ in vendors) + ")" if vendors else ""
    buckets: list[CostBucket] = []
    revision = db.execute("SELECT value FROM calibration_revision WHERE id=1").fetchone()[0]
    for row in db.execute(
        "SELECT b.*,t.vendor_id,EXISTS(SELECT 1 FROM calibration_scope_count c WHERE c.bucket_id=b.id "
        "AND dimension='eligibility' AND value='known' AND count>0) known_basis,"
        "EXISTS(SELECT 1 FROM calibration_scope_count c WHERE c.bucket_id=b.id "
        "AND dimension='eligibility' AND value='unavailable' AND count>0) invalid_basis "
        "FROM calibration_bucket b JOIN calibration_target t ON t.id=b.target_id "
        "WHERE t.state='active' AND b.coverage_start<? AND b.coverage_end>?" + clause,
        (query["end"], query["start"], *vendors),
    ):
        check_read()
        if len(buckets) >= 10000:
            raise DomainError("ANALYSIS_LIMIT", "校准分组超过读取预算，请缩小范围")
        buckets.append(
            CostBucket(
                row["id"],
                row["target_id"],
                row["coverage_start"],
                row["coverage_end"],
                row["day"],
                int(row["delta_pico"]),
                int(row["target_pico"]),
                int(row["estimate_pico"]),
                row["vendor_id"],
                json.loads(row["scope_json"]),
                bool(row["model_allocated"]),
                bool(row["known_basis"] and not row["invalid_basis"]),
            )
        )
    return CostProjection(query, tuple(buckets), revision)


def adjustments(db: sqlite3.Connection, query: dict[str, Any]) -> dict[str, Any]:
    full, partial = projection(db, query).selected()
    return {
        "deltaPico": str(sum(row.delta for row in full)),
        "unallocatedDeltaPico": str(sum(row.delta for row in partial)),
        "allocationIncomplete": bool(partial),
        "adjustments": [
            {
                "id": row.id,
                "targetId": row.target_id,
                "day": row.day,
                "deltaPico": str(row.delta),
                "targetPico": str(row.target),
                "estimatePico": str(row.estimate),
                "modelAllocated": row.model_allocated,
                "vendorId": row.vendor,
                "scope": row.scope,
            }
            for row in full
        ],
    }
