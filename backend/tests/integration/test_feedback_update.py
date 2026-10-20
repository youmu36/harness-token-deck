"""Current feedback: selective queries, durable version lifecycle and auditable drift."""

import json
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
from htu.adapters.sqlite.account_book import AccountBook
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.ledger_read import LedgerReader
from htu.adapters.sqlite.remote_store import SqliteRemoteStore
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import WriteExecutor
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, parse_instant
from htu.domain.statistics import Aggregator
from htu.domain.usage.model import Harness

from backend.tests.contracts.command_helpers import seed_rows
from backend.tests.contracts.management_helpers import catalog, group, price
from backend.tests.unit.statistics.helpers import row

from .test_remote_wallet import ACCOUNT, balance
from .test_wallet_storage import USD, vendor


def request(app: Application, **extra: object):
    response = app.router.call(
        "stats.summary",
        {
            "harness": "codex",
            "start": "2026-09-01T00:00:00.000Z",
            "end": "2026-09-14T02:00:00.000Z",
            "offsetMinutes": 345,
            "grain": "day",
            **extra,
        },
    )
    assert response["ok"], response
    return response["data"]


def test_selective_queries_reconcile_and_cache_unaffected_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    clock = FakeClock(parse_instant("2026-09-14T02:00:00Z"))
    with Application(tmp_path / "data", clock=clock, start_scheduler=False) as app:
        seed_rows(app, [row(1, day=12), row(2, day=14), row(3, day=14, model="b")])
        full = request(app)
        reduced = request(app, sections=["models"])
        assert reduced["metrics"] == full["metrics"] and reduced["models"] == full["models"]
        assert reduced["trend"] == reduced["hourly"] == reduced["hourlyTotals"] == []
        selected = request(app, sections=["hourly"], modelKey="b")
        assert len(selected["hourly"]) == 1 and selected["hourly"][0]["modelKey"] == "b"
        app.statistics.invalidate()
        calls = []
        original = LedgerReader.iter_rows

        def counted(self, start, end, *, harness="all"):
            calls.append((start, end))
            return original(self, start, end, harness=harness)

        monkeypatch.setattr(LedgerReader, "iter_rows", counted)
        request(app, sections=["models"])
        assert len(calls) == 2
        seed_rows(app, [row(4, day=14, harness=Harness.CLAUDE)])
        request(app, sections=["models"])
        assert len(calls) == 2
        seed_rows(app, [row(5, day=14)])
        request(app, sections=["models"])
        assert len(calls) == 3  # Only today's tail.
        seed_rows(app, [row(6, day=12)])
        assert request(app, sections=["models"])["metrics"]["recordCount"] == 5
        assert len(calls) == 4  # Late historical data invalidates the prefix.


def test_light_status_and_tick_never_count_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with Application(tmp_path / "data", start_scheduler=False) as app:

        def forbidden(*args, **kwargs):
            raise AssertionError("light status counted historical rows")

        monkeypatch.setattr(LedgerReader, "source_counts", forbidden)
        result = app.router.call("collection.status", {"details": False})
        assert result["ok"], result
        assert all(source["ledgerCount"] is None for source in result["data"]["sources"])


def test_version_disable_delete_preserves_history_and_blocks_selection(tmp_path: Path):
    directory = tmp_path / "data"
    with Application(directory, start_scheduler=False) as app:
        gid, first = group(app)
        second = price(app, "pricing.addVersion", groupId=gid, number=2).versionId
        assert second
        price(app, "pricing.setVersionDisabled", versionId=second, disabled=True)
        assert next(v for v in catalog(app).groups[0].versions if v.id == second).disabled
        failed = app.router.call(
            "pricing.activateVersion",
            {
                "groupId": gid,
                "versionId": second,
                "operationId": str(uuid4()),
                "expectedRevision": app.revisions()["pricing"],
            },
        )
        assert not failed["ok"] and failed["error"]["code"] == "VERSION_DISABLED"
        price(app, "pricing.setVersionDisabled", versionId=second, disabled=False)
        price(app, "pricing.activateVersion", groupId=gid, versionId=second)
        price(app, "pricing.deleteVersion", versionId=first)
        assert [v.id for v in catalog(app).groups[0].versions] == [second]
        assert (
            app.writer.execute(
                lambda w: w.connection.execute(
                    "SELECT COUNT(*) FROM price_revision WHERE version_id=?", (first,)
                ).fetchone()[0]
            )
            == 1
        )
    with Application(directory, start_scheduler=False) as app:
        assert [v.id for v in catalog(app).groups[0].versions] == [second]


def test_balance_inference_includes_exact_one_dollar_and_repeats_once(tmp_path: Path):
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(vendor)
        store = SqliteRemoteStore(writer, readers)
        store.execute(lambda w: store.save_account(w, ACCOUNT, clear=True))
        store.execute(lambda w: store.observe(w, balance(0)))
        for minute in (5, 6):
            store.execute(lambda w, minute=minute: store.observe(w, balance(USD, minute)))
        store.flush_pending(Instant(balance(USD, 6).fetched_at.value + timedelta(minutes=10)))
        account = AccountBook(readers.get()).summary("vendor")
        assert account["R"] == str(USD) and account["B"] == str(USD)
        store.execute(lambda w: store.observe(w, balance(3 * USD, 7)))
        store.flush_pending(Instant(balance(3 * USD, 7).fetched_at.value + timedelta(minutes=10)))
        assert AccountBook(readers.get()).summary("vendor")["R"] == str(3 * USD)
        assert WalletRepository(readers.get()).totals("vendor").r == 0


def test_selective_aggregation_measurement(tmp_path: Path):
    start = parse_instant("2026-09-01T00:00:00Z")
    end = Instant(start.value + timedelta(days=30))
    rows = [
        row(i, day=1 + i % 28, hour=i % 24, model=f"model-{i % 20}", session=f"s-{i}") for i in range(10000)
    ]
    rows.sort(key=lambda item: item.order_key)
    results = []
    for sections in (None, frozenset(("models",))):
        args = {} if sections is None else {"sections": sections}
        aggregate = Aggregator(480, "day", start, end, **args)
        began = perf_counter()
        for item in rows:
            aggregate.push(item)
        summary = aggregate.finish()
        results.append(
            {
                "seconds": perf_counter() - began,
                "estimatedBytes": aggregate.estimated_bytes(),
                "responseBytes": len(json.dumps(summary.to_dict()).encode()),
                "metrics": summary.metrics.to_dict(),
            }
        )
    assert results[0]["metrics"] == results[1]["metrics"]
    assert results[1]["responseBytes"] < results[0]["responseBytes"] / 2
    print("SELECTIVE_QUERY_MEASUREMENT", json.dumps(results))  # noqa: T201
