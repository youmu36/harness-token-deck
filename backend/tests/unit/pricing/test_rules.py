from dataclasses import replace

import pytest
from htu.domain.common.errors import (
    CLOCK_BEHIND_PRICE_HISTORY,
    GROUP_DISABLED,
    GROUP_DUPLICATE,
    GROUP_IN_USE,
    GROUP_LIMIT,
    GROUP_REFERENCED,
    OPERATION_ID_REUSED,
    PRICING_REVISION_CONFLICT,
    VERSION_DUPLICATE,
    VERSION_GROUP_MISMATCH,
    DomainError,
)
from htu.domain.common.time import parse_instant
from htu.domain.pricing.catalog import Catalog
from htu.domain.pricing.model import (
    BillingRule,
    EditorHints,
    HarnessSelection,
    ModelRate,
    PriceRates,
    PriceRevision,
    VersionActivation,
)
from htu.domain.pricing.rules import (
    add_version,
    apply_switch,
    create_group,
    delete_group,
    deletion_reason,
    group_harnesses,
    plan_switch,
    rename_group,
    save_editor_hints,
    save_version_prices,
    set_group_disabled,
    set_version_label,
)
from htu.domain.usage.model import Harness

T0 = parse_instant("2026-09-01T00:00:00Z")
T1 = parse_instant("2026-09-01T01:00:00Z")
T2 = parse_instant("2026-09-01T02:00:00Z")


def third(catalog: Catalog) -> Catalog:
    return create_group(catalog, group_id="g3", version_id="v5", revision_id="r5", name="Group 3", at=T1)


def test_create_group_initial_revision_and_activation() -> None:
    original = Catalog()
    created = create_group(original, group_id="g", version_id="v", revision_id="r", name=" First ", at=T0)
    assert created.revision == 1
    assert created.group("g").name == "First"
    assert created.version("v").number == 1
    assert created.revisions[0].models == ()
    assert (created.revisions[0].seq, created.activations[0].seq) == (1, 2)
    assert created.active_version("g", T0) == created.version("v")
    assert created.selections == ()
    assert original == Catalog()


def test_create_group_copies_active_revision_not_latest_version(catalog: Catalog) -> None:
    created = create_group(
        catalog, group_id="g3", version_id="v5", revision_id="r5", name="Copy", at=T1, copy_from_group_id="g2"
    )
    assert created.revision_at("v5", T1).models == catalog.revision_at("v3", T1).models
    assert created.revision_at("v5", T1).models != catalog.revision_at("v4", T1).models
    assert group_harnesses(created, "g3") == ()


def test_add_version_copies_requested_revision_without_activation(catalog: Catalog) -> None:
    updated = add_version(
        catalog,
        "g1",
        version_id="v5",
        revision_id="r5",
        number=5,
        label="New",
        at=T1,
        copy_from_version_id="v4",
    )
    assert updated.active_version("g1", T1) == catalog.version("v1")
    assert updated.activations == catalog.activations
    assert updated.revision_at("v5", T1).models == catalog.revision_at("v4", T1).models
    assert updated.revision == catalog.revision + 1


def test_P04_legacy_add_freezes_whole_creation_timeline(catalog: Catalog) -> None:
    legacy = replace(
        catalog,
        activations=(),
        versions=(
            replace(catalog.versions[0], number=99),
            replace(catalog.versions[1], created_at=T1),
            *catalog.versions[2:],
        ),
    )
    snapshots = [legacy.active_version("g1", at) for at in (T0, T1, T2)]
    changed = add_version(legacy, "g1", version_id="v5", revision_id="r5", number=5, label="", at=T2)
    assert [changed.active_version("g1", at) for at in (T0, T1, T2)] == snapshots
    assert [(record.version_id, record.effective_at) for record in changed.activations] == [
        ("v1", T0),
        ("v2", T1),
    ]
    assert changed.sequence == legacy.sequence + 3
    # A second addition must not refreeze or activate either added version.
    again = add_version(changed, "g1", version_id="v6", revision_id="r6", number=6, label="", at=T2)
    assert again.activations == changed.activations
    assert again.active_version("g1", T2) == legacy.version("v2")


def test_legacy_freeze_preserves_same_time_creation_order(catalog: Catalog) -> None:
    legacy = replace(catalog, activations=())
    changed = add_version(legacy, "g1", version_id="v5", revision_id="r5", number=5, label="", at=T1)
    assert changed.active_version("g1", T0) == legacy.active_version("g1", T0)


