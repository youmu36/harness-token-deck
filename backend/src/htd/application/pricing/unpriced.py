"""Fill only unresolved requests with their original version's latest prices, up to 30 days."""

import hashlib
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import cast
from uuid import uuid4

from htd.application.command import execute, expected
from htd.application.ports import PricingChanged, QualityChanged, UnitOfWork, UsageChanged, WalletChanged
from htd.application.ports.maintenance import (
    RecalcRow,
    RecalcSnapshot,
    RecalcUpdate,
    UnpricedDecision,
    UnpricedStore,
)
from htd.application.pricing import model_dict
from htd.domain.billing.estimate import CostEstimate, estimate_cost, select_prices
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Clock, Instant, format_instant, parse_instant
from htd.domain.pricing import rules
from htd.domain.pricing.catalog import Resolution
from htd.domain.pricing.history import HISTORY_GROUP_ID, HISTORY_VERSION_ID
from htd.domain.pricing.model import EditorHints, ModelRate, PriceRevision
from htd.domain.pricing.multiplier import parse_multiplier
from htd.domain.usage.model import Harness
from htd.domain.usage.model_key import model_key


@dataclass(frozen=True)
class Candidate:
    row: RecalcRow
    revision: PriceRevision | None
    cost: CostEstimate | None
    group_name: str
    version_name: str
    reason: str


def candidate(snapshot: RecalcSnapshot, row: RecalcRow, at: Instant) -> Candidate:
    catalog = snapshot.catalog
    group_id, version_id = target(row)
    group = next((g for g in catalog.groups if g.id == group_id), None)
    version = next((v for v in catalog.versions if v.id == version_id), None)
    reason = ""
    revision = None
    cost = None
    if group is None or version is None or version.group_id != group.id:
        reason = "入账时未选择价格组或版本，不能自动指定历史归属"
    elif group.deleted_at is not None:
        reason = "原价格组已删除，无法补价"
    elif version.deleted_at is not None or version.disabled:
        reason = "原价格版本已停用或删除，无法补价"
    elif row.event.model_id is None and not (row.decision and row.decision.model):
        reason = "来源未报告模型名称，无法匹配价格"
    else:
        revision = catalog.revision_at(version.id, max(at, catalog.latest_time or at))
        rate = Resolution(group, version, revision).rate_for(
            row.decision.model.model_id if row.decision and row.decision.model else row.event.model_id
        )
        occurred = parse_instant(row.event.occurred_at)
        cost = estimate_cost(row.event.tokens, rate, occurred)
        if cost.unpriced:
            if rate is None:
                reason = "版本中尚未添加此模型"
            else:
                prices, _ = select_prices(rate, row.event.tokens, occurred)
                missing = [
                    name
                    for field, name in (
                        ("input", "输入价格"),
                        ("cache", "缓存命中价格"),
                        ("output", "输出价格"),
                    )
                    if getattr(prices, field) is None
                ]
                reason = (
                    "请补充"
                    + "、".join(missing)
                    + (" - 附加规则" if cost.rule_applied in ("tier", "peak") else "")
                )
    return Candidate(
        row,
        revision,
        cost,
        group.name if group else "未选择价格组",
        version.display_name if version else "未选择版本",
        reason,
    )


def target(row: RecalcRow) -> tuple[str | None, str | None]:
    if row.decision and row.decision.group_id:
        return row.decision.group_id, row.decision.version_id
    return cast(str | None, row.old["price_group_id"]), cast(str | None, row.old["price_version_id"])


def grouped(snapshots: tuple[RecalcSnapshot, ...]) -> dict[str, tuple[RecalcSnapshot, tuple[RecalcRow, ...]]]:
    groups: dict[tuple[object, ...], list[RecalcRow]] = {}
    owners: dict[tuple[object, ...], RecalcSnapshot] = {}
    for snapshot in snapshots:
        for row in snapshot.rows:
            key = (
                row.event.harness,
                row.event.model_id,
                *target(row),
                bool(row.decision and row.decision.ignored),
                row.decision.model.model_id if row.decision and row.decision.model else None,
            )
            groups.setdefault(key, []).append(row)
            owners[key] = snapshot
    return {
        hashlib.sha256(
            "\n".join(sorted(f"{r.event.harness.value}/{r.event.dedupe_key}" for r in rows)).encode()
        ).hexdigest(): (
            owners[key],
            tuple(sorted(rows, key=lambda r: (r.event.occurred_at, r.event.dedupe_key))),
        )
        for key, rows in groups.items()
    }


