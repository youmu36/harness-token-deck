"""Regressions for request identity, evidence acknowledgement and scoped analysis reads."""

import hashlib
import json
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from htd.adapters.sqlite import account_analysis, explorer_read, usage_archive
from htd.adapters.sqlite.account_book import AccountBook
from htd.adapters.sqlite.analysis_cache import AnalysisCache
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.counter_codec import compress, decompress
from htd.adapters.sqlite.output_speed import close_quiet
from htd.adapters.sqlite.projection_snapshot import ProjectionSnapshot
from htd.adapters.sqlite.rollup_repo import RollupRepository
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.application.ports.query_control import ReadControl, analysis_read, cancel_analysis
from htd.domain.common.errors import DomainError
from htd.domain.common.time import Instant, format_instant
from htd.domain.remote.accounting import CENT
from htd.domain.usage.model import Tokens
from htd.ipc.dto.explorer import ExploreOptions, ExploreRequest, ExploreResponse, OutputReferencesResponse

from .collection_helpers import usage
from .test_account_book import observe
from .test_account_book import setup as setup_account
from .test_explorer_043 import AT, bill, request, setup
from .test_remote_wallet import balance
from .test_wallet_storage import USD, consumption


def result(
    store: SqliteStatisticsStore, query: dict[str, Any] | None = None, at: Instant = AT
) -> ExploreResponse:
    return ExploreResponse.model_validate(store.explore(query or request(), at))


