"""Plan request-time repricing and commit ledger, audit and wallet deltas atomically."""

import hashlib
import json
from dataclasses import asdict
from datetime import timedelta
from typing import cast

from htd.application.command import execute
from htd.application.ports import UnitOfWork, UsageChanged, WalletChanged
from htd.application.ports.maintenance import ManualMaintenanceStore, RecalcSnapshot, RecalcUpdate
from htd.domain.billing.estimate import estimate_cost
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant, format_instant, parse_instant
from htd.domain.pricing.catalog import Resolution
from htd.domain.pricing.history import HISTORY_GROUP_ID
from htd.domain.usage.model import Harness
from htd.domain.wallet.ledger import attribute_consumption


def validate_window(start: Instant, end: Instant, now: Instant) -> None:
    if end <= start:
        raise DomainError("RANGE_INVALID", "结束时间必须晚于开始时间", "end")
    if end.value - start.value > timedelta(hours=72):
        raise DomainError("RECALC_RANGE_TOO_LONG", "重算范围不能超过 72 小时", "start")
    if start.value < now.value - timedelta(hours=72):
        raise DomainError("RECALC_TOO_OLD", "只能重算最近 72 小时的明细", "start")
    if end.value > now.value + timedelta(minutes=1):
        raise DomainError("RECALC_IN_FUTURE", "重算结束时间不能在未来", "end")