class Unpriced:
    def __init__(self, store: UnpricedStore, clock: Clock) -> None:
        self.store, self.clock = store, clock

    @staticmethod
    def window(at: Instant) -> tuple[Instant, Instant]:
        return Instant(at.value - timedelta(days=30)), Instant(at.value + timedelta(milliseconds=1))

    def list(self) -> dict[str, object]:
        at = self.clock.now_utc()
        start, end = self.window(at)
        snapshots = self.store.unpriced_snapshots(start, end)
        items: list[dict[str, object]] = []
        ready = total = ignored_count = 0
        for identifier, (snapshot, rows) in grouped(snapshots).items():
            item: dict[str, object] | None = None
            for row in rows:
                c = candidate(snapshot, row, at)
                group_id, version_id = target(row)
                ignored = bool(row.decision and row.decision.ignored)
                if item is None:
                    item = {
                        "id": identifier,
                        "ignored": ignored,
                        "harness": row.event.harness.value,
                        "modelId": row.event.model_id,
                        "groupId": group_id,
                        "versionId": version_id,
                        "groupName": c.group_name,
                        "versionName": c.version_name,
                        "recordCount": 0,
                        "readyCount": 0,
                        "reasons": [],
                        "firstAt": row.event.occurred_at,
                        "lastAt": row.event.occurred_at,
                        "editable": True,
                        "price": model_dict(row.decision.model)
                        if row.decision and row.decision.model
                        else None,
                        "multiplier": row.decision.multiplier if row.decision else "",
                    }
                    items.append(item)
                item["recordCount"] = cast(int, item["recordCount"]) + 1
                if ignored:
                    ignored_count += 1
                else:
                    total += 1
                if not c.reason:
                    item["readyCount"] = cast(int, item["readyCount"]) + 1
                    ready += not ignored
                elif c.reason not in cast(list[str], item["reasons"]):
                    cast(list[str], item["reasons"]).append(c.reason)
                item["firstAt"] = min(cast(str, item["firstAt"]), row.event.occurred_at)
                item["lastAt"] = max(cast(str, item["lastAt"]), row.event.occurred_at)
        return {
            "items": items,
            "limited": any(len(snapshot.rows) >= 2500 for snapshot in snapshots),
            "ignoredCount": ignored_count,
            "usageRevision": snapshots[0].usage_revision,
            "recordCount": total,
            "readyCount": ready,
            "pricingRevision": snapshots[0].catalog.revision,
            "start": format_instant(start),
            "end": format_instant(at),
            "archived": self.store.archived_unpriced(start, end),
        }

    def resolve(
        self, parameters: dict[str, object], operation_id: str, *, model: ModelRate | None = None
    ) -> dict[str, object]:
        def save(work: UnitOfWork, at: Instant) -> dict[str, object]:
            expected(work, "pricing", cast(int, parameters["expectedPricingRevision"]))
            expected(work, "quality", cast(int, parameters["expectedUsageRevision"]))
            start, end = self.window(at)
            snapshots = self.store.unpriced_snapshots(start, end, work)
            available = grouped(snapshots)
            identifiers = cast(list[str], parameters["itemIds"])
            if len(set(identifiers)) != len(identifiers) or any(i not in available for i in identifiers):
                raise DomainError("UNPRICED_CHANGED", "未定价列表已变化，请刷新后重新选择")
            selected = [r for i in identifiers for r in available[i][1]]
            limit = cast(int | None, parameters.get("recordLimit"))
            if limit is not None:
                if len(identifiers) != 1 or limit > len(selected):
                    raise DomainError("UNPRICED_CHANGED", "选择条数已变化，请刷新后重试")
                selected = selected[:limit]
            action = parameters["action"]
            old = snapshots[0].catalog
            new = old
            if action in ("assign", "price"):
                group_id = cast(str, parameters.get("groupId") or HISTORY_GROUP_ID)
                version_id = cast(
                    str,
                    parameters.get("versionId")
                    or (HISTORY_VERSION_ID if group_id == HISTORY_GROUP_ID else ""),
                )
                if group_id == HISTORY_GROUP_ID and not any(g.id == group_id for g in old.groups):
                    new = rules.create_group(
                        old,
                        group_id=group_id,
                        version_id=HISTORY_VERSION_ID,
                        revision_id=str(uuid4()),
                        name="历史价格组",
                        at=at,
                    )
                group = new.group(group_id)
                version = new.version(version_id)
                if (
                    version.group_id != group.id
                    or group.disabled
                    or group.deleted_at
                    or version.disabled
                    or version.deleted_at
                ):
                    raise DomainError("PRICING_INVALID", "请选择可用的价格组和版本")
                multiplier = cast(str, parameters.get("multiplier") or "")
                if multiplier:
                    multiplier = parse_multiplier(multiplier).normalized
                if action == "price":
                    if model is None:
                        raise DomainError("PRICING_INVALID", "请填写模型价格")
                    current = new.revision_at(version_id, max(at, new.latest_time or at))
                    models = (
                        *(m for m in current.models if m.key != model.key),
                        model,
                    )
                    new = rules.save_version_prices(
                        new,
                        version_id,
                        models,
                        revision_id=str(uuid4()),
                        expected_revision=new.revision,
                        at=at,
                    )
                    hints = tuple(
                        h
                        for h in new.hints
                        if (h.version_id, model_key(h.model_id)) != (version_id, model.key)
                    )
                    new = replace(
                        new,
                        hints=(*hints, EditorHints(version_id, model.model_id, multiplier))
                        if multiplier
                        else hints,
                    )
                if new is not old:
                    new = replace(new, revision=old.revision + 1)
                    self.store.save_unpriced_catalog(work, old, new)
                    work.emit(PricingChanged(work.revision("pricing")))
                for row in selected:
                    previous = row.decision or UnpricedDecision()
                    self.store.save_unpriced(
                        work,
                        (row,),
                        UnpricedDecision(
                            False,
                            group_id,
                            version_id,
                            model if action == "price" else previous.model,
                            multiplier if action == "price" else previous.multiplier,
                        ),
                        at=at,
                    )
            else:
                for row in selected:
                    self.store.save_unpriced(
                        work,
                        (row,),
                        replace(row.decision or UnpricedDecision(), ignored=action == "ignore"),
                        at=at,
                    )
            work.emit(QualityChanged(work.revision("quality")))
            return {"changed": len(selected), "revision": work.revision("quality")}

        return execute(self.store, self.clock, "pricing.resolveUnpriced", parameters, operation_id, save)

    def notice(self) -> dict[str, object]:
        return self.store.unpriced_notice(self.clock.now_utc())

    def recalculate(self, pricing_revision: int, operation_id: str) -> dict[str, object]:
        parameters: dict[str, object] = {"expectedPricingRevision": pricing_revision}

        def freeze(work: UnitOfWork, at: Instant) -> dict[str, object]:
            expected(work, "pricing", pricing_revision)
            start, end = self.window(at)
            return self.store.freeze_unpriced(work, operation_id, pricing_revision, start, end)

        result = execute(
            self.store, self.clock, "pricing.recalculateUnpriced", parameters, operation_id, freeze
        )
        if result.get("complete", True):
            return result

        def batch(work: UnitOfWork) -> dict[str, object]:
            expected(work, "pricing", pricing_revision)
            at = self.clock.now_utc()
            members, snapshots = self.store.unpriced_batch(work, operation_id)
            dates = [parse_instant(row.event.occurred_at) for snapshot in snapshots for row in snapshot.rows]
            start = min(dates) if dates else at
            end = Instant((max(dates) if dates else at).value + timedelta(milliseconds=1))
            delta = remaining = 0
            completed: list[tuple[str, str]] = []
            vendors: set[str] = set()
            harnesses: set[Harness] = set()
            for snapshot in snapshots:
                batches: dict[tuple[str, str], list[RecalcUpdate]] = defaultdict(list)
                for row in snapshot.rows:
                    if row.decision and row.decision.ignored:
                        continue
                    c = candidate(snapshot, row, at)
                    if c.reason or c.revision is None or c.cost is None:
                        remaining += 1
                        continue
                    group_id, version_id = target(row)
                    assert group_id is not None and version_id is not None
                    rate = Resolution(revision=c.revision).rate_for(
                        row.decision.model.model_id
                        if row.decision and row.decision.model
                        else row.event.model_id
                    )
                    assert rate is not None
                    new = dict(
                        row.old,
                        price_group_id=group_id,
                        price_version_id=version_id,
                        price_revision_id=c.revision.id,
                        pico=str(c.cost.pico_usd),
                        incomplete=int(c.cost.incomplete),
                        unpriced=0,
                        display_name=rate.model_id,
                        rule_applied=c.cost.rule_applied,
                        billed_at=format_instant(at),
                    )
                    batches[group_id, version_id].append(RecalcUpdate(row, new))
                    delta += c.cost.pico_usd - int(cast(str, row.old["pico"]))
                for (group_id, version_id), updates in batches.items():
                    harness = updates[0].row.event.harness
                    _, changed = self.store.commit_recalc(
                        work,
                        {
                            "harness": harness.value,
                            "start": format_instant(start),
                            "end": format_instant(end),
                            "groupId": group_id,
                            "versionId": version_id,
                            "fingerprint": str(uuid4()),
                        },
                        tuple(updates),
                        str(uuid4()),
                        at=at,
                    )
                    vendors.update(changed)
                    harnesses.add(harness)
                    completed.extend(
                        (update.row.event.harness.value, update.row.event.dedupe_key) for update in updates
                    )
            if harnesses:
                work.emit(UsageChanged(tuple(sorted(harnesses)), work.revision("usage")))
            if vendors:
                work.emit(WalletChanged(tuple(sorted(vendors)), work.revision("wallet")))
            return self.store.advance_unpriced(work, operation_id, members, tuple(completed), delta)

        try:
            while result.get("complete") is not True:
                result = self.store.execute(batch)
            return result
        except Exception as error:
            partial = self.store.unpriced_progress(operation_id)
            return dict(
                partial,
                errorCode=error.code if isinstance(error, DomainError) else "UNPRICED_PARTIAL_FAILURE",
            )
