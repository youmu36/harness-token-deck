"""Pricing catalog commands, atomic FB-06 switches and editor metadata."""

from dataclasses import replace
from typing import cast
from uuid import uuid4

from htd.application.command import execute, expected
from htd.application.ports import PricingChanged, PricingSnapshot, PricingStore, UnitOfWork, WalletChanged
from htd.domain.common.time import Clock, Instant, format_instant
from htd.domain.pricing import rules
from htd.domain.pricing.catalog import Catalog
from htd.domain.pricing.history import HISTORY_GROUP_ID
from htd.domain.pricing.model import EditorHints, ModelRate, SwitchPlan
from htd.domain.usage.model import Harness
from htd.domain.wallet.ledger import attribute_consumption


def model_dict(model: ModelRate) -> dict[str, object]:
    rule = model.billing_rule
    return {
        "modelId": model.model_id,
        "input": model.input,
        "cache": model.cache,
        "output": model.output,
        "cacheWrite": model.cache_write,
        "billingRule": None
        if rule is None
        else {
            "mode": rule.mode,
            "rates": {
                "input": rule.rates.input,
                "cache": rule.rates.cache,
                "output": rule.rates.output,
                "cacheWrite": rule.rates.cache_write,
            },
            "weekdays": list(rule.weekdays),
            "hours": list(rule.hours),
            "offsetMinutes": rule.offset_minutes,
            "thresholdK": rule.threshold_k,
        },
    }


def catalog_dict(snapshot: PricingSnapshot, now: Instant) -> dict[str, object]:
    catalog = snapshot.catalog
    at = catalog.latest_time or now
    groups: list[dict[str, object]] = []
    for group in catalog.groups:
        if group.deleted_at is not None:
            continue
        active = catalog.active_version(group.id, at)
        reason = rules.deletion_reason(
            catalog,
            group.id,
            ledger_referenced=group.id in snapshot.ledger_groups,
            recalc_referenced=group.id in snapshot.recalc_groups,
        )
        versions: list[dict[str, object]] = []
        for version in catalog.versions:
            if version.group_id != group.id or version.deleted_at is not None:
                continue
            revision = catalog.revision_at(version.id, at)
            versions.append(
                {
                    "id": version.id,
                    "number": version.number,
                    "label": version.label,
                    "displayName": version.display_name,
                    "createdAt": format_instant(version.created_at),
                    "current": active is not None and active.id == version.id,
                    "disabled": version.disabled,
                    "revisionId": revision.id,
                    "models": [model_dict(m) for m in revision.models],
                    "revisionCount": sum(r.version_id == version.id for r in catalog.revisions),
                    "hints": [
                        {"modelId": h.model_id, "multiplier": h.multiplier}
                        for h in catalog.hints
                        if h.version_id == version.id
                    ],
                }
            )
        groups.append(
            {
                "id": group.id,
                "name": group.name,
                "historical": group.id == HISTORY_GROUP_ID,
                "disabled": group.disabled,
                "createdAt": format_instant(group.created_at),
                "harnesses": [h.value for h in rules.group_harnesses(catalog, group.id)],
                "canDelete": reason is None,
                "deleteReason": reason,
                "vendorId": attribute_consumption(snapshot.links, group.id, now),
                "versions": versions,
                "nextVersionNumber": max(v.number for v in catalog.versions if v.group_id == group.id) + 1,
            }
        )
    current: dict[str, object] = {}
    for harness in Harness:
        resolved = catalog.resolve(harness, max(at, now))
        current[harness.value] = {
            "groupId": resolved.group.id if resolved.group else None,
            "versionId": resolved.version.id if resolved.version else None,
        }
    return {"groups": groups, "current": current, "revision": catalog.revision}


