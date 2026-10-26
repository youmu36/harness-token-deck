"""Frozen-member calibration planning; no money or targets are written while previewing."""

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from htd.adapters.sqlite.calibration_costs import adjustments
from htd.adapters.sqlite.calibration_projection import Basis
from htd.adapters.sqlite.reconciliation_read import member_rows, preview
from htd.adapters.sqlite.wallet_closing import bounds, timezone
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.wallet.calibration import matches, nonnegative_pico, validate_conservation

SCOPE_KEYS = ("vendorId", "start", "end", "localBatchIds", "intervalIds")


@dataclass
class Bucket:
    key: str
    day: str
    selector: dict[str, Any]
    target: int = 0
    basis: Basis = field(default_factory=Basis)
    model_allocated: bool = False


@dataclass
class Plan:
    query: dict[str, Any]
    proposal: dict[str, Any]
    view: dict[str, Any]
    buckets: list[Bucket]
    day_basis: dict[str, Basis]
    replacements: list[sqlite3.Row]

    def bucket_for(self, member: sqlite3.Row, zone: ZoneInfo) -> Bucket:
        day = parse_instant(member["occurred_at"]).value.astimezone(zone).date().isoformat()
        parts = [
            part
            for part in self.buckets
            if part.day == day and part.selector and matches(member, part.selector)
        ]
        return parts[0] if parts else next(part for part in self.buckets if part.key == "remaining:" + day)


