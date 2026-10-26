"""Cross-query effective-cost conservation from independent financial target vectors."""

from datetime import timedelta
from pathlib import Path

import pytest
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import format_instant
from htd.domain.statistics import Window
from htd.domain.usage.model import Harness, Tokens
from htd.ipc.dto.explorer import ExploreResponse
from htd.ipc.dto.statistics import AnalysisResponse, LongTermResponse

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.management_helpers import catalog, price

from .collection_helpers import AT, usage
from .test_calibration_targets import apply, proposal
from .test_reconciliation_batches import USD, configured, receive


@pytest.mark.parametrize("allocated", [False, True])
def test_locked_cost_is_shared_by_summary_explore_analytics_and_long_term(
    tmp_path: Path, allocated: bool
) -> None:
    clock = FakeClock(AT)
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        _, supplier = configured(app)
        version = catalog(app).groups[0].versions[0].id
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=version,
            models=[{"modelId": "Model", "input": "8", "cache": "0", "output": "0"}],
        )
        clock.advance(60_000)
        receive(app, usage(1, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0)))
        clock.advance(11 * 60_000)
        parameters = proposal(app, supplier, 10 * USD)
        if allocated:
            parameters["allocations"] = [
                {
                    "day": "2026-09-30",
                    "targetPico": str(10 * USD),
                    "modelKeys": ["model"],
                    "confirmModelCoverage": True,
                }
            ]
        apply(app, parameters)
        start, end = AT, clock.now_utc()
        summary = app.statistics.summary("all", Window(start, end, 0, "hour"))[0]
        assert summary.metrics.pico_usd == str(10 * USD)
        assert summary.metrics.estimate_pico == str(8 * USD)
        assert summary.metrics.correction_pico == str(2 * USD)
        expected_model = str((10 if allocated else 8) * USD)
        assert summary.models[0].metrics.pico_usd == expected_model
        assert summary.harnesses[0].metrics.pico_usd == str(10 * USD)
        assert sum(int(row.pico_usd) for row in summary.trend) == 10 * USD
        query = {"start": format_instant(start), "end": format_instant(end), "grain": "day"}
        data = invoke(app, "stats.explore", query, ExploreResponse)
        assert data.totals.picoUsd == str(10 * USD) and data.models[0].picoUsd == expected_model
        assert sum(int(row.picoUsd) for row in data.harnesses) == int(data.totals.picoUsd)
        compared = invoke(
            app,
            "stats.explore",
            dict(query, sections=["totals", "comparison"], compareDimension="model", compareIds=["model"]),
            ExploreResponse,
        )
        assert sum(int(row.picoUsd) for row in compared.comparisons[0].trend) == int(expected_model)
        assert data.totals.estimatePico == str(8 * USD) and data.totals.correctionPico == str(2 * USD)
        assert data.models[0].tokensPerDollar == ("100000.0" if allocated else None)
        assert data.models[0].ratioUnavailable is not allocated
        assert sum(int(row.picoUsd) for row in data.trend) == 10 * USD
        scoped = invoke(app, "stats.explore", dict(query, modelKeys=["model"]), ExploreResponse)
        assert scoped.totals.picoUsd == expected_model
        assert scoped.totals.unallocatedCorrectionPico == ("0" if allocated else str(2 * USD))
        analytics = invoke(
            app,
            "stats.analytics",
            {"harness": "all", "period": "range", "start": query["start"], "end": query["end"]},
            AnalysisResponse,
        )
        assert analytics.totals.picoUsd == str(10 * USD)
        assert analytics.models[0].picoUsd == expected_model
        # Long-term reads span more than thirty days and include the current authority layer.
        long = invoke(
            app,
            "stats.longTerm",
            {
                "harness": "all",
                "start": format_instant(start),
                "end": format_instant(type(end)(end.value + timedelta(days=100))),
            },
            LongTermResponse,
        )
        assert long.totals["pico"] == str(10 * USD) and long.totals["input"] == "1000000"
        assert long.totals["estimatePico"] == str(8 * USD)
        assert (
            app.statistics.summary(Harness.CODEX, Window(start, end, 0, "hour"), model_key="model")[
                0
            ].metrics.pico_usd
            == expected_model
        )
