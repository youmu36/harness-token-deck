from dataclasses import replace

import pytest
from htd.domain.common.errors import DomainError
from htd.domain.common.money import parse_rate
from htd.domain.common.time import parse_instant
from htd.domain.pricing.catalog import Catalog, Resolution
from htd.domain.pricing.model import (
    EditorHints,
    HarnessSelection,
    ModelRate,
    PriceRevision,
    VersionActivation,
)
from htd.domain.pricing.rules import rename_group, save_editor_hints, set_version_label
from htd.domain.usage.model import Harness

T0 = parse_instant("2026-09-01T00:00:00Z")
T1 = parse_instant("2026-09-01T01:00:00Z")
T2 = parse_instant("2026-09-01T02:00:00Z")


def test_P01_selection_boundary(catalog: Catalog) -> None:
    changed = replace(
        catalog, selections=(*catalog.selections, HarnessSelection(Harness.CODEX, "g2", T1, 10))
    )
    assert changed.resolve(Harness.CODEX, parse_instant("2026-09-01T00:59:59.999Z")).group == catalog.group(
        "g1"
    )
    assert changed.resolve(Harness.CODEX, T1).group == catalog.group("g2")


def test_P02_before_first_selection(catalog: Catalog) -> None:
    result = catalog.resolve(Harness.CODEX, parse_instant("2026-08-31T23:59:59.999Z"))
    assert result == Resolution()
    assert result.rate_for("Model A") is None
    assert Catalog().resolve(Harness.DSH, T0) == Resolution()


def test_P03_reactivation_uses_latest_revision(catalog: Catalog) -> None:
    catalog = replace(
        catalog,
        revisions=(
            replace(catalog.revisions[0], models=(ModelRate("Model A", "A", "2"),)),
            replace(catalog.revisions[1], models=(ModelRate("Model A", "A", "7"),)),
            *catalog.revisions[2:],
            PriceRevision("new", "v1", T2, 11, (ModelRate("Model A", "A", "4"),)),
        ),
        versions=(catalog.versions[0], replace(catalog.versions[1], number=7), *catalog.versions[2:]),
        activations=(
            *catalog.activations,
            VersionActivation("g1", "v2", T1, 10),
            VersionActivation("g1", "v1", T2, 12),
        ),
    )
    for at, expected in [(T0, 2), (parse_instant("2026-09-01T00:30:00Z"), 2), (T1, 7), (T2, 4)]:
        rate = catalog.resolve(Harness.CODEX, at).rate_for("Model A")
        assert rate is not None and rate.input is not None
        assert parse_rate(rate.input).scaled * 1_000_000 == expected * 10**12


def test_equal_timestamp_uses_global_sequence(catalog: Catalog) -> None:
    catalog = replace(
        catalog,
        selections=(
            *catalog.selections,
            HarnessSelection(Harness.CODEX, "g2", T0, 10),
            HarnessSelection(Harness.CODEX, "g1", T0, 11),
        ),
        activations=(*catalog.activations, VersionActivation("g1", "v2", T0, 12)),
        revisions=(*catalog.revisions, PriceRevision("later", "v2", T0, 13, (ModelRate("m", "M", "9"),))),
    )
    result = catalog.resolve(Harness.CODEX, T0)
    assert result.version == catalog.version("v2")
    assert result.revision is not None and result.revision.id == "later"


def test_initial_revision_fallback_before_version_creation(catalog: Catalog) -> None:
    changed = replace(
        catalog,
        versions=(replace(catalog.versions[0], created_at=T1), *catalog.versions[1:]),
        revisions=(replace(catalog.revisions[0], effective_at=T1), *catalog.revisions[1:]),
    )
    result = changed.resolve(Harness.CODEX, T0)
    assert result.revision == changed.revisions[0]


def test_first_future_activation_preserves_legacy_creation_history(catalog: Catalog) -> None:
    changed = replace(
        catalog, activations=tuple(replace(record, effective_at=T1) for record in catalog.activations)
    )
    result = changed.resolve(Harness.CODEX, T0)
    assert result.group == changed.group("g1")
    assert result.version == changed.version("v2")
    assert result.revision == changed.revision_at("v2", T0)


def test_legacy_fallback_uses_creation_order_not_number(catalog: Catalog) -> None:
    legacy = replace(
        catalog, activations=(), versions=(replace(catalog.versions[0], number=99), *catalog.versions[1:])
    )
    assert legacy.active_version("g1", T0) == legacy.version("v2")
    assert legacy.active_version("g1", parse_instant("2026-08-31T00:00:00Z")) is None


def test_matching_unknown_model_and_disabled_historical_group(catalog: Catalog) -> None:
    catalog = replace(catalog, groups=(replace(catalog.groups[0], disabled=True), catalog.groups[1]))
    result = catalog.resolve(Harness.CODEX, T0)
    assert result.rate_for("Model A") == result.rate_for("\ufeffMODEL\u0085 A")
    assert result.rate_for("other") is None
    assert result.rate_for(None) is None
    assert result.group is not None and result.version is not None


def test_fingerprint_ignores_display_metadata_and_hints(catalog: Catalog) -> None:
    fingerprint = catalog.fingerprint
    changed = rename_group(catalog, "g1", "Renamed")
    changed = set_version_label(changed, "v1", "New label")
    changed = save_editor_hints(changed, EditorHints("v1", "Model A", "1.500"))
    assert changed.fingerprint == fingerprint
    assert len(fingerprint) == 64
    assert replace(catalog, revisions=tuple(reversed(catalog.revisions))).fingerprint == fingerprint
    assert replace(catalog, activations=tuple(reversed(catalog.activations))).fingerprint == fingerprint
    changed_rate = replace(
        catalog.revisions[0], models=(ModelRate("Model A", "Different display", "1", "0.1", "2"),)
    )
    assert replace(catalog, revisions=(changed_rate, *catalog.revisions[1:])).fingerprint != fingerprint
    assert replace(catalog, versions=tuple(reversed(catalog.versions))).fingerprint != fingerprint


@pytest.mark.parametrize("field", ["groups", "versions", "revisions", "activations", "selections"])
def test_rejects_duplicate_entity_or_sequence(catalog: Catalog, field: str) -> None:
    records = getattr(catalog, field)
    with pytest.raises(DomainError):
        replace(catalog, **{field: (*records, records[0])})


def test_rejects_dangling_references_and_incomplete_entities(catalog: Catalog) -> None:
    for changes in [
        {"groups": ()},
        {"versions": ()},
        {"revisions": ()},
        {"activations": (VersionActivation("g1", "v3", T0, 10),)},
        {"selections": (HarnessSelection(Harness.CODEX, "missing", T0, 10),)},
    ]:
        with pytest.raises(DomainError):
            replace(catalog, **changes)