def test_same_timestamp_requests_preserve_versions_vendors_and_published_reference(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        first = replace(usage(1, at=AT, model="gpt-6-astra"), generation_milliseconds=1000)
        bill(writer, (first,))

        def other(work: UnitOfWork) -> None:
            wallet = WalletRepository(work.connection)
            wallet.create_vendor(work, vendor_id="other", name="Other", at=AT, operation_id="create-other")
            wallet.link_group(work, "g", "other", at=AT)

        writer.execute(other)
        bill(writer, (replace(first, dedupe_key=f"{2:064x}", request_id="second"),), "v2")
        rows = (
            pool.get()
            .execute(
                "SELECT vendor_id,version_id,output_tokens FROM output_session_scope ORDER BY version_id"
            )
            .fetchall()
        )
        assert [tuple(row) for row in rows] == [("vendor", "v", "10"), ("other", "v2", "10")]
        quiet = Instant(AT.value + timedelta(minutes=11))
        writer.execute(lambda w: close_quiet(w.connection, quiet))
        store = SqliteStatisticsStore(pool)
        scoped: dict[str, object] = {
            "modelKeys": ["gpt-6-astra"],
            "vendorIds": ["vendor"],
            "versionIds": ["v"],
        }
        assert (
            OutputReferencesResponse.model_validate(store.output_references(scoped, quiet))
            .references[0]
            .tokensPerSecond
            == "10.0"
        )
        later = Instant(AT.value + timedelta(minutes=12))
        bill(
            writer,
            (replace(first, dedupe_key=f"{3:064x}", request_id="third", occurred_at=format_instant(later)),),
            at=later,
        )
        # Resuming the session preserves the last published scoped and ordinary sample.
        assert (
            OutputReferencesResponse.model_validate(store.output_references(scoped, later))
            .references[0]
            .tokensPerSecond
            == "10.0"
        )
        assert (
            OutputReferencesResponse.model_validate(
                store.output_references({"modelKeys": ["gpt-6-astra"]}, later)
            )
            .references[0]
            .samples
            == 1
        )


def test_retirement_and_monthly_folding_do_not_change_missing_count_or_ack(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        bill(
            writer,
            (
                usage(1, at=AT, model="gpt-6-astra", tokens=Tokens(input=100, output=10)),
                usage(2, at=AT, model="gpt-6-astra"),
            ),
        )
        store = SqliteStatisticsStore(pool, writer)
        before = result(store)
        issue = before.quality[0]
        assert issue.recordCount == "1" and issue.countAccuracy == "exact"
        assert (
            store.acknowledge_quality(
                [{"id": issue.id, "fingerprint": issue.fingerprint, "revision": before.revision}], AT
            )["acknowledged"]
            == 1
        )
        for days in (4, 1200):
            later = Instant(AT.value + timedelta(days=days))
            writer.execute(
                lambda w, later=later: RollupRepository(w.connection).compact(
                    w, at=later, publish_daily=False
                )
            )
            after = result(store, at=later)
            assert after.quality[0].fingerprint == issue.fingerprint
            assert after.quality[0].recordCount == "1" and after.quality[0].acknowledged


def test_quality_reason_filter_precedes_pagination_and_keeps_evidence_identity(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        bill(writer, (usage(1, at=AT, model="gpt-6-astra", tokens=Tokens(input=100, output=10)),))
        store = SqliteStatisticsStore(pool, writer)
        before = result(store, request(sections=["quality"]))
        issue = before.quality[0]
        filtered = result(store, request(sections=["quality"], qualityReasons=["cache"]))
        assert filtered.quality[0].fingerprint == issue.fingerprint
        assert filtered.qualityTotal == 1
        assert not result(store, request(sections=["quality"], qualityReasons=["version"])).quality
        store.acknowledge_quality([{"id": issue.id, "fingerprint": issue.fingerprint}], AT)
        assert not result(
            store, request(sections=["quality"], qualityReasons=["cache"], qualityStatus="unacknowledged")
        ).quality
        confirmed = result(
            store, request(sections=["quality"], qualityReasons=["cache"], qualityStatus="acknowledged")
        )
        assert confirmed.quality[0].id == issue.id


@pytest.mark.parametrize("legacy_ack", [False, True])
def test_legacy_quality_flags_are_a_lower_bound(tmp_path: Path, legacy_ack: bool) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        bill(
            writer,
            (
                usage(1, at=AT, model="gpt-6-astra", tokens=Tokens(input=100, output=10)),
                usage(2, at=AT, model="gpt-6-astra"),
            ),
        )
        later = Instant(AT.value + timedelta(days=63))
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later, publish_daily=False))

        def old_format(work: UnitOfWork) -> None:
            row = work.connection.execute("SELECT payload FROM usage_dimensions").fetchone()
            old = {key: value for key, value in decompress(row[0]).items() if not key.startswith("quality_")}
            work.connection.execute("UPDATE usage_dimensions SET payload=?", (compress(old),))
            work.connection.execute("DELETE FROM usage_quality_day")
            work.touch("usage")

        writer.execute(old_format)
        store = SqliteStatisticsStore(pool, writer)
        page = result(store, at=later)
        issue = page.quality[0]
        assert issue.recordCount == "1" and issue.countAccuracy == "at_least"
        if legacy_ack:
            old_key = (
                issue.date,
                issue.harness,
                issue.vendorId,
                issue.groupId,
                issue.versionId,
                issue.modelKey,
                issue.displayName,
                *issue.reasons,
            )
            old_id = hashlib.sha256(json.dumps(old_key, ensure_ascii=False).encode()).hexdigest()
            fingerprint = hashlib.sha256(f"{old_id}:2".encode()).hexdigest()
            writer.execute(
                lambda w: (
                    w.connection.execute(
                        "INSERT INTO quality_notice_ack VALUES(?,?,?)",
                        (old_id, fingerprint, format_instant(AT)),
                    ),
                    w.touch("quality"),
                )
            )
        else:
            store.acknowledge_quality(
                [{"id": issue.id, "fingerprint": issue.fingerprint, "revision": page.revision}], AT
            )
        assert result(store, at=later).quality[0].acknowledged
        far = Instant(AT.value + timedelta(days=1200))
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=far, publish_daily=False))
        folded = result(store, at=far).quality[0]
        assert folded.fingerprint == issue.fingerprint and folded.acknowledged
        assert folded.recordCount == "1" and folded.date == issue.date

        bill(
            writer,
            (
                usage(
                    3,
                    at=Instant(AT.value + timedelta(hours=1)),
                    model="gpt-6-astra",
                    tokens=Tokens(input=100, output=10),
                ),
            ),
            at=far,
        )
        mixed = result(store, at=far).quality[0]
        assert not mixed.acknowledged and mixed.recordCount == "2"
        assert mixed.fingerprint != issue.fingerprint


