"""Metadata reads, idle cache expiry and paged wallet history stay bounded."""

# pyright: reportPrivateUsage=false
# Inspect cache ownership as well as the public query results.

import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from htd.adapters.sqlite.analysis_cache import AnalysisCache
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.title_repo import TitleRepository
from htd.adapters.sqlite.wallet_entries import daily_spending, wallet_entries
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.application.ports import AuxiliaryChanged, TitleRequest
from htd.bootstrap import Application
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import parse_instant
from htd.domain.statistics import Window
from htd.domain.usage.model import Harness
from htd.ipc.dto.pricing import CatalogResponse
from htd.ipc.dto.wallet import EntriesResponse, WalletListResponse

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.management_helpers import catalog, group, price, vendor

from .test_wallet_storage import vendor as seed_vendor


def test_startup_and_shutdown_leave_unused_http_provider_unloaded(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from pathlib import Path
from htd.adapters.collectors.discovery import Discovery
from htd.bootstrap import Application
root = Path(sys.argv[1])
discovery = Discovery(home=root, environ={})
with Application(root / 'data', discovery=discovery, start_scheduler=False) as app:
    assert app.bridge.call('app.hello', {'clientProtocol': 1})['ok']
    assert app.bridge.call('preferences.get', {})['ok']
    assert 'httpx' not in sys.modules
assert 'httpx' not in sys.modules
""",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_title_only_changes_publish_a_dedicated_signal_once(tmp_path: Path) -> None:
    events: list[object] = []
    at = parse_instant("2026-10-25T00:01:00Z")
    title = TitleRequest("f" * 64, at, "model")
    with WriteExecutor(tmp_path / "a.db", publish=events.append) as writer:
        for _ in range(2):
            writer.execute(lambda work: TitleRepository(work.connection).collect(work, (title,), at=at))
        assert [event for event in events if isinstance(event, AuxiliaryChanged)] == [AuxiliaryChanged(1)]


def test_metadata_catalog_omits_price_history_and_wallet_card_reads_only_selected_vendor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import htd.adapters.sqlite.wallet_command_store as module

    with Application(tmp_path, start_scheduler=False) as app:
        _, version_id = group(app)
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=version_id,
            models=[{"modelId": "model", "input": "1", "cache": "0.1", "output": "3"}],
        )
        complete = catalog(app).model_dump()
        metadata = invoke(app, "pricing.catalog", {"metadataOnly": True}, CatalogResponse).model_dump()
        expected = deepcopy(complete)
        for item in expected["groups"]:
            for version in item["versions"]:
                version.update(models=[], hints=[])
        assert metadata == expected
        assert any(version["models"] for item in complete["groups"] for version in item["versions"])
        first, second = vendor(app, "First"), vendor(app, "Second")
        visited: list[str] = []
        original = module.AccountBook.summary

        def summary(book: Any, identity: str, **values: Any) -> Any:
            visited.append(identity)
            return original(book, identity, **values)

        monkeypatch.setattr(module.AccountBook, "summary", summary)
        response = invoke(app, "wallet.list", {"vendorId": first.id}, WalletListResponse)
        assert visited == [first.id]
        assert [row.id for row in response.vendors] == [first.id]
        assert second.id != first.id


def test_existing_maintenance_releases_unused_caches_without_a_new_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import htd.adapters.sqlite.analysis_cache as cache_module
    import htd.adapters.sqlite.wallet_command_store as wallet_module

    now = [0.0]
    monkeypatch.setattr(cache_module, "monotonic", lambda: now[0])
    monkeypatch.setattr(wallet_module.time, "monotonic", lambda: now[0])
    clock = FakeClock(parse_instant("2026-10-25T00:01:00Z"))
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        app.statistics.summary(
            Harness.CODEX, Window(parse_instant("2026-10-25T00:00:00Z"), clock.now_utc(), 0)
        )
        store = app.statistics.store
        assert isinstance(store, SqliteStatisticsStore)
        store.monitor_data("codex", clock.now_utc())
        cache: AnalysisCache = store.analysis_cache
        future, _ = cache.claim("sample")
        cache.finish("sample", future, {"rows": [1, 2]}, cache.generation)
        first = vendor(app)
        app.wallet.entries(first.id, None, 10)
        assert app.statistics.diagnostics()["entries"]
        assert cache.entries and app.wallet_store._entries_cache
        now[0] = 61
        clock.advance(61_000)
        app.expire_idle_caches()
        assert not cache.entries and cache.bytes == 0
        assert app.statistics.diagnostics()["entries"] == 0
        assert store.monitor_diagnostics()["entries"] == 0
        assert not app.wallet_store._entries_cache and not app.wallet_store._daily_cache


def test_oversize_daily_history_pages_from_closed_amounts_without_loading_all_days(tmp_path: Path) -> None:
    def seed(work: UnitOfWork) -> None:
        seed_vendor(work)
        work.connection.executemany(
            "INSERT INTO wallet_daily_close VALUES('vendor',?,'Asia/Shanghai',?,?,?,1,1)",
            [
                (f"2026-09-{index:02}", str(10**24 + index), str(10**24 + index), index)
                for index in range(1, 6)
            ],
        )

    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        writer.execute(seed)
        with readers.connection() as db:
            assert db.execute("PRAGMA temp_store").fetchone()[0] == 1
            assert daily_spending(db, "vendor", 0, max_days=2) is None
            first = EntriesResponse.model_validate(
                wallet_entries(db, "vendor", None, 2, "consumption_daily", 0)
            )
            second = EntriesResponse.model_validate(
                wallet_entries(db, "vendor", first.nextCursor, 2, "consumption_daily", 0)
            )
            assert [row.seq for row in first.entries] == [5, 4]
            assert [row.seq for row in second.entries] == [3, 2]
            assert first.entries[0].amountPico == str(10**24 + 5)
            assert not db.in_transaction
