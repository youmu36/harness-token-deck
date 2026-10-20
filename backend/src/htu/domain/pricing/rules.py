"""Pure catalog commands and atomic, replayable pricing-switch plans."""

from dataclasses import replace

from htu.domain.common.errors import (
    CLOCK_BEHIND_PRICE_HISTORY,
    GROUP_DISABLED,
    GROUP_IN_USE,
    GROUP_LIMIT,
    GROUP_REFERENCED,
    OPERATION_ID_REUSED,
    PRICING_INVALID,
    PRICING_REVISION_CONFLICT,
    VERSION_GROUP_MISMATCH,
    DomainError,
)
from htu.domain.common.time import Instant
from htu.domain.pricing.catalog import Catalog
from htu.domain.pricing.history import HISTORY_GROUP_ID
from htu.domain.pricing.model import (
    EditorHints,
    HarnessSelection,
    ModelRate,
    PriceGroup,
    PriceRevision,
    PriceVersion,
    SwitchPlan,
    VersionActivation,
    VersionDisplay,
    normalize_models,
)
from htu.domain.usage.model import Harness
from htu.domain.usage.model_key import model_key


def _clock(catalog: Catalog, at: Instant) -> None:
    latest = catalog.latest_time
    if latest is not None and at < latest:
        raise DomainError(
            CLOCK_BEHIND_PRICE_HISTORY, "系统时间早于最近一次价格变更，请校正时间后重试", "effective_at"
        )


def _expected(catalog: Catalog, revision: int) -> None:
    if type(revision) is not int or revision != catalog.revision:
        raise DomainError(PRICING_REVISION_CONFLICT, "价格目录已更新，请刷新后重试", "expected_revision")


def _enabled(catalog: Catalog, group_id: str) -> None:
    if catalog.group(group_id).disabled or catalog.group(group_id).deleted_at is not None:
        raise DomainError(GROUP_DISABLED, "该价格组已停用", "group_id")


def create_group(
    catalog: Catalog,
    *,
    group_id: str,
    version_id: str,
    revision_id: str,
    name: str,
    at: Instant,
    copy_from_group_id: str | None = None,
) -> Catalog:
    if sum(group.deleted_at is None for group in catalog.groups) >= 100:
        raise DomainError(GROUP_LIMIT, "最多保留 100 个价格组", "groups")
    _clock(catalog, at)
    models: tuple[ModelRate, ...] = ()
    if copy_from_group_id is not None:
        version = catalog.active_version(copy_from_group_id, at)
        if version is not None:
            models = catalog.revision_at(version.id, at).models
    group = PriceGroup(group_id, name, False, at)
    version = PriceVersion(version_id, group_id, 1, "", at)
    revision = PriceRevision(revision_id, version_id, at, catalog.sequence + 1, models)
    activation = VersionActivation(group_id, version_id, at, catalog.sequence + 2)
    return replace(
        catalog,
        groups=(*catalog.groups, group),
        versions=(*catalog.versions, version),
        revisions=(*catalog.revisions, revision),
        activations=(*catalog.activations, activation),
        revision=catalog.revision + 1,
    )


def rename_group(catalog: Catalog, group_id: str, name: str) -> Catalog:
    if group_id == HISTORY_GROUP_ID:
        raise DomainError(PRICING_INVALID, "历史价格组为系统统计分组，不能重命名")
    old = catalog.group(group_id)
    updated = replace(old, name=name)
    if old == updated:
        return catalog
    return replace(
        catalog,
        groups=tuple(updated if group.id == group_id else group for group in catalog.groups),
        revision=catalog.revision + 1,
    )


def group_harnesses(catalog: Catalog, group_id: str) -> tuple[Harness, ...]:
    group = catalog.group(group_id)
    at = catalog.latest_time or group.created_at
    return tuple(
        harness
        for harness in Harness
        if (selection := catalog.selection_at(harness, at)) is not None and selection.group_id == group_id
    )


def set_group_disabled(catalog: Catalog, group_id: str, disabled: bool) -> Catalog:
    if group_id == HISTORY_GROUP_ID:
        raise DomainError(PRICING_INVALID, "历史价格组为系统统计分组，不能停用")
    old = catalog.group(group_id)
    updated = replace(old, disabled=disabled)
    if disabled and group_harnesses(catalog, group_id):
        raise DomainError(GROUP_IN_USE, "请先切换正在使用此分组的 Harness", "group_id")
    if old == updated:
        return catalog
    return replace(
        catalog,
        groups=tuple(updated if group.id == group_id else group for group in catalog.groups),
        revision=catalog.revision + 1,
    )


