import pytest
from htu.domain.common.time import parse_instant
from htu.domain.pricing.catalog import Catalog
from htu.domain.pricing.model import (
    HarnessSelection,
    ModelRate,
    PriceGroup,
    PriceRevision,
    PriceVersion,
    VersionActivation,
)
from htu.domain.usage.model import Harness


@pytest.fixture
def catalog() -> Catalog:
    at = parse_instant("2026-09-01T00:00:00Z")
    return Catalog(
        groups=(PriceGroup("g1", "Group 1", False, at), PriceGroup("g2", "Group 2", False, at)),
        versions=tuple(
            PriceVersion(f"v{i}", "g1" if i <= 2 else "g2", 1 if i % 2 else 2, "", at) for i in range(1, 5)
        ),
        revisions=tuple(
            PriceRevision(f"r{i}", f"v{i}", at, i, (ModelRate("Model A", "Display A", str(i), "0.1", "2"),))
            for i in range(1, 5)
        ),
        activations=(VersionActivation("g1", "v1", at, 5), VersionActivation("g2", "v3", at, 6)),
        selections=(
            HarnessSelection(Harness.CODEX, "g1", at, 7),
            HarnessSelection(Harness.ZCODE, "g2", at, 8),
            HarnessSelection(Harness.DSH, "g2", at, 9),
        ),
        revision=10,
    )
