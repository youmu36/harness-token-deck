"""Dimensional conservation, historical precision, durable acknowledgement and timing quality."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.explorer_storage import detail
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.rollup_repo import RollupRepository
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import BilledUsage
from htd.domain.billing.estimate import estimate_cost
from htd.domain.common.time import Instant, parse_instant
from htd.domain.pricing.model import ModelRate, PriceRevision, PriceVersion
from htd.domain.sources.zcode import ZcodeRow, normalize_row
from htd.domain.statistics.output_window import SCALE, OutputWindow
from htd.domain.usage.model import OUTPUT_TIMING_UNCERTAIN, Tokens, UsageEvent
from htd.ipc.dto.explorer import ExploreRequest, ExploreResponse, OutputReferencesResponse

from .collection_helpers import collect, usage
from .scan_helpers import seed_prices

AT = parse_instant("2026-10-21T10:00:00Z")


def setup(writer: WriteExecutor, at: Instant = AT) -> None:
    writer.execute(lambda w: seed_prices(w, at))

    def seed(w):
        repo = PricingRepository(w.connection)
        old = repo.load()
        rate = ModelRate("gpt-6-astra", "AsTra", "2", "0.2", "4", cache_write="3")
        repo.save(
            w,
            old,
            replace(
                old,
                versions=(*old.versions, PriceVersion("v2", "g", 2, "2x", at)),
                revisions=(*old.revisions, PriceRevision("r2", "v2", at, 99, (rate,))),
                revision=old.revision + 1,
            ),
        )
        wallet = WalletRepository(w.connection)
        wallet.create_vendor(w, vendor_id="vendor", name="Supplier", at=at, operation_id="create-vendor")
        wallet.link_group(w, "g", "vendor", at=at)

    writer.execute(seed)


def bill(writer: WriteExecutor, events: tuple[UsageEvent, ...], version: str = "v", at: Instant = AT) -> None:
    collect(writer, events, harness=events[0].harness, at=at)

    def run(w):
        rate = PricingRepository(w.connection).load().revisions[0 if version == "v" else 1].models[0]

        def quote(event):
            return BilledUsage(
                event,
                estimate_cost(event.tokens, rate, parse_instant(event.occurred_at)),
                "g",
                version,
                "r" if version == "v" else "r2",
                rate.model_id,
            )

        IngestRepository(w.connection).bill_batch(
            w,
            quote,
            at=at,
            on_billed=lambda work, priced: WalletRepository(w.connection).append_consumption(
                work, priced, at=at
            ),
        )

    writer.execute(run)


def request(**patch: Any) -> dict[str, Any]:
    return ExploreRequest(
        start="2026-10-21T00:00:00.000Z", end="2026-10-22T00:00:00.000Z", **patch
    ).model_dump()


def test_scope_conserves_costs_and_tokens_through_detail_retirement(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "test.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        first = usage(
            1,
            at=AT,
            model="gpt-6-astra",
            tokens=Tokens(input=1000, cache_read=700, output=100, cache_write=0),
        )
        second = replace(first, dedupe_key=f"{2:064x}", request_id="r2")
        bill(writer, (first,))
        bill(writer, (second,), "v2")
        store = SqliteStatisticsStore(pool, writer)
        before = ExploreResponse.model_validate(store.explore(request(), AT))
        one = ExploreResponse.model_validate(
            store.explore(request(vendorIds=["vendor"], versionIds=["v"]), AT)
        )
        two = ExploreResponse.model_validate(
            store.explore(request(vendorIds=["vendor"], versionIds=["v2"]), AT)
        )
        assert int(one.totals.picoUsd) + int(two.totals.picoUsd) == int(before.totals.picoUsd)
        assert before.totals.totalTokens == "2200" and one.totals.totalTokens == "1100"
        assert before.totals.cacheHitRate == 0.7
        assert one.totals.parts == {"input": "300", "cache": "700", "output": "100", "write": "0"}
        later = Instant(AT.value + timedelta(days=4))
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later, publish_daily=False))
        after = ExploreResponse.model_validate(store.explore(request(), later))
        assert after.totals == before.totals
        assert after.missingVersionRecords == "0"
        assert (
            ExploreResponse.model_validate(store.explore(request(versionIds=["v2"]), later)).totals
            == two.totals
        )
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later, publish_daily=False))
        assert ExploreResponse.model_validate(store.explore(request(), later)).totals == after.totals


def test_actual_cache_creation_cost_controls_per_dollar_partition(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "test.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        no_write = usage(
            1,
            at=AT,
            model="gpt-6-astra",
            tokens=Tokens(input=1000, cache_read=500, cache_write=0, output=100),
        )
        paid_write = replace(
            no_write,
            dedupe_key=f"{2:064x}",
            tokens=Tokens(input=1000, cache_read=500, cache_write=100, output=100),
        )
        bill(writer, (no_write,), "v2")
        row = pool.get().execute("SELECT * FROM ledger").fetchone()
        assert detail(pool.get(), dict(row))["eligible_write"] == 0
        bill(writer, (paid_write,), "v2")
        result = ExploreResponse.model_validate(SqliteStatisticsStore(pool).explore(request(), AT))
        assert result.totals.parts == {"input": "900", "cache": "1000", "output": "200", "write": "100"}
        assert sum(map(int, result.totals.parts.values())) == int(result.totals.eligibleTokens)


def test_acknowledgement_survives_reopen_and_new_missing_input_alerts(tmp_path: Path) -> None:
    path = tmp_path / "test.db"
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        setup(writer)
        missing = usage(1, at=AT, model="gpt-6-astra", tokens=Tokens(input=100, cache_read=None, output=10))
        bill(writer, (missing,))
        store = SqliteStatisticsStore(pool, writer)
        before = ExploreResponse.model_validate(store.explore(request(), AT))
        assert before.qualityUnacknowledged == 1
        store.acknowledge_quality(
            [{"id": before.quality[0].id, "fingerprint": before.quality[0].fingerprint}], AT
        )
        assert ExploreResponse.model_validate(store.explore(request(), AT)).qualityUnacknowledged == 0
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        store = SqliteStatisticsStore(pool, writer)
        old = ExploreResponse.model_validate(store.explore(request(), AT))
        assert old.qualityTotal == 1 and old.quality[0].acknowledged
        bill(writer, (replace(missing, dedupe_key=f"{2:064x}", request_id="second"),))
        fresh = ExploreResponse.model_validate(store.explore(request(), AT))
        assert fresh.qualityUnacknowledged == 1 and fresh.quality[0].recordCount == "2"
        assert int(fresh.totals.picoUsd) == int(old.totals.picoUsd) * 2


def test_monthly_compaction_keeps_exact_totals_and_boundary_notice(tmp_path: Path) -> None:
    old = parse_instant("2023-01-15T10:00:00Z")
    with WriteExecutor(tmp_path / "test.db") as writer, ReadPool(writer.path) as pool:
        setup(writer, old)
        bill(
            writer,
            (
                usage(1, at=old, model="gpt-6-astra"),
                usage(2, at=Instant(old.value + timedelta(days=6)), model="gpt-6-astra"),
            ),
            at=old,
        )
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=AT, publish_daily=False))
        result = ExploreResponse.model_validate(
            SqliteStatisticsStore(pool).explore(
                ExploreRequest(start="2023-01-15T00:00:00.000Z", end="2023-01-16T00:00:00.000Z").model_dump(),
                AT,
            )
        )
        assert result.totals.recordCount == "2"
        assert result.totals.totalTokens == "220"
        assert any("月份" in notice for notice in result.notices)
        assert pool.get().execute("SELECT grain FROM usage_dimensions").fetchone()[0] == "month"


def test_native_zcode_wait_never_becomes_inferred_output_but_usage_remains() -> None:
    timed = ZcodeRow(
        1,
        "first",
        "session",
        "r1",
        model_id="deepseek",
        status="completed",
        completed_at=1792584000000,
        first_token_at=1792583995000,
        raw_usage_json='{"inputTokens":100,"cacheReadTokens":0,"outputTokens":1000}',
    )
    waiting = replace(
        timed,
        rowid=2,
        id="second",
        logical_request_id="r2",
        completed_at=1792584045000,
        first_token_at=None,
        started_at=1792584001000,
        duration_ms=44000,
        raw_usage_json='{"inputTokens":100,"cacheReadTokens":0,"outputTokens":51}',
    )
    first, second = normalize_row(timed), normalize_row(waiting)
    assert isinstance(first, UsageEvent) and isinstance(second, UsageEvent)
    assert second.tokens.output == 51 and OUTPUT_TIMING_UNCERTAIN in second.issues
    old = normalize_row(replace(waiting, started_at=None, duration_ms=None))
    assert isinstance(old, UsageEvent) and OUTPUT_TIMING_UNCERTAIN not in old.issues
    window = OutputWindow()
    window.add(
        first.occurred_at,
        first.tokens.output,
        first.generation_milliseconds,
        first.session_id,
        first.occurred_at,
    )
    before = window.totals(True)
    window.add(second.occurred_at, None, None, second.session_id, second.occurred_at)
    assert window.totals(True) == before
    tokens, duration, _ = before
    assert tokens * 1000 / SCALE / duration == 200


def test_output_reference_is_separate_and_empty_today_is_not_replaced(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "test.db") as writer, ReadPool(writer.path) as pool:
        result = OutputReferencesResponse.model_validate(
            SqliteStatisticsStore(pool).output_references({"harnesses": ["codex"], "modelKeys": ["none"]}, AT)
        )
        assert result.today == "2026-10-21" and result.references == []


def test_late_earlier_session_start_moves_published_sample_without_double_count(tmp_path: Path) -> None:
    from htd.adapters.sqlite.output_speed import close_quiet, record

    with WriteExecutor(tmp_path / "test.db") as writer, ReadPool(writer.path) as pool:
        for stamp, duration, received in (
            ("2026-10-22T10:00:00.000Z", 6000, "2026-10-22T10:00:00.000Z"),
            ("2026-10-21T10:00:00.000Z", 12000, "2026-10-22T11:00:00.000Z"),
        ):
            at = parse_instant(received)
            writer.execute(
                lambda w, stamp=stamp, at=at, duration=duration: record(
                    w.connection,
                    harness="dsh",
                    model_key="m",
                    display_name="M",
                    output=600,
                    occurred_at=stamp,
                    at=at,
                    generation_milliseconds=duration,
                    session_ids=["same"],
                )
            )
            writer.execute(
                lambda w, at=at: close_quiet(w.connection, Instant(at.value + timedelta(minutes=11)))
            )
        rows = (
            pool.get()
            .execute("SELECT day,tps,samples FROM output_daily_speed WHERE harness='dsh'")
            .fetchall()
        )
        assert [tuple(row) for row in rows] == [("2026-10-21", "66.7", 1)]


def test_upgrade_rebuilds_zcode_speed_once_without_changing_ledger_or_wallet(tmp_path: Path) -> None:
    from htd.adapters.sqlite.session_speed import live
    from htd.adapters.sqlite.zcode_speed_repair import KEY, install, repair

    epoch = int(AT.value.timestamp() * 1000)
    native = tmp_path / "native.sqlite"
    values = []
    for index, end, first, output in ((1, epoch, epoch - 5000, 1000), (2, epoch + 45000, None, 51)):
        values.append(
            (
                str(index),
                "same",
                f"r{index}",
                0,
                "gpt-6-astra",
                "completed",
                end,
                json.dumps({"inputTokens": 100, "cacheReadTokens": 0, "outputTokens": output}),
                end - (5000 if first else 44000),
                first,
                5000 if first else 44000,
                None,
            )
        )
    with sqlite3.connect(native) as source:
        source.execute(
            "CREATE TABLE model_usage(id,session_id,logical_request_id,attempt_index,model_id,status,"
            "completed_at,raw_usage_json,started_at,first_token_at,duration_ms,time_to_first_token_ms)"
        )
        source.executemany("INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values)
    with WriteExecutor(tmp_path / "test.db") as writer, ReadPool(writer.path) as pool:
        setup(writer)
        events = []
        for index, row in enumerate(values, 1):
            event = normalize_row(
                ZcodeRow(
                    index,
                    *row[:8],
                    started_at=row[8],
                    first_token_at=row[9],
                    duration_ms=row[10],
                    time_to_first_token_ms=row[11],
                )
            )
            assert isinstance(event, UsageEvent)
            events.append(replace(event, issues=()))  # Simulate released fallback timing.
        bill(writer, tuple(events))
        before = [(r["pico"], r["input"], r["output"]) for r in pool.get().execute("SELECT * FROM ledger")]
        wallet = [tuple(r) for r in pool.get().execute("SELECT amount_pico,kind FROM wallet_entry")]
        later = Instant(AT.value + timedelta(minutes=2))

        def upgrade(work):
            work.connection.execute("UPDATE source SET path=? WHERE harness='zcode'", (str(native),))
            work.connection.execute("DELETE FROM runtime_state WHERE key=?", (KEY,))
            install(work.connection, later)

        writer.execute(upgrade)
        assert repair(pool, writer, later) == 2
        assert live(pool.get(), "zcode", later)[0]["sessionAverageTps"] == "200.0"
        assert repair(pool, writer, later) == 0
        assert [
            (r["pico"], r["input"], r["output"]) for r in pool.get().execute("SELECT * FROM ledger")
        ] == before
        assert [tuple(r) for r in pool.get().execute("SELECT amount_pico,kind FROM wallet_entry")] == wallet