def build(db: sqlite3.Connection, proposal: dict[str, Any], at: Instant, *, legacy: bool = False) -> Plan:
    action = proposal.get("action", "apply")
    query = {key: proposal[key] for key in SCOPE_KEYS if key in proposal}
    query.setdefault("localBatchIds", [])
    query.setdefault("intervalIds", [])
    target_id = proposal.get("targetId")
    if action == "revoke":
        old = db.execute(
            "SELECT * FROM calibration_target WHERE id=? AND state='active'", (target_id,)
        ).fetchone()
        if old is None or old["vendor_id"] != query["vendorId"]:
            raise DomainError("CALIBRATION_TARGET_NOT_FOUND", "锁定目标不存在或已被替换/撤销")
        query = json.loads(old["scope_json"])
        query["memberTargetId"] = old["id"]
    elif legacy:
        query["legacyFlowId"] = proposal["legacyFlowId"]
    if not query["vendorId"]:
        raise DomainError("CALIBRATION_VENDOR_REQUIRED", "未归属请求须先核实供应商与消费凭证")
    if (
        action != "revoke"
        and not legacy
        and parse_instant(query["start"]).value < at.value - timedelta(days=3)
    ):
        raise DomainError("CALIBRATION_EXPIRED", "仅支持校准过去三天的消费事务")
    if parse_instant(query["end"]).value > at.value + timedelta(minutes=1):
        raise DomainError("CALIBRATION_FUTURE", "不能校准尚未发生的消费范围")
    base = preview(db, query, at)
    if base["memberCount"] == "0":
        raise DomainError("CALIBRATION_EMPTY", "所选范围没有可核验的本地消费成员")
    if base["legacyOverrides"] and not legacy:
        raise DomainError("CALIBRATION_LEGACY_PENDING", "旧人工金额尚未完成成员承接，不能再叠加目标")
    if action != "revoke":
        if not proposal.get("confirmLocalScope"):
            raise DomainError("CALIBRATION_CONFIRM_SCOPE", "请确认目标金额属于所选本地消费范围")
        if proposal.get("basis", "manual") == "remote":
            if base["reliability"] != "reliable_usage" or not (
                base["localComplete"] and base["quiet"] and base["coverageAligned"]
            ):
                raise DomainError(
                    "CALIBRATION_REMOTE_UNPROVEN", "当前远程依据不能证明完整覆盖，请核实后使用人工指定"
                )
            if proposal["targetPico"] != base["remoteTargetPico"]:
                raise DomainError("CALIBRATION_REMOTE_CHANGED", "采用远程金额须与本次有效远程目标一致")
        elif not (base["localComplete"] and base["quiet"]) and not proposal.get("acknowledgeIncomplete"):
            raise DomainError("CALIBRATION_INCOMPLETE", "请先核实覆盖，或明确确认依据不足时的人工判断")
    zone = ZoneInfo(timezone(db))
    from htd.adapters.sqlite.calibration_allocation import expand

    proposal = expand(db, query, proposal)
    declared: list[dict[str, Any]] = proposal.get("allocations", []) if action != "revoke" else []
    parts = [Bucket("allocation:" + str(index), part["day"], part) for index, part in enumerate(declared)]
    days: dict[str, Basis] = {}
    owners: Counter[str] = Counter()
    for member in member_rows(db, query, with_owner=True):
        if not member["voucher_seq"]:
            raise DomainError("CALIBRATION_VOUCHER_MISSING", "范围包含没有消费凭证的估算，请先补价/重算核实")
        day = parse_instant(member["occurred_at"]).value.astimezone(zone).date().isoformat()
        days.setdefault(day, Basis()).add(member)
        selected = [
            part for part in parts if part.selector and part.day == day and matches(member, part.selector)
        ]
        if len(selected) > 1:
            raise DomainError("CALIBRATION_ALLOCATION_OVERLAP", "同一成员不能归入多个人工分项")
        if selected:
            selected[0].basis.add(member)
        else:
            remaining = next((part for part in parts if part.key == "remaining:" + day), None)
            if remaining is None:
                remaining = Bucket("remaining:" + day, day, {})
                parts.append(remaining)
            remaining.basis.add(member)
        if member["active_target"]:
            owners[member["active_target"]] += 1
    replacements: list[sqlite3.Row] = []
    requested: set[str] = set(proposal.get("replaceTargetIds", []))
    if target_id:
        requested.add(str(target_id))
    if set(owners) != requested:
        raise DomainError("CALIBRATION_OVERLAP", "范围已有锁定目标，须选择全部重叠目标并明确替换")
    if owners and action != "revoke" and not proposal.get("confirmReplacement"):
        raise DomainError("CALIBRATION_CONFIRM_REPLACEMENT", "请确认替换原锁定目标及其完整范围")
    for identity, count in owners.items():
        row = db.execute("SELECT * FROM calibration_target WHERE id=?", (identity,)).fetchone()
        old_scope = json.loads(row["scope_json"])
        if (
            count != row["member_count"]
            or query["start"] > old_scope["start"]
            or query["end"] < old_scope["end"]
        ):
            raise DomainError("CALIBRATION_PARTIAL_OVERLAP", "无法拆分原目标的部分覆盖，请选择完整原范围")
        replacements.append(row)
    target = (
        sum(day.estimate for day in days.values())
        if action == "revoke"
        else nonnegative_pico(proposal["targetPico"])
    )
    daily = (
        proposal.get("dayTargets", [])
        if action != "revoke"
        else [{"day": day, "targetPico": str(basis.estimate)} for day, basis in days.items()]
    )
    if len(days) == 1 and not daily:
        daily = [{"day": next(iter(days)), "targetPico": str(target)}]
    validate_conservation(target, daily)
    if {part["day"] for part in daily} != set(days):
        raise DomainError("CALIBRATION_DATES_REQUIRED", "跨日校准须逐个明确实际业务日的目标")
    day_targets = {part["day"]: nonnegative_pico(part["targetPico"]) for part in daily}
    for part in parts:
        if not part.selector:
            continue
        if not part.basis.count:
            raise DomainError("CALIBRATION_ASSIGNMENT_EMPTY", "人工分项没有匹配的本地成员")
        part.target = nonnegative_pico(part.selector["targetPico"])
        if part.selector.get("confirmModelCoverage"):
            if len(part.basis.dimensions["modelKeys"]) != 1 or "" in part.basis.dimensions["modelKeys"]:
                raise DomainError("CALIBRATION_MODEL_COVERAGE", "归属模型须为一个可确认模型，请分别指定")
            part.model_allocated = True
    for day in days:
        remainder = day_targets[day] - sum(part.target for part in parts if part.day == day and part.selector)
        if remainder < 0:
            raise DomainError("CALIBRATION_ALLOCATION_INVALID", "分项金额不能超过该日目标")
        remaining = next((part for part in parts if part.key == "remaining:" + day), None)
        if remaining is None:
            remaining = Bucket("remaining:" + day, day, {})
            parts.append(remaining)
        remaining.target = remainder
    day_view: list[dict[str, Any]] = []
    for day, basis in sorted(days.items()):
        start, end = map(format_instant, bounds(day, zone.key))
        subquery = dict(query, start=max(start, query["start"]), end=min(end, query["end"]))
        effective = basis.estimate + int(adjustments(db, subquery)["deltaPico"])
        if legacy:
            # The old flow already changed wallet money; its metadata adoption
            # does not authorize or post a second amount.
            effective = day_targets[day]
        day_view.append(
            {
                "day": day,
                "estimatePico": str(basis.estimate),
                "currentEffectivePico": str(effective),
                "targetPico": str(day_targets[day]),
                "deltaPico": str(day_targets[day] - effective),
                "unallocatedPico": str(next(part.target for part in parts if part.key == "remaining:" + day)),
            }
        )
    delta = sum(int(day["deltaPico"]) for day in day_view)
    costs = db.execute("SELECT value FROM calibration_revision WHERE id=1").fetchone()[0]
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                base["fingerprint"],
                proposal,
                day_view,
                costs,
                [(row["id"], row["revision"], row["state"]) for row in replacements],
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    view = {
        **base,
        "fingerprint": fingerprint,
        "action": action,
        "targetPico": str(target),
        "deltaPico": str(delta),
        "days": day_view,
        "modelAllocationCount": sum(part.model_allocated for part in parts),
        "modelTargets": [
            {
                "day": part.day,
                "modelKey": next(iter(part.basis.dimensions["modelKeys"])),
                "estimatePico": str(part.basis.estimate),
                "targetPico": str(part.target),
                "deltaPico": str(part.target - part.basis.estimate),
            }
            for part in parts
            if part.model_allocated
        ],
        "replacedTargets": [
            {"id": row["id"], "targetPico": row["target_pico"], "revision": row["revision"]}
            for row in replacements
        ],
    }
    return Plan(query, proposal, view, parts, days, replacements)
