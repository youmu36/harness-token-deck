"""Independent request-to-wallet vectors when a later target introduces a billing rule."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from htd.adapters.sqlite.history_repo import HistoryRepository
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import parse_instant
from htd.domain.usage.model import Tokens
from htd.ipc.dto.maintenance import RecalcPreview, RecalcResponse

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.maintenance_helpers import add_usage
from backend.tests.contracts.management_helpers import catalog, group, price, vendor, wallet
from backend.tests.integration.collection_helpers import usage


@pytest.mark.parametrize(
    ("mode", "inputs", "write", "expected", "branch", "incomplete"),
    [
        ("tier", 199_999, 0, 250_123_000_000, "base", False),
        ("tier", 200_000, 0, 250_125_000_000, "base", False),
        ("tier", 200_001, 0, 500_254_000_000, "tier", False),
        ("tier", 200_001, None, 250_000_000, "tier", True),
        ("peak", 200_000, 0, 500_250_000_000, "peak", False),
    ],
)
def test_new_target_rule_reprices_full_request_and_projection(
    tmp_path: Path,
    mode: str,
    inputs: int,
    write: int | None,
    expected: int,
    branch: Literal["none", "base", "peak", "tier"],
    incomplete: bool,
) -> None:
    clock = FakeClock(parse_instant("2026-09-30T06:00:00.000Z"))
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        g, v = group(app)
        base = {"modelId": "gpt-6.1 sol", "input": "2", "cache": "1", "cacheWrite": "0", "output": "5"}
        price(app, "pricing.saveVersionPrices", versionId=v, models=[base])
        price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
        original = vendor(app, initial="10")
        wallet(
            app,
            "wallet.linkGroups",
            vendorId=original.id,
            groupIds=[g],
            expectedRevision=app.revisions()["wallet"],
        )
        clock.advance(1000)
        add_usage(
            app,
            [usage(1, at=clock.now_utc(), model="gpt-6.1 sol", tokens=Tokens(inputs, 150_000, write, 25, 0))],
        )
        clock.advance(1000)
        rule = {
            "mode": mode,
            "thresholdK": 200,
            "weekdays": [3],
            "hours": [6],
            "offsetMinutes": 0,
            "rates": {"input": "4", "cache": "2", "cacheWrite": "3", "output": "10"},
        }
        price(app, "pricing.saveVersionPrices", versionId=v, models=[base | {"billingRule": rule}])
        saved_rule = catalog(app).groups[0].versions[0].models[0].billingRule
        assert saved_rule is not None and saved_rule.rates.input == "4"
        params = {
            "harness": "codex",
            "start": "2026-09-30T06:00:00.000Z",
            "end": "2026-09-30T06:00:02.000Z",
            "groupId": g,
            "versionId": v,
        }
        preview = invoke(app, "maintenance.previewRecalc", params, RecalcPreview)
        assert int(preview.newTotal) == expected
        assert preview.ruleCounts[branch] == 1
        assert preview.incompleteCount == int(incomplete)
        result = invoke(
            app,
            "maintenance.commitRecalc",
            params | {"fingerprint": preview.fingerprint, "operationId": str(uuid4())},
            RecalcResponse,
        )
        db = app.readers.get()
        assert db.execute("SELECT pico,rule_applied FROM ledger").fetchone()[:] == (str(expected), branch)
        assert db.execute("SELECT records,input,cache_read,estimate FROM session_total").fetchone()[:] == (
            "1",
            str(inputs),
            "150000",
            str(expected),
        )
        assert (
            db.execute("SELECT vendor_id FROM wallet_entry WHERE kind='consumption'").fetchone()[0]
            == original.id
        )
        assert result.newTotal == preview.newTotal


def test_preview_counts_accepted_but_unvalued_without_billing_on_read(tmp_path: Path) -> None:
    clock = FakeClock(parse_instant("2026-09-30T06:00:00.000Z"))
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        g, v = group(app)
        app.writer.execute(
            lambda work: HistoryRepository(work.connection).import_events(
                work,
                (usage(1, at=clock.now_utc()),),
                roots={s.harness: s.path for s in app.store.sources().sources},
                at=clock.now_utc(),
            )
        )
        result = invoke(
            app,
            "maintenance.previewRecalc",
            {
                "harness": "codex",
                "start": "2026-09-30T06:00:00.000Z",
                "end": "2026-09-30T06:00:01.000Z",
                "groupId": g,
                "versionId": v,
            },
            RecalcPreview,
        )
        assert (result.totalCount, result.recordCount, result.pendingCount, result.unavailableCount) == (
            1,
            0,
            1,
            1,
        )
        assert app.readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
