"""Calibration plans and writes share the existing writer, receipts and WAL snapshots."""

import json
from typing import Any

from htd.adapters.sqlite.calibration_plan import build
from htd.adapters.sqlite.calibration_write import move_money, persist
from htd.adapters.sqlite.command_store import SqliteCommandStore, connection, read_snapshot
from htd.application.ports import UnitOfWork
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant


class SqliteCalibrationStore(SqliteCommandStore):
    def calibration_preview(self, parameters: dict[str, Any], at: Instant) -> dict[str, Any]:
        with read_snapshot(self.readers) as db:
            return build(db, parameters, at).view

    def calibration_commit(
        self, work: UnitOfWork, parameters: dict[str, Any], operation: str, at: Instant
    ) -> dict[str, Any]:
        db = connection(work)
        proposal = {key: value for key, value in parameters.items() if key != "fingerprint"}
        plan = build(db, proposal, at)
        if plan.view["fingerprint"] != parameters["fingerprint"]:
            raise DomainError("CALIBRATION_PLAN_CHANGED", "成员、估值、账户或旧目标已变化，请重新预览")
        identity = persist(work, db, plan, operation, at)
        move_money(work, db, plan, identity, operation, at)
        return {**plan.view, "targetId": identity, "revision": work.revision("cost")}

    def calibration_targets(self, parameters: dict[str, Any]) -> dict[str, Any]:
        with read_snapshot(self.readers) as db:
            args: list[Any] = [parameters["vendorId"]]
            clause = ""
            if parameters.get("state", "all") != "all":
                clause += " AND state=?"
                args.append(parameters["state"])
            if parameters.get("cursor"):
                try:
                    stamp, identity = json.loads(parameters["cursor"])
                except (ValueError, TypeError):
                    raise DomainError("INVALID_CURSOR", "校准记录分页游标无效") from None
                clause += " AND (created_at<? OR (created_at=? AND id<?))"
                args.extend((stamp, stamp, identity))
            rows = db.execute(
                "SELECT * FROM calibration_target WHERE vendor_id=?"
                + clause
                + " ORDER BY created_at DESC,id DESC LIMIT 51",
                args,
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows[:50]:
                evidence = json.loads(row["evidence_json"])
                proposal = evidence["proposal"]
                result.append(
                    {
                        "id": row["id"],
                        "vendorId": row["vendor_id"],
                        "state": row["state"],
                        "targetPico": row["target_pico"],
                        "estimatePico": row["estimate_pico"],
                        "deltaPico": row["delta_pico"],
                        "scope": json.loads(row["scope_json"]),
                        "createdAt": row["created_at"],
                        "revision": row["revision"],
                        "basis": evidence["kind"],
                        "legacyFlowId": evidence.get("legacyFlowId"),
                        "allocations": proposal.get("allocations", []),
                        "dayTargets": proposal.get("dayTargets", []),
                    }
                )
            return {
                "targets": result,
                "nextCursor": json.dumps([rows[49]["created_at"], rows[49]["id"]])
                if len(rows) > 50
                else None,
                "revision": db.execute("SELECT value FROM calibration_revision WHERE id=1").fetchone()[0],
            }