def test_disjoint_windows_and_stale_acknowledgements_cannot_close_new_evidence(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        event = usage(1, at=AT, model="gpt-6-astra", tokens=Tokens(input=100, output=10))
        bill(writer, (event,))
        store = SqliteStatisticsStore(pool, writer)
        first_query = request()
        first_query["end"] = "2026-10-21T10:30:00.000Z"
        first = result(store, first_query)
        item = {
            "id": first.quality[0].id,
            "fingerprint": first.quality[0].fingerprint,
            "revision": first.revision,
        }
        store.acknowledge_quality([item], AT)
        next_at = Instant(AT.value + timedelta(hours=1))
        bill(
            writer,
            (
                replace(
                    event, dedupe_key=f"{2:064x}", request_id="second", occurred_at=format_instant(next_at)
                ),
            ),
            at=next_at,
        )
        second_query = dict(first_query, start="2026-10-21T11:00:00.000Z", end="2026-10-21T11:30:00.000Z")
        second = result(store, second_query, next_at)
        assert second.quality[0].id == first.quality[0].id
        assert second.quality[0].fingerprint != first.quality[0].fingerprint
        assert not second.quality[0].acknowledged
        assert store.acknowledge_quality([item], next_at) == {"acknowledged": 0, "conflicts": 1}
        fresh = second.quality[0]
        assert (
            store.acknowledge_quality(
                [{"id": fresh.id, "fingerprint": fresh.fingerprint, "revision": second.revision}], next_at
            )["acknowledged"]
            == 1
        )
        assert result(store, second_query, next_at).quality[0].acknowledged


def test_quality_status_is_filtered_before_pagination(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        bill(
            writer,
            tuple(
                usage(i, at=AT, model=f"missing-{i:03}", tokens=Tokens(input=100, output=1))
                for i in range(1, 104)
            ),
        )
        store = SqliteStatisticsStore(pool, writer)
        query = request(sections=["quality"])
        page = result(store, query)
        assert len(page.quality) == 100 and page.qualityNextOffset == 100
        store.acknowledge_quality(
            [
                {"id": item.id, "fingerprint": item.fingerprint, "revision": page.revision}
                for item in page.quality
            ],
            AT,
        )
        remaining = result(store, dict(query, qualityStatus="unacknowledged"))
        assert len(remaining.quality) == remaining.qualityTotal == remaining.qualityUnacknowledged == 3
        assert remaining.qualityNextOffset is None and not any(
            item.acknowledged for item in remaining.quality
        )


def test_batch_comparison_price_cache_paging_and_unknown_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        bill(
            writer,
            tuple(
                usage(
                    i,
                    at=AT,
                    model="gpt-6-astra",
                    tokens=Tokens(input=1000, cache_read=500, cache_write=100, output=100),
                )
                for i in range(1, 41)
            ),
            "v2",
        )
        bill(writer, (usage(100, at=AT, model=None),))
        statements: list[str] = []
        original = explorer_read.explore

        def trace(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, object]:
            db.set_trace_callback(statements.append)
            try:
                return original(db, query, at)
            finally:
                db.set_trace_callback(None)

        monkeypatch.setattr(explorer_read, "explore", trace)
        store = SqliteStatisticsStore(pool)
        query = request(
            sections=["totals", "models", "trend", "comparison"],
            compareDimension="version",
            compareIds=["v", "v2"],
            limit=1,
        )
        combined = result(store, query)
        assert len([sql for sql in statements if "FROM price_revision_model" in sql]) == 1
        assert sum(int(point.totalTokens) for line in combined.comparisons for point in line.trend) == int(
            combined.totals.totalTokens
        )
        assert combined.quality == []
        count = len(statements)
        second = result(store, dict(query, offset=1))
        assert len(statements) == count  # Paging reuses the full immutable projection.
        assert second.models[0].modelKey == ""
        unknown = result(store, request(modelKeys=[""]))
        assert unknown.totals.recordCount == "1"
        assert any(row.id == "" for row in ExploreOptions.model_validate(store.explore_options()).models)


@pytest.mark.parametrize("reliable", [True, False])
def test_remote_method_threshold_late_comparison_and_no_model_allocation(
    tmp_path: Path, reliable: bool
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        remote = setup_account(writer, pool, initial="10", used=0 if reliable else None)
        writer.execute(lambda w: consumption(w, amount=USD, at=balance(0, 1).fetched_at))
        observe(remote, 9 * USD - 2 * CENT, 5, USD + 2 * CENT if reliable else None)
        store = SqliteStatisticsStore(pool)
        query = ExploreRequest(
            start="2026-09-30T00:00:00.000Z", end="2026-10-01T00:00:00.000Z", costBasis="remote"
        ).model_dump()
        now = balance(0, 30).fetched_at
        before = result(store, query, now)
        assert before.accounts[0].status == ("review" if reliable else "estimated")
        assert before.models == before.trend == before.breakdowns == before.quality == []
        assert before.totals.picoUsd == "0" and before.remoteCostPico == str(USD + 2 * CENT)
        original = tuple(pool.get().execute("SELECT e,c FROM account_interval").fetchone())
        writer.execute(lambda w: consumption(w, index=2, amount=2 * CENT - 1, at=balance(0, 2).fetched_at))
        after = result(store, query, now)
        assert after.accounts[0].differencePico == "1"
        assert after.accounts[0].status == ("matched" if reliable else "estimated")
        assert tuple(pool.get().execute("SELECT e,c FROM account_interval").fetchone()) == original
        with pytest.raises(DomainError, match="仅支持"):
            result(store, dict(query, modelKeys=[""]), now)


def test_selected_archive_source_model_is_pushed_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        bill(writer, (usage(1, at=AT, model="gpt-6-astra"), usage(2, at=AT, model="other")))
        later = Instant(AT.value + timedelta(days=40))
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later, publish_daily=False))
        decoded: list[int] = []
        unpack = usage_archive.unpack

        def track(payload: bytes) -> list[dict[str, Any]]:
            decoded.append(1)
            return unpack(payload)

        monkeypatch.setattr(usage_archive, "unpack", track)
        filtered = result(
            SqliteStatisticsStore(pool), request(harnesses=["codex"], modelKeys=["gpt-6-astra"]), later
        )
        assert filtered.totals.recordCount == "1" and len(decoded) == 1
        plan = (
            pool.get()
            .execute(
                "EXPLAIN QUERY PLAN SELECT * FROM usage_dimensions WHERE version_id=? AND bucket<?",
                ("v", "z"),
            )
            .fetchall()
        )
        assert any("usage_dimensions_version" in str(tuple(row)) for row in plan)


def test_single_flight_releases_followers_and_cancelled_read_does_not_poison_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        bill(writer, (usage(1, at=AT, model="gpt-6-astra"),))
        started, release = Event(), Event()
        original = explorer_read.explore
        calls: list[int] = []

        def slow(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, object]:
            calls.append(1)
            started.set()
            assert release.wait(2)
            return original(db, query, at)

        monkeypatch.setattr(explorer_read, "explore", slow)
        store = SqliteStatisticsStore(pool)
        with ThreadPoolExecutor(2) as executor:
            first = executor.submit(store.explore, request(), AT)
            assert started.wait(2)
            second = executor.submit(store.explore, request(), AT)
            release.set()
            assert first.result()["totals"] == second.result()["totals"]
        assert len(calls) == 1
        cancel_analysis("already-hidden")
        with pytest.raises(DomainError, match="取消"), analysis_read("already-hidden"):
            store.explore(request(), AT)


def test_cancelled_owner_preserves_one_shared_read_for_an_active_follower(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        started, joined, release = Event(), Event(), Event()
        original, claim = explorer_read.explore, AnalysisCache.claim
        calls: list[int] = []

        def slow(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, object]:
            calls.append(1)
            started.set()
            assert release.wait(3)
            return original(db, query, at)

        def observed(
            cache: AnalysisCache, key: str, reader: ReadControl | None = None
        ) -> tuple[Future[ProjectionSnapshot], bool]:
            future, owner = claim(cache, key, reader)
            if not owner and not future.done():
                joined.set()
            return future, owner

        monkeypatch.setattr(explorer_read, "explore", slow)
        monkeypatch.setattr(AnalysisCache, "claim", observed)
        store = SqliteStatisticsStore(pool)

        def read(identity: str) -> dict[str, object]:
            with analysis_read(identity):
                return store.explore(request(), AT)

        with ThreadPoolExecutor(2) as executor:
            owner = executor.submit(read, "owner")
            assert started.wait(3)
            follower = executor.submit(read, "follower")
            assert joined.wait(3)
            cancel_analysis("owner")
            release.set()
            with pytest.raises(DomainError, match="取消"):
                owner.result(timeout=3)
            assert follower.result(timeout=3)["costBasis"] == "local"
        assert len(calls) == 1
        assert store.explore(request(), AT)["costBasis"] == "local"
        assert len(calls) == 1 and pool.diagnostics()["active"] == 0


def test_running_sql_is_interrupted_and_reader_is_reusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path, capacity=1) as pool:
        started = Event()
        original = explorer_read.explore

        def long_sql(db: sqlite3.Connection, query: dict[str, Any], at: Instant) -> dict[str, object]:
            started.set()
            db.execute(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
                "SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n"
            ).fetchone()
            return original(db, query, at)

        monkeypatch.setattr(explorer_read, "explore", long_sql)
        store = SqliteStatisticsStore(pool)

        def read() -> dict[str, object]:
            with analysis_read("running"):
                return store.explore(request(), AT)

        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(read)
            assert started.wait(3)
            cancel_analysis("running")
            with pytest.raises(DomainError, match="取消"):
                future.result(timeout=3)
        monkeypatch.setattr(explorer_read, "explore", original)
        assert result(store).totals.recordCount == "0"
        assert pool.diagnostics()["active"] == 0


@pytest.mark.parametrize("kind", ["consumption", "balance"])
def test_remote_unallocated_corrections_are_separate_and_in_the_total(tmp_path: Path, kind: str) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        remote = setup_account(writer, pool, initial="10", used=0)
        writer.execute(lambda w: consumption(w, amount=USD, at=balance(0, 1).fetched_at))
        observe(remote, 9 * USD, 5, USD)
        at = balance(0, 7).fetched_at

        def correct(work: UnitOfWork) -> None:
            book = AccountBook(work.connection)
            if kind == "consumption":
                book.correct_consumption(
                    work, "vendor", 2 * USD, work.revision("account"), "校对累计", "fix", at, remote_only=True
                )
            else:
                book.calibrate_balance(work, "vendor", 8 * USD, "fix", at, remote_only=True)

        writer.execute(correct)
        query = ExploreRequest(
            start="2026-09-30T00:00:00.000Z", end="2026-10-01T00:00:00.000Z", costBasis="remote"
        ).model_dump()
        data = result(SqliteStatisticsStore(pool), query, balance(0, 30).fetched_at)
        assert data.intervalCostPico == data.unallocatedCostPico == str(USD)
        assert data.remoteCostPico == str(2 * USD)
        assert data.accountCorrectionTotal == 1
        assert data.accountCorrections[0].kind == f"{kind if kind == 'balance' else 'consumption'}_correction"
        assert data.accounts[0].costPico == data.accounts[0].currentEstimatePico == str(USD)
        assert data.accounts[0].status == "matched"


def test_remote_vendor_filter_precedes_pagination_and_estimates_are_batched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        remote = setup_account(writer, pool, initial="10", used=0)
        observe(remote, 9 * USD, 5, USD)

        def many(work: UnitOfWork) -> None:
            db = work.connection
            WalletRepository(db).create_vendor(
                work, vendor_id="other", name="Other", at=balance(0, 0).fetched_at, operation_id="other"
            )
            book = AccountBook(db)
            segment = book.ensure_local("other", format_instant(balance(0, 0).fetched_at))
            db.execute("UPDATE account_segment SET kind='remote' WHERE id=?", (segment["id"],))
            previous = book.endpoint(segment["id"], balance(10 * USD, 0), "usd")
            for index in range(1, 1051):
                at = Instant(balance(0, 10).fetched_at.value + timedelta(seconds=index))
                endpoint = book.endpoint(segment["id"], replace(balance(9 * USD, 0), fetched_at=at), "usd")
                identity = f"other-{index:04}"
                db.execute(
                    "INSERT INTO account_interval(id,segment_id,start_endpoint,end_endpoint,e,e_watermark,"
                    "r,c,a,raw_inflow,unresolved,method) "
                    "VALUES(?,?,?,?,'0',0,'0',?,'0','0','0','remote_usage')",
                    (identity, segment["id"], previous["id"], endpoint["id"], str(USD)),
                )
                book.post(
                    work,
                    vendor="other",
                    segment=segment["id"],
                    interval=identity,
                    at=format_instant(at),
                    kind="consumption",
                    c=USD,
                    note="synthetic",
                    operation=identity,
                )
                previous = endpoint

        writer.execute(many)
        statements: list[str] = []
        original = account_analysis.current_estimates

        def traced(db: sqlite3.Connection, intervals: list[sqlite3.Row]) -> dict[str, tuple[int, bool]]:
            db.set_trace_callback(statements.append)
            try:
                return original(db, intervals)
            finally:
                db.set_trace_callback(None)

        monkeypatch.setattr(account_analysis, "current_estimates", traced)
        query = ExploreRequest(
            start="2026-09-30T00:00:00.000Z",
            end="2026-10-01T00:00:00.000Z",
            costBasis="remote",
            vendorIds=["vendor"],
        ).model_dump()
        store = SqliteStatisticsStore(pool)
        data = result(store, query, balance(0, 40).fetched_at)
        assert data.accountTotal == len(data.accounts) == 1 and data.accounts[0].vendorId == "vendor"
        statements.clear()
        first = result(store, dict(query, vendorIds=["other"]), balance(0, 40).fetched_at)
        assert first.accountTotal == 1050 and len(first.accounts) == 100 and first.accountNextOffset == 100
        assert len([sql for sql in statements if "FROM wallet_entry" in sql]) == 1
        last = result(store, dict(query, vendorIds=["other"], accountOffset=1000), balance(0, 40).fetched_at)
        assert len(last.accounts) == 50 and last.accountNextOffset is None
        assert last.remoteCostPico == str(1050 * USD)


def test_model_directory_literal_search_selected_names_and_model_only_reads(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        writer.execute(
            lambda w: w.connection.executemany(
                "INSERT INTO analysis_model_directory VALUES(?,?,?)",
                ((f"m-{i:03}", f"模型 {i}", format_instant(AT)) for i in range(300)),
            )
        )
        store = SqliteStatisticsStore(pool)
        data = ExploreOptions.model_validate(
            store.explore_options({"modelKeys": ["m-299"], "modelSearch": "%", "includeCatalog": False})
        )
        assert data.totalModels == 0 and data.selectedModels[0].name == "模型 299"
        assert data.vendors == data.groups == data.versions == []
        assert not data.groupVendors
        page = ExploreOptions.model_validate(store.explore_options({"modelOffset": 200}))
        assert len(page.models) == 100 and page.models[-1].id == "m-299" and page.modelNextOffset is None
        assert page.groupVendors["g"] == ["vendor"]