def test_P05_multiplier_and_label_do_not_create_price_revision(catalog: Catalog) -> None:
    hinted = save_editor_hints(catalog, EditorHints("v1", "Model A", "1.500"))
    changed = set_version_label(hinted, "v1", " New label ")
    assert changed.revisions == catalog.revisions
    assert changed.activations == catalog.activations
    assert changed.fingerprint == catalog.fingerprint
    assert changed.hints[0].multiplier == "1.5"
    assert changed.version("v1").display_name == "New label"
    assert save_editor_hints(hinted, EditorHints("v1", "Model A", "1.5")) is hinted
    assert set_version_label(changed, "v1", "New label") is changed


def test_P06_model_order_and_rate_spelling_do_not_append(catalog: Catalog) -> None:
    models = (ModelRate("z", "Z", "1.50"), ModelRate("a", "A", "007", "0.000000"))
    first = save_version_prices(
        catalog, "v1", models, revision_id="r5", expected_revision=catalog.revision, at=T1
    )
    equivalent = (ModelRate("a", "A", "7", "0"), ModelRate("z", "Z", "1.5"))
    assert (
        save_version_prices(
            first, "v1", equivalent, revision_id="unused", expected_revision=first.revision, at=T0
        )
        is first
    )
    assert first.revisions[:-1] == catalog.revisions


@pytest.mark.parametrize("change", ["display", "model_id", "price", "rule", "rule_metadata"])
def test_changes_to_snapshot_fields_append_revision(catalog: Catalog, change: str) -> None:
    rule = BillingRule("tier", PriceRates("2"), (1,), (0,), 0, 10)
    base = replace(catalog.revisions[0].models[0], billing_rule=rule)
    catalog = replace(
        catalog, revisions=(replace(catalog.revisions[0], models=(base,)), *catalog.revisions[1:])
    )
    changed = {
        "display": replace(base, display_name="New display"),
        "model_id": replace(base, model_id="MODEL A"),
        "price": replace(base, input=None),
        "rule": replace(base, billing_rule=None),
        "rule_metadata": replace(base, billing_rule=replace(rule, hours=(1,))),
    }[change]
    updated = save_version_prices(
        catalog, "v1", (changed,), revision_id="r5", expected_revision=catalog.revision, at=T1
    )
    assert len(updated.revisions) == len(catalog.revisions) + 1
    assert updated.revision_at("v1", T0).models == (base,)
    assert updated.revision_at("v1", T1).models == (changed,)


@pytest.mark.parametrize("future_record", ["revision", "activation", "selection", "version_creation"])
def test_P07_clock_guard_is_global(catalog: Catalog, future_record: str) -> None:
    if future_record == "revision":
        catalog = replace(catalog, revisions=(*catalog.revisions, PriceRevision("future", "v4", T2, 10, ())))
    elif future_record == "activation":
        catalog = replace(catalog, activations=(*catalog.activations, VersionActivation("g2", "v4", T2, 10)))
    elif future_record == "selection":
        catalog = replace(
            catalog, selections=(*catalog.selections, HarnessSelection(Harness.DSH, "g1", T2, 10))
        )
    else:
        catalog = replace(
            catalog, versions=(*catalog.versions[:3], replace(catalog.versions[3], created_at=T2))
        )
    with pytest.raises(DomainError) as failure:
        save_version_prices(catalog, "v1", (), revision_id="new", expected_revision=catalog.revision, at=T1)
    assert failure.value.code == CLOCK_BEHIND_PRICE_HISTORY
    assert (
        save_version_prices(catalog, "v1", (), revision_id="new", expected_revision=catalog.revision, at=T2)
        .revisions[-1]
        .effective_at
        == T2
    )


def test_stale_save_rejected_even_when_prices_are_identical(catalog: Catalog) -> None:
    with pytest.raises(DomainError) as failure:
        save_version_prices(
            catalog,
            "v1",
            catalog.revisions[0].models,
            revision_id="new",
            expected_revision=catalog.revision - 1,
            at=T1,
        )
    assert failure.value.code == PRICING_REVISION_CONFLICT