def deletion_reason(
    catalog: Catalog,
    group_id: str,
    *,
    ledger_referenced: bool,
    recalc_referenced: bool,
) -> str | None:
    return GROUP_REFERENCED if catalog.group(group_id).deleted_at is not None else None


def delete_group(
    catalog: Catalog,
    group_id: str,
    *,
    ledger_referenced: bool,
    recalc_referenced: bool,
    at: Instant | None = None,
) -> Catalog:
    if group_id == HISTORY_GROUP_ID:
        raise DomainError(PRICING_INVALID, "历史价格组为系统统计分组，不能删除")
    if deletion_reason(
        catalog, group_id, ledger_referenced=ledger_referenced, recalc_referenced=recalc_referenced
    ):
        raise DomainError(GROUP_REFERENCED, "价格组已删除", "group_id")
    group = catalog.group(group_id)
    deleted = replace(group, disabled=True, deleted_at=at or catalog.latest_time or group.created_at)
    return replace(
        catalog,
        groups=tuple(deleted if item.id == group_id else item for item in catalog.groups),
        revision=catalog.revision + 1,
    )


def add_version(
    catalog: Catalog,
    group_id: str,
    *,
    version_id: str,
    revision_id: str,
    number: int,
    label: str,
    at: Instant,
    copy_from_version_id: str | None = None,
) -> Catalog:
    _enabled(catalog, group_id)
    _clock(catalog, at)
    models = catalog.revision_at(copy_from_version_id, at).models if copy_from_version_id is not None else ()
    version = PriceVersion(version_id, group_id, number, label, at)
    frozen: list[VersionActivation] = []
    if not any(activation.group_id == group_id for activation in catalog.activations):
        # Preserve the complete legacy fallback, including stable equal-time creation order.
        existing = sorted(
            (item for item in catalog.versions if item.group_id == group_id), key=lambda item: item.created_at
        )
        frozen = [
            VersionActivation(group_id, item.id, item.created_at, catalog.sequence + index)
            for index, item in enumerate(existing, 1)
        ]
    revision = PriceRevision(revision_id, version_id, at, catalog.sequence + len(frozen) + 1, models)
    return replace(
        catalog,
        versions=(*catalog.versions, version),
        revisions=(*catalog.revisions, revision),
        activations=(*catalog.activations, *frozen),
        revision=catalog.revision + 1,
    )


def save_version_prices(
    catalog: Catalog,
    version_id: str,
    models: tuple[ModelRate, ...],
    *,
    revision_id: str,
    expected_revision: int,
    at: Instant,
) -> Catalog:
    _expected(catalog, expected_revision)
    version = catalog.version(version_id)
    if version.disabled or version.deleted_at is not None:
        raise DomainError("VERSION_DISABLED", "该价格版本已停用或删除", "version_id")
    _enabled(catalog, version.group_id)
    normalized = normalize_models(models)
    current = catalog.revision_at(version_id, catalog.latest_time or at)
    if normalized == current.models:
        return catalog
    _clock(catalog, at)
    revision = PriceRevision(revision_id, version_id, at, catalog.sequence + 1, normalized)
    return replace(catalog, revisions=(*catalog.revisions, revision), revision=catalog.revision + 1)


def set_version_label(catalog: Catalog, version_id: str, label: str) -> Catalog:
    display = VersionDisplay(version_id, label)
    old = catalog.version(version_id)
    if old.disabled or old.deleted_at is not None:
        raise DomainError("VERSION_DISABLED", "该价格版本已停用或删除", "version_id")
    updated = replace(old, label=display.label)
    if old == updated:
        return catalog
    return replace(
        catalog,
        versions=tuple(updated if version.id == version_id else version for version in catalog.versions),
        revision=catalog.revision + 1,
    )


def activate_version(catalog: Catalog, group_id: str, version_id: str, *, at: Instant) -> Catalog:
    _enabled(catalog, group_id)
    version = catalog.version(version_id)
    if version.disabled or version.deleted_at is not None:
        raise DomainError("VERSION_DISABLED", "该价格版本已停用或删除", "version_id")
    if version.group_id != group_id:
        raise DomainError(VERSION_GROUP_MISMATCH, "所选版本不属于该价格组", "version_id")
    active = catalog.active_version(group_id, catalog.latest_time or at)
    if active is not None and active.id == version_id:
        return catalog
    _clock(catalog, at)
    return replace(
        catalog,
        activations=(*catalog.activations, VersionActivation(group_id, version_id, at, catalog.sequence + 1)),
        revision=catalog.revision + 1,
    )