class Pricing:
    def __init__(self, store: PricingStore, clock: Clock) -> None:
        self.store, self.clock = store, clock

    def catalog(self, *, metadata_only: bool = False) -> dict[str, object]:
        return catalog_dict(self.store.pricing(metadata_only=metadata_only), self.clock.now_utc())

    @staticmethod
    def _plan(catalog: Catalog, parameters: dict[str, object], operation_id: str, at: Instant) -> SwitchPlan:
        return rules.plan_switch(
            catalog,
            Harness(parameters["harness"]),
            cast(str, parameters["groupId"]),
            cast(str, parameters["versionId"]),
            expected_revision=catalog.revision,
            operation_id=operation_id,
            at=at,
        )

    def preview(self, parameters: dict[str, object]) -> dict[str, object]:
        catalog = self.store.pricing().catalog
        plan = self._plan(catalog, parameters, str(uuid4()), self.clock.now_utc())
        return {
            "changed": plan.changed,
            "affectedHarnesses": [h.value for h in plan.affected_harnesses],
            "revision": catalog.revision,
        }

    def change(
        self,
        command: str,
        parameters: dict[str, object],
        operation_id: str,
        *,
        models: tuple[ModelRate, ...] = (),
        hints: tuple[EditorHints, ...] = (),
    ) -> dict[str, object]:
        def save(work: UnitOfWork, at: Instant) -> dict[str, object]:
            expected(work, "pricing", cast(int, parameters["expectedRevision"]))
            snapshot = self.store.pricing(work)
            old = snapshot.catalog
            group_id, version_id = (
                cast(str, parameters.get("groupId")),
                cast(str, parameters.get("versionId")),
            )
            result: dict[str, object] = {}
            match command:
                case "pricing.createGroup":
                    group_id, version_id = str(uuid4()), str(uuid4())
                    new = rules.create_group(
                        old,
                        group_id=group_id,
                        version_id=version_id,
                        revision_id=str(uuid4()),
                        name=cast(str, parameters["name"]),
                        at=at,
                        copy_from_group_id=cast(str | None, parameters.get("copyFromGroupId")),
                    )
                case "pricing.renameGroup":
                    new = rules.rename_group(old, group_id, cast(str, parameters["name"]))
                case "pricing.setGroupDisabled":
                    new = rules.set_group_disabled(old, group_id, cast(bool, parameters["disabled"]))
                case "pricing.deleteGroup":
                    new = rules.delete_group(
                        old,
                        group_id,
                        ledger_referenced=group_id in snapshot.ledger_groups,
                        recalc_referenced=group_id in snapshot.recalc_groups,
                        at=at,
                    )
                case "pricing.addVersion":
                    version_id = str(uuid4())
                    new = rules.add_version(
                        old,
                        group_id,
                        version_id=version_id,
                        revision_id=str(uuid4()),
                        number=cast(int, parameters["number"]),
                        label=cast(str, parameters["label"]),
                        at=at,
                        copy_from_version_id=cast(str | None, parameters.get("copyFromVersionId")),
                    )
                case "pricing.setVersionDisabled" | "pricing.deleteVersion":
                    new = rules.set_version_status(
                        old,
                        version_id,
                        disabled=cast(bool, parameters.get("disabled", True)),
                        delete=command == "pricing.deleteVersion",
                        at=at,
                    )
                    group_id = old.version(version_id).group_id
                case "pricing.setVersionLabel":
                    new = rules.set_version_label(old, version_id, cast(str, parameters["label"]))
                case "pricing.activateVersion":
                    new = rules.activate_version(old, group_id, version_id, at=at)
                    result["affectedHarnesses"] = (
                        [h.value for h in rules.group_harnesses(old, group_id)] if new != old else []
                    )
                case "pricing.saveVersionPrices":
                    new = rules.save_version_prices(
                        old,
                        version_id,
                        models,
                        revision_id=str(uuid4()),
                        expected_revision=old.revision,
                        at=at,
                    )
                    # Metadata shares the one command revision, never a price-history revision.
                    new = replace(new, hints=tuple(h for h in new.hints if h.version_id != version_id))
                    for hint in hints:
                        new = rules.save_editor_hints(new, hint)
                    if new != old:
                        new = replace(new, revision=old.revision + 1)
                case "pricing.switchCurrent":
                    plan = self._plan(old, parameters, operation_id, at)
                    # IPC owns the full receipt; legacy repository switch receipts remain supported.
                    new = replace(rules.apply_switch(old, plan), switches=old.switches)
                    result["affectedHarnesses"] = [h.value for h in plan.affected_harnesses]
                case _:
                    raise ValueError("Unknown pricing use case")
            self.store.save_catalog(work, old, new)
            changed = new.revision != old.revision
            if changed:
                work.emit(PricingChanged(work.revision("pricing")))
                if command == "pricing.deleteGroup":
                    vendor_id = attribute_consumption(snapshot.links, group_id, at)
                    if vendor_id is not None:
                        work.emit(WalletChanged((vendor_id,), work.touch("wallet")))
            return result | {
                "changed": changed,
                "revision": work.revision("pricing"),
                "groupId": group_id,
                "versionId": version_id,
            }

        return execute(self.store, self.clock, command, parameters, operation_id, save)
