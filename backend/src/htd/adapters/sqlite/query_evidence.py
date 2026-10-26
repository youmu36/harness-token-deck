"""On-demand request-to-voucher metadata, read on the ordinary bounded connection pool."""

import sqlite3
from datetime import timedelta
from typing import Any

from htd.adapters.sqlite.key_codec import key_bytes
from htd.application.ports.query_control import check_read
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant


def read(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, Any]:
    if parse_instant(query["end"]) <= parse_instant(query["start"]):
        raise DomainError("RANGE_INVALID", "诊断结束时间须晚于开始时间")
    clause = ""
    arguments: list[Any] = [query["start"], query["end"]]
    for name, field in (
        ("harness", "s.harness"),
        ("sessionRef", "s.session_ref"),
        ("vendorId", "m.vendor_id"),
    ):
        if query.get(name) is not None:
            clause += " AND COALESCE(" + field + ",'')=?"
            arguments.append(query[name])
    if query.get("localBatchIds"):
        clause += " AND m.batch_id IN (" + ",".join("?" for _ in query["localBatchIds"]) + ")"
        arguments.extend(query["localBatchIds"])
    offset = query.get("offset", 0)
    rows = db.execute(
        "SELECT s.*,l.price_revision_id,l.price_group_id,l.price_version_id,l.pico,"
        "m.original_group_id,m.original_version_id,m.original_pico,m.estimate_pico,m.voucher_seq,"
        "m.batch_id,o.target_id FROM session_request s LEFT JOIN ledger l USING(harness,dedupe_key) "
        "LEFT JOIN reconciliation_member m USING(harness,dedupe_key) "
        "LEFT JOIN calibration_owner o USING(harness,dedupe_key) "
        "WHERE s.occurred_at>=? AND s.occurred_at<?"
        + clause
        + " ORDER BY s.occurred_at DESC,s.harness,s.dedupe_key LIMIT 51 OFFSET ?",
        (*arguments, offset),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows[:50]:
        check_read()
        intervals = [
            item[0]
            for item in db.execute(
                "SELECT interval_id FROM reconciliation_interval_member WHERE harness=? AND dedupe_key=? "
                "ORDER BY interval_id LIMIT 20",
                (row["harness"], key_bytes(row["dedupe_key"])),
            )
        ]
        items.append(
            {
                "harness": row["harness"],
                "requestKey": key_bytes(row["dedupe_key"]).hex(),
                "sessionRef": row["session_ref"],
                "occurredAt": row["occurred_at"],
                "receivedAt": row["received_at"],
                "watermark": row["watermark"],
                "state": row["state"],
                "acceptedRevision": row["revision"],
                "valuedRevision": row["valued_revision"],
                "projectedRevision": row["projected_revision"],
                "publishedRevision": row["published_revision"],
                "groupId": row["price_group_id"],
                "versionId": row["price_version_id"],
                "priceRevisionId": row["price_revision_id"],
                "originalGroupId": row["original_group_id"],
                "originalVersionId": row["original_version_id"],
                "originalPico": row["original_pico"],
                "estimatePico": row["estimate_pico"] if row["estimate_pico"] is not None else row["pico"],
                "voucherSequence": row["voucher_seq"],
                "localBatchId": row["batch_id"],
                "calibrationTargetId": row["target_id"],
                "remoteIntervalIds": intervals,
                "canRecalculate": row["pico"] is not None
                and parse_instant(row["occurred_at"]).value >= at.value - timedelta(hours=72),
            }
        )
    snapshot = dict(db.execute("SELECT scope,value FROM revision"))
    snapshot["cost"] = db.execute("SELECT value FROM calibration_revision WHERE id=1").fetchone()[0]
    return {
        "items": items,
        "nextOffset": offset + 50 if len(rows) > 50 else None,
        "sourceStates": dict(db.execute("SELECT harness,state FROM source_visibility")),
        "snapshot": snapshot,
        "generatedAt": format_instant(at),
    }