def save_editor_hints(catalog: Catalog, hint: EditorHints) -> Catalog:
    catalog.version(hint.version_id)
    key = (hint.version_id, model_key(hint.model_id))
    old = next((item for item in catalog.hints if (item.version_id, model_key(item.model_id)) == key), None)
    if old == hint:
        return catalog
    others = tuple(item for item in catalog.hints if (item.version_id, model_key(item.model_id)) != key)
    return replace(catalog, hints=(*others, hint), revision=catalog.revision + 1)


def _replay(
    catalog: Catalog, operation_id: str, harness: Harness, group_id: str, version_id: str
) -> SwitchPlan | None:
    if type(operation_id) is not str or not operation_id:
        raise DomainError(PRICING_INVALID, "操作标识不能为空", "operation_id")
    for prior in catalog.switches:
        if prior.operation_id == operation_id:
            if (prior.harness, prior.group_id, prior.version_id) != (harness, group_id, version_id):
                raise DomainError(OPERATION_ID_REUSED, "该操作标识已用于另一项切换", "operation_id")
            return prior
    return None


def plan_switch(
    catalog: Catalog,
    harness: Harness,
    group_id: str,
    version_id: str,
    *,
    expected_revision: int,
    operation_id: str,
    at: Instant,
) -> SwitchPlan:
    if group_id == HISTORY_GROUP_ID:
        raise DomainError(PRICING_INVALID, "历史价格组仅用于统计，不能用于实时计费")
    harness = Harness(harness)
    prior = _replay(catalog, operation_id, harness, group_id, version_id)
    if prior is not None:
        return prior
    _expected(catalog, expected_revision)
    _enabled(catalog, group_id)
    version = catalog.version(version_id)
    if version.disabled or version.deleted_at is not None:
        raise DomainError("VERSION_DISABLED", "该价格版本已停用或删除", "version_id")
    if version.group_id != group_id:
        raise DomainError(VERSION_GROUP_MISMATCH, "所选版本不属于该价格组", "version_id")
    current_at = catalog.latest_time or at
    selection = catalog.selection_at(harness, current_at)
    active = catalog.active_version(group_id, current_at)
    change_group = selection is None or selection.group_id != group_id
    change_version = active is None or active.id != version_id
    selections: tuple[HarnessSelection, ...] = ()
    activations: tuple[VersionActivation, ...] = ()
    affected: set[Harness] = set()
    if change_group or change_version:
        _clock(catalog, at)
        affected.add(harness)
    if change_group:
        selections = (HarnessSelection(harness, group_id, at, catalog.sequence + 1),)
    if change_version:
        activations = (VersionActivation(group_id, version_id, at, catalog.sequence + len(selections) + 1),)
        affected.update(group_harnesses(catalog, group_id))
    return SwitchPlan(
        operation_id,
        harness,
        group_id,
        version_id,
        expected_revision,
        at,
        selections,
        activations,
        tuple(item for item in Harness if item in affected),
    )


def apply_switch(catalog: Catalog, plan: SwitchPlan) -> Catalog:
    prior = _replay(catalog, plan.operation_id, plan.harness, plan.group_id, plan.version_id)
    if prior is not None:
        return catalog
    expected = plan_switch(
        catalog,
        plan.harness,
        plan.group_id,
        plan.version_id,
        expected_revision=plan.expected_revision,
        operation_id=plan.operation_id,
        at=plan.effective_at,
    )
    if expected != plan:
        raise DomainError(PRICING_INVALID, "切换计划与当前价格目录不一致", "plan")
    return replace(
        catalog,
        selections=(*catalog.selections, *plan.selections),
        activations=(*catalog.activations, *plan.activations),
        switches=(*catalog.switches, plan),
        revision=catalog.revision + int(plan.changed),
    )


def set_version_status(
    catalog: Catalog, version_id: str, *, disabled: bool, delete: bool, at: Instant
) -> Catalog:
    old = catalog.version(version_id)
    if old.deleted_at is not None:
        raise DomainError("VERSION_DELETED", "该价格版本已删除", "version_id")
    active = catalog.active_version(old.group_id, catalog.latest_time or at)
    if (disabled or delete) and active and active.id == version_id:
        raise DomainError("VERSION_IN_USE", "请先将其他版本设为生效，再停用或删除此版本", "version_id")
    _clock(catalog, at)
    updated = replace(old, disabled=disabled or delete, deleted_at=at if delete else None)
    if updated == old:
        return catalog
    return replace(
        catalog,
        versions=tuple(updated if v.id == version_id else v for v in catalog.versions),
        revision=catalog.revision + 1,
    )
