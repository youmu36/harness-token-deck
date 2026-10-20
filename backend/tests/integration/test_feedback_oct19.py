"""Late compaction evidence and DSH timing/cache recovery without changing billed costs."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from htu.adapters.collectors.codex_reader import CodexReader
from htu.adapters.sqlite.account_book import AccountBook
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.ingest_repo import IngestRepository
from htu.adapters.sqlite.output_speed import live, references
from htu.adapters.sqlite.remote_store import SqliteRemoteStore
from htu.adapters.sqlite.rollup_repo import RollupRepository
from htu.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htu.adapters.sqlite.writer import WriteExecutor
from htu.application.ports import CheckpointRecord
from htu.application.statistics import Statistics
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, format_instant
from htu.domain.sources.dsh import DshContext, first_token_time, parse_record
from htu.domain.statistics.window import Window
from htu.domain.usage.model import Harness, Tokens, UsageEvent
from htu.ipc.dto.wallet import AccountEntriesResponse

from .collection_helpers import AT, collect, ensure_source, quote, usage
from .test_account_book import observe, setup
from .test_remote_wallet import balance
from .test_wallet_storage import USD


def test_late_compaction_retracts_a_cached_warning_after_billing_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "source.jsonl"
    request: dict[str, Any] = {
        "type": "token_usage_record",
        "timestamp": format_instant(AT),
        "payload": {
            "response_id": "compaction-request",
            "turn_id": "t",
            "usage": {
                "input_tokens": 347604,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 3614,
                "reasoning_output_tokens": 1034,
            },
        },
    }
    path.write_text(
        "\n".join(
            map(
                json.dumps,
                [
                    {
                        "type": "session_meta",
                        "payload": {"id": "synthetic", "originator": "Codex Desktop", "source": "vscode"},
                    },
                    {"type": "turn_context", "payload": {"turn_id": "t", "model": "model-a"}},
                    request,
                ],
            )
        )
        + "\n",
        encoding="utf-8",
    )
    reader = CodexReader()
    first = reader.read(str(path), CheckpointRecord(), AT)
    dbpath = tmp_path / "ledger.db"
    with WriteExecutor(dbpath) as writer:
        collect(writer, first.events)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
    # An unrelated request may have arrived before the marker. Matching is by
    # response identity, independent of the last request and the batch boundary.
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    **request,
                    "payload": {
                        **request["payload"],
                        "response_id": "later",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 50,
                            "output_tokens": 10,
                        },
                    },
                }
            )
            + "\n"
        )
        stream.write(
            json.dumps(
                {
                    "type": "compacted",
                    "timestamp": format_instant(AT),
                    "payload": {
                        "compaction_response_id": "compaction-request",
                        "latest_token_usage_record": {"thread_id": "synthetic"},
                        "message": "PRIVATE COMPACTION CONTENT",
                    },
                }
            )
            + "\n"
        )
    late = reader.read(str(path), CheckpointRecord(first.checkpoint), AT)
    assert len(late.compaction_keys) == 1 and len(late.events) == 1
    assert "PRIVATE" not in repr(late)
    with WriteExecutor(dbpath) as writer, ReadPool(dbpath) as readers:
        statistics = Statistics(SqliteStatisticsStore(readers), FakeClock(AT))
        window = Window(AT, Instant(AT.value + timedelta(minutes=1)), 0, "minute")
        before, _ = statistics.summary(Harness.CODEX, window)
        assert before.trend[0].cache_bust
        original = dict(readers.get().execute("SELECT * FROM ledger").fetchone())
        root = ensure_source(writer)

        def apply() -> None:
            writer.execute(
                lambda w: IngestRepository(w.connection).commit_batch(
                    w,
                    Harness.CODEX,
                    root,
                    str(path),
                    late.checkpoint,
                    late.events,
                    at=AT,
                    compaction_keys=late.compaction_keys,
                )
            )

        apply()
        after, revision = statistics.summary(Harness.CODEX, window)
        assert not after.trend[0].cache_bust and not after.trend[0].cache_loss
        assert after.trend[0].context_compacted
        assert dict(readers.get().execute("SELECT * FROM ledger").fetchone()) == original
        assert readers.get().execute("SELECT COUNT(*) FROM usage_annotation").fetchone()[0] == 1
        apply()
        assert statistics.summary(Harness.CODEX, window)[1] == revision
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
        assert readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2
        assert (
            readers.get()
            .execute("SELECT COUNT(*) FROM diagnostic_log WHERE code='identity_conflict'")
            .fetchone()[0]
            == 0
        )


def test_dsh_missing_cache_is_recovered_from_exact_total_and_existing_cost_is_preserved(
    tmp_path: Path,
) -> None:
    context = DshContext(session_id="s", raw_session_id="s", model_id="model-a")
    event = parse_record(
        {
            "type": "assistant/message",
            "time": int(AT.value.timestamp() * 1000),
            "data": {
                "turn": 1,
                "step": 1,
                "usage": {"inputTokens": 1234, "outputTokens": 60, "totalTokens": 1294},
            },
        },
        context,
    )
    assert isinstance(event, UsageEvent) and event.tokens == Tokens(1234, 0, 0, 60, None)
    old = replace(event, tokens=Tokens(1234, None, 0, 60, None))
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, (old,), harness=Harness.DSH)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
        original = dict(readers.get().execute("SELECT * FROM ledger").fetchone())
        collect(writer, (event,), harness=Harness.DSH)
        statistics = Statistics(SqliteStatisticsStore(readers), FakeClock(AT))
        result, revision = statistics.summary(
            Harness.DSH, Window(AT, Instant(AT.value + timedelta(minutes=1)), 0)
        )
        assert result.metrics.new_input == 1234 and not result.metrics.cache_coverage_incomplete
        assert dict(readers.get().execute("SELECT * FROM ledger").fetchone()) == original
        collect(writer, (event,), harness=Harness.DSH)
        assert (
            statistics.summary(Harness.DSH, Window(AT, Instant(AT.value + timedelta(minutes=1)), 0))[1]
            == revision
        )
        assert (
            readers.get()
            .execute("SELECT COUNT(*) FROM diagnostic_log WHERE code='identity_conflict'")
            .fetchone()[0]
            == 0
        )

        later = Instant(AT.value + timedelta(days=4))
        writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later))
        assert readers.get().execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
        collect(writer, (event,), harness=Harness.DSH, at=later)
        assert (
            readers.get()
            .execute("SELECT COUNT(*) FROM diagnostic_log WHERE code='identity_conflict'")
            .fetchone()[0]
            == 0
        )


def test_packed_timing_skips_empty_fragments_and_counts_named_tool_deltas() -> None:
    assert (
        first_token_time([{"type": "text-chunks", "time0": 1000, "texts": ["", "hello"], "dt": [250]}])
        == 1250
    )
    assert (
        first_token_time(
            [{"type": "tool-call-chunks", "time0": 1000, "name": "tool", "args": [""], "dt": []}]
        )
        == 1000
    )
    assert first_token_time([{"type": "text-chunks", "time0": 1000, "texts": [""], "dt": []}]) is None


def test_account_entry_exposes_actual_interval_separately_from_posting_time(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, initial="10", used=0)
        observe(store, 9 * USD, 5, USD)
        page = AccountEntriesResponse.model_validate(AccountBook(readers.get()).entries("vendor", None, 50))
        entry = next(row for row in page.entries if row.kind == "consumption")
        assert entry.fromAt == format_instant(balance(0, 0).fetched_at)
        assert entry.toAt == format_instant(balance(0, 5).fetched_at)
        assert entry.fromAt != entry.toAt and entry.postedAt is not None
        assert str(USD) == entry.C


def test_dsh_tps_uses_weighted_decode_time_and_survives_quiet_restart(tmp_path: Path) -> None:
    events = tuple(
        replace(usage(i, harness=Harness.DSH, tokens=Tokens(output=count)), generation_milliseconds=ms)
        for i, count, ms in [(1, 600, 10_000), (2, 1200, 20_000)]
    )
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        collect(writer, events, harness=Harness.DSH)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
        current = live(readers.get(), "dsh", AT)[0]
        assert current["tokensPerSecond"] == "60.0" and current["basis"] == "generation"
    later = Instant(AT.value + timedelta(minutes=11))
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        store = SqliteRemoteStore(writer, readers)
        store.begin_run(later)
        store.flush_pending(later)
        store.flush_pending(later)
        assert live(readers.get(), "dsh", later)[0]["frozen"]
        daily = references(readers.get(), "dsh", format_instant(AT), format_instant(later), "model")
        assert len(daily) == 1 and daily[0]["tokensPerSecond"] == "60.0" and daily[0]["samples"] == 1