@pytest.mark.parametrize(
    ("group", "version", "selection_count", "activation_count", "affected"),
    [
        ("g1", "v1", 0, 0, ()),
        ("g2", "v3", 1, 0, (Harness.CODEX,)),
        ("g1", "v2", 0, 1, (Harness.CODEX,)),
        ("g2", "v4", 1, 1, (Harness.CODEX, Harness.ZCODE, Harness.DSH)),
    ],
)
def test_switch_four_combinations(
    catalog: Catalog,
    group: str,
    version: str,
    selection_count: int,
    activation_count: int,
    affected: tuple[Harness, ...],
) -> None:
    plan = plan_switch(
        catalog,
        Harness.CODEX,
        group,
        version,
        expected_revision=catalog.revision,
        operation_id="switch",
        at=T1,
    )
    assert len(plan.selections) == selection_count
    assert len(plan.activations) == activation_count
    assert plan.affected_harnesses == affected
    assert plan.changed == bool(selection_count or activation_count)
    records = (*plan.selections, *plan.activations)
    assert [record.seq for record in records] == list(
        range(catalog.sequence + 1, catalog.sequence + len(records) + 1)
    )
    assert all(record.effective_at == T1 for record in records)
    applied = apply_switch(catalog, plan)
    assert applied.revision == catalog.revision + int(plan.changed)
    assert applied.resolve(Harness.CODEX, T1).version == catalog.version(version)
    assert applied.resolve(Harness.CODEX, T0) == catalog.resolve(Harness.CODEX, T0)
    if activation_count and group == "g2":
        assert applied.resolve(Harness.ZCODE, T1).version == catalog.version(version)
        assert applied.resolve(Harness.DSH, T1).version == catalog.version(version)
    assert catalog.switches == ()


@pytest.mark.parametrize(("group", "version"), [("g1", "v1"), ("g2", "v4")])
def test_switch_idempotency_preserves_first_result_after_later_changes(
    catalog: Catalog, group: str, version: str
) -> None:
    first = plan_switch(
        catalog, Harness.CODEX, group, version, expected_revision=catalog.revision, operation_id="op", at=T1
    )
    applied = apply_switch(catalog, first)
    later = rename_group(applied, "g1", "Renamed later")
    replay = plan_switch(
        later, Harness.CODEX, group, version, expected_revision=-99, operation_id="op", at=T0
    )
    assert replay is first
    assert apply_switch(later, replay) is later
    with pytest.raises(DomainError) as failure:
        plan_switch(
            later, Harness.DSH, group, version, expected_revision=later.revision, operation_id="op", at=T2
        )
    assert failure.value.code == OPERATION_ID_REUSED


def test_failed_or_stale_switch_does_not_partially_apply(catalog: Catalog) -> None:
    before = catalog.fingerprint
    with pytest.raises(DomainError) as failure:
        plan_switch(
            catalog, Harness.CODEX, "g1", "v3", expected_revision=catalog.revision, operation_id="bad", at=T1
        )
    assert failure.value.code == VERSION_GROUP_MISMATCH
    plan = plan_switch(
        catalog, Harness.CODEX, "g2", "v4", expected_revision=catalog.revision, operation_id="op", at=T1
    )
    with pytest.raises(DomainError):
        apply_switch(catalog, replace(plan, activations=()))
    later = rename_group(catalog, "g1", "Renamed")
    with pytest.raises(DomainError) as failure:
        apply_switch(later, plan)
    assert failure.value.code == PRICING_REVISION_CONFLICT
    assert catalog.fingerprint == before
    assert catalog.switches == later.switches == ()
    assert later.activations == catalog.activations
    assert later.selections == catalog.selections


def test_referenced_group_deletion_preserves_history(catalog: Catalog) -> None:
    catalog = third(catalog)
    catalog = save_editor_hints(catalog, EditorHints("v5", "Model A", "2"))
    assert deletion_reason(catalog, "g3", ledger_referenced=True, recalc_referenced=True) is None
    deleted = delete_group(catalog, "g3", ledger_referenced=True, recalc_referenced=True, at=T1)
    assert deleted.group("g3").deleted_at == T1
    assert deleted.versions == catalog.versions
    assert deleted.revisions == catalog.revisions
    assert deleted.activations == catalog.activations
    assert deleted.hints == catalog.hints
    assert deleted.sequence == catalog.sequence
    with pytest.raises(DomainError) as failure:
        delete_group(deleted, "g3", ledger_referenced=False, recalc_referenced=False)
    assert failure.value.code == GROUP_REFERENCED
    only = create_group(Catalog(), group_id="g", version_id="v", revision_id="r", name="Only", at=T0)
    assert delete_group(only, "g", ledger_referenced=False, recalc_referenced=False).group("g").deleted_at


def test_deleted_current_group_resolves_historical_time_only(catalog: Catalog) -> None:
    deleted = delete_group(catalog, "g1", ledger_referenced=True, recalc_referenced=False, at=T1)
    assert deleted.resolve(Harness.CODEX, T0).revision == catalog.resolve(Harness.CODEX, T0).revision
    assert deleted.resolve(Harness.CODEX, T1).group is None
    assert deleted.selections == catalog.selections