def plan(
    snapshot: RecalcSnapshot, parameters: dict[str, object], at: Instant
) -> tuple[dict[str, object], tuple[RecalcUpdate, ...]]:
    catalog = snapshot.catalog
    group = catalog.group(cast(str, parameters["groupId"]))
    if group.deleted_at is not None:
        raise DomainError("GROUP_NOT_FOUND", "目标价格组已删除", "group_id")
    version = catalog.version(cast(str, parameters["versionId"]))
    if version.disabled or version.deleted_at is not None:
        raise DomainError("VERSION_DISABLED", "目标价格版本已停用或删除", "version_id")
    if version.group_id != group.id:
        raise DomainError("VERSION_NOT_FOUND", "目标版本不属于所选价格组", "version_id")
    updates: list[RecalcUpdate] = []
    old_total = new_total = unpriced = incomplete = 0
    vendors: dict[str, list[int]] = {}
    rules = {"none": 0, "base": 0, "peak": 0, "tier": 0}
    target_revision = max(
        (revision for revision in catalog.revisions if revision.version_id == version.id),
        key=lambda revision: (revision.effective_at, revision.seq),
    )
    for row in snapshot.rows:
        event = row.event
        occurred = parse_instant(event.occurred_at)
        revision = target_revision
        rate = Resolution(group, version, revision).rate_for(event.model_id)
        cost = estimate_cost(event.tokens, rate, occurred)
        old_total += int(cast(str, row.old["pico"]))
        new_total += cost.pico_usd
        unpriced += cost.unpriced
        incomplete += cost.incomplete
        rules[cost.rule_applied] += 1
        updates.append(
            RecalcUpdate(
                row,
                {
                    "price_group_id": group.id,
                    "price_version_id": version.id,
                    "price_revision_id": revision.id,
                    "pico": str(cost.pico_usd),
                    "incomplete": int(cost.incomplete),
                    "unpriced": int(cost.unpriced),
                    "display_name": rate.model_id if rate else None,
                    "rule_applied": cost.rule_applied,
                    "billed_at": format_instant(at),
                },
            )
        )
        vendor_id = (
            row.consumption.vendor_id
            if row.consumption is not None
            else attribute_consumption(snapshot.links, group.id, at)
            if group.id != HISTORY_GROUP_ID
            else None
        )
        if vendor_id is not None:
            delta = (
                0
                if row.local_override
                else cost.pico_usd - int(cast(str, row.old["pico"]))
                if row.consumption is not None
                else cost.pico_usd
            )
            aggregate = vendors.setdefault(vendor_id, [0, 0])
            aggregate[0] += delta
            aggregate[1] -= delta

    # Old billing/usage and wallet calibration snapshots also bind what the user approved.
    payload = {
        "target": [
            {
                "id": r.id,
                "at": format_instant(r.effective_at),
                "seq": r.seq,
                "models": [asdict(m) for m in r.models],
            }
            for r in sorted(catalog.revisions, key=lambda r: r.seq)
            if r.version_id == version.id
        ],
        "window": parameters,
        "recordCount": len(snapshot.rows),
        "unavailable": snapshot.unavailable,
        "costRevision": snapshot.cost_revision,
        "identityDigest": hashlib.sha256(
            json.dumps(
                [(r.event.harness.value, r.event.dedupe_key) for r in snapshot.rows],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "rows": [
            {
                "event": asdict(r.event),
                "old": r.old,
                "consumption": r.consumption.id if r.consumption else None,
                "localOverride": r.local_override,
            }
            for r in snapshot.rows
        ],
        "localProjectionVendors": sorted(snapshot.vendors),
        "links": [
            asdict(link) | {"effective_at": format_instant(link.effective_at)} for link in snapshot.links
        ],
        "calibrations": {
            v: [(c.entry_id, format_instant(c.at), c.seq) for c in records]
            for v, records in sorted(snapshot.calibrations.items())
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()
    return {
        "recordCount": len(updates),
        "totalCount": len(updates) + len(snapshot.unavailable),
        "pendingCount": sum(pending for _, pending in snapshot.unavailable),
        "unavailableCount": len(snapshot.unavailable),
        "evidenceInsufficientCount": len(snapshot.unavailable) + incomplete,
        "targetRevisionId": target_revision.id,
        "targetRevisionSeq": target_revision.seq,
        "ruleCounts": rules,
        "oldTotal": str(old_total),
        "newTotal": str(new_total),
        "unpricedCount": unpriced,
        "incompleteCount": incomplete,
        "groupName": group.name,
        "versionDisplayName": version.display_name,
        "fingerprint": fingerprint,
        "vendors": [
            {
                "vendorId": v,
                "name": snapshot.vendors[v],
                "consumptionDelta": str(d[0]),
                "balanceDelta": str(d[1]),
            }
            for v, d in sorted(vendors.items())
        ],
    }, tuple(updates)


class Recalculate:
    def __init__(self, store: ManualMaintenanceStore, clock: Clock) -> None:
        self.store, self.clock = store, clock

    def preview(self, parameters: dict[str, object]) -> dict[str, object]:
        now = self.clock.now_utc()
        harness = Harness(parameters["harness"])
        start, end = (
            parse_instant(cast(str, parameters["start"])),
            parse_instant(cast(str, parameters["end"])),
        )
        validate_window(start, end, now)
        return plan(self.store.recalc_snapshot(harness, start, end), parameters, now)[0]

    def commit(self, parameters: dict[str, object], operation_id: str) -> dict[str, object]:
        window = {key: value for key, value in parameters.items() if key != "fingerprint"}

        def save(work: UnitOfWork, at: Instant) -> dict[str, object]:
            harness = Harness(parameters["harness"])
            start, end = (
                parse_instant(cast(str, parameters["start"])),
                parse_instant(cast(str, parameters["end"])),
            )
            validate_window(start, end, at)
            preview, updates = plan(self.store.recalc_snapshot(harness, start, end, work), window, at)
            if preview["fingerprint"] != parameters["fingerprint"]:
                raise DomainError("RECALC_PLAN_CHANGED", "价格或账本已变化，请重新预览后再重算")
            batch_id, vendors = self.store.commit_recalc(work, parameters, updates, operation_id, at=at)
            if updates:
                work.emit(UsageChanged((harness,), work.revision("usage")))
            if vendors:
                work.emit(WalletChanged(vendors, work.revision("wallet")))
            return {**preview, "batchId": batch_id, "revision": work.revision("usage")}

        return execute(self.store, self.clock, "maintenance.commitRecalc", parameters, operation_id, save)
