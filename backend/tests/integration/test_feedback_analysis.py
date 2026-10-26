"""Comparison structures reuse the same bounded aggregation and effective-cost scope."""

from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.writer import WriteExecutor
from htd.domain.usage.model import Tokens
from htd.ipc.dto.explorer import ExploreResponse

from .collection_helpers import usage
from .test_explorer_043 import AT, bill, request, setup


@pytest.mark.parametrize(
    ("dimension", "identity", "selector"),
    [("model", "gpt-6-astra", "modelKeys"), ("group", "g", "groupIds"), ("version", "v", "versionIds")],
)
def test_comparison_totals_preserve_sample_structure_without_another_read(
    tmp_path: Path, dimension: str, identity: str, selector: str
) -> None:
    with WriteExecutor(tmp_path / "test.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        bill(writer, (usage(1, at=AT, model="gpt-6-astra", tokens=Tokens(1000, 700, 100, 0)),))
        bill(writer, (usage(2, at=AT, model="gpt-6-astra", tokens=Tokens(2000, 300, 250, 0)),), "v2")
        store = SqliteStatisticsStore(pool, writer)
        comparison = ExploreResponse.model_validate(
            store.explore(
                request(compareDimension=dimension, compareIds=[identity], sections=["comparison"]), AT
            )
        ).comparisons[0]
        scoped = ExploreResponse.model_validate(store.explore(request(**{selector: [identity]}), AT))
        assert comparison.totals == scoped.totals
        assert comparison.totals.parts == scoped.totals.parts
        assert sum(int(point.recordCount) for point in comparison.trend) == int(scoped.totals.recordCount)