def test_disable_in_use_and_disabled_write_rules(catalog: Catalog) -> None:
    with pytest.raises(DomainError) as failure:
        set_group_disabled(catalog, "g2", True)
    assert failure.value.code == GROUP_IN_USE
    disabled = set_group_disabled(third(catalog), "g3", True)
    with pytest.raises(DomainError) as failure:
        add_version(disabled, "g3", version_id="new", revision_id="new-r", number=2, label="", at=T1)
    assert failure.value.code == GROUP_DISABLED
    with pytest.raises(DomainError):
        save_version_prices(
            disabled, "v5", (), revision_id="new-r", expected_revision=disabled.revision, at=T1
        )
    with pytest.raises(DomainError):
        plan_switch(
            disabled, Harness.CODEX, "g3", "v5", expected_revision=disabled.revision, operation_id="op", at=T1
        )
    assert not set_group_disabled(disabled, "g3", False).group("g3").disabled


def test_P09_group_limit_includes_disabled_groups() -> None:
    catalog = Catalog()
    for i in range(100):
        catalog = create_group(
            catalog, group_id=f"g{i}", version_id=f"v{i}", revision_id=f"r{i}", name=f"Group {i}", at=T0
        )
        catalog = set_group_disabled(catalog, f"g{i}", True)
    with pytest.raises(DomainError) as failure:
        create_group(catalog, group_id="g101", version_id="v101", revision_id="r101", name="101", at=T0)
    assert failure.value.code == GROUP_LIMIT


def test_name_and_version_uniqueness(catalog: Catalog) -> None:
    assert rename_group(catalog, "g1", " Group 1 ") is catalog
    assert rename_group(catalog, "g1", "group 2").group("g1").name == "group 2"
    with pytest.raises(DomainError) as failure:
        rename_group(catalog, "g1", "Group 2")
    assert failure.value.code == GROUP_DUPLICATE
    with pytest.raises(DomainError) as failure:
        add_version(catalog, "g1", version_id="v5", revision_id="r5", number=2, label="", at=T1)
    assert failure.value.code == VERSION_DUPLICATE


def test_rule_sorting_and_duplicate_time_entries_do_not_append(catalog: Catalog) -> None:
    rule = BillingRule("peak", PriceRates("1.50"), (7, 1, 1), (23, 0, 0), 480, 100)
    model = ModelRate("Model A", "A", billing_rule=rule)
    first = save_version_prices(
        catalog, "v1", (model,), revision_id="r5", expected_revision=catalog.revision, at=T1
    )
    reordered = replace(model, billing_rule=BillingRule("peak", PriceRates("1.5"), (1, 7), (0, 23), 480, 100))
    assert (
        save_version_prices(
            first, "v1", (reordered,), revision_id="unused", expected_revision=first.revision, at=T2
        )
        is first
    )


def test_noop_switch_checks_latest_state_even_if_clock_has_gone_back(catalog: Catalog) -> None:
    first = plan_switch(
        catalog, Harness.CODEX, "g1", "v2", expected_revision=catalog.revision, operation_id="first", at=T1
    )
    applied = apply_switch(catalog, first)
    noop = plan_switch(
        applied, Harness.CODEX, "g1", "v2", expected_revision=applied.revision, operation_id="noop", at=T0
    )
    assert not noop.changed
    with pytest.raises(DomainError) as failure:
        plan_switch(
            applied, Harness.CODEX, "g1", "v1", expected_revision=applied.revision, operation_id="back", at=T0
        )
    assert failure.value.code == CLOCK_BEHIND_PRICE_HISTORY


def test_unknown_entities_and_failed_copy_leave_catalog_unchanged(catalog: Catalog) -> None:
    before = catalog.fingerprint
    with pytest.raises(DomainError):
        create_group(
            catalog,
            group_id="g3",
            version_id="v5",
            revision_id="r5",
            name="Copy",
            at=T1,
            copy_from_group_id="missing",
        )
    with pytest.raises(DomainError):
        add_version(
            catalog,
            "g1",
            version_id="v5",
            revision_id="r5",
            number=5,
            label="",
            at=T1,
            copy_from_version_id="missing",
        )
    with pytest.raises(DomainError):
        plan_switch(
            catalog,
            Harness.CODEX,
            "missing",
            "v1",
            expected_revision=catalog.revision,
            operation_id="op",
            at=T1,
        )
    assert catalog.fingerprint == before
