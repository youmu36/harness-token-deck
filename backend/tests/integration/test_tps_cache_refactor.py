"""Recent speed excludes idle reports; confirmed compaction remains visible."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.output_speed import begin_run, close_quiet, live, record
from htd.adapters.sqlite.writer import WriteExecutor
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.sources.codex import CodexContext, parse_record
from htd.domain.usage.model import OUTPUT_TIMING_UNCERTAIN, Tokens, UsageEvent

from .collection_helpers import AT, collect, quote, usage


def stamp(seconds: int) -> str:
    return format_instant(Instant(AT.value + timedelta(seconds=seconds)))


def test_codex_uses_completed_output_before_delayed_usage_and_preserves_checkpoint() -> None:
    ctx = CodexContext(
        file_session_id="s",
        raw_file_session_id="s",
        saw_session_meta=True,
        originator_ok=True,
        kind="desktop",
        turn_id="t",
        model_id="M",
    )
    parse_record({"type": "event_msg", "timestamp": stamp(0), "payload": {"type": "task_started"}}, ctx)
    parse_record(
        {
            "type": "response_item",
            "timestamp": stamp(20),
            "payload": {"type": "message", "role": "assistant", "content": "PRIVATE"},
        },
        ctx,
    )
    assert "PRIVATE" not in repr(ctx)
    ctx = CodexContext.from_dict(ctx.to_dict())
    parse_record({"type": "event_msg", "timestamp": stamp(21), "payload": {"type": "task_complete"}}, ctx)
    request = {
        "type": "token_usage_record",
        "timestamp": stamp(240),
        "payload": {
            "thread_id": "s",
            "turn_id": "t",
            "response_id": "r",
            "usage": {"output_tokens": 1200},
        },
    }
    event = parse_record(request, ctx)
    assert isinstance(event, UsageEvent) and event.generation_milliseconds == 20_000
    replay = parse_record(request, CodexContext.from_dict(ctx.to_dict()))
    assert isinstance(replay, UsageEvent) and replay.generation_milliseconds == 20_000


def test_codex_tool_work_is_outside_the_next_request_and_invalid_end_does_not_shorten_it() -> None:
    ctx = CodexContext(
        file_session_id="s",
        raw_file_session_id="s",
        saw_session_meta=True,
        originator_ok=True,
        kind="desktop",
        turn_id="t",
        model_id="M",
    )
    parse_record({"type": "event_msg", "timestamp": stamp(0), "payload": {"type": "task_started"}}, ctx)
    parse_record(
        {"type": "response_item", "timestamp": stamp(180), "payload": {"type": "function_call_output"}}, ctx
    )
    parse_record(
        {
            "type": "response_item",
            "timestamp": stamp(300),
            "payload": {"type": "message", "role": "assistant"},
        },
        ctx,
    )
    event = parse_record(
        {
            "type": "token_usage_record",
            "timestamp": stamp(200),
            "payload": {
                "thread_id": "s",
                "turn_id": "t",
                "response_id": "r",
                "usage": {"output_tokens": 1200},
            },
        },
        ctx,
    )
    assert isinstance(event, UsageEvent) and event.generation_milliseconds == 20_000


def test_positive_sample_window_smoothing_idle_gaps_freeze_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        writer.execute(
            lambda w: record(
                w.connection,
                harness="codex",
                model_key="m",
                display_name="M",
                output=None,
                occurred_at=stamp(60),
                at=AT,
                samples=[(stamp(0), 600, 60_000), (stamp(60), 600, 10_000)],
            )
        )
        initial = live(readers.get(), "codex", AT)[0]
        assert initial["tokensPerSecond"] == "17.1"
        for minute in (2, 3, 4):
            writer.execute(
                lambda w, minute=minute: record(
                    w.connection,
                    harness="codex",
                    model_key="m",
                    display_name="M",
                    output=0,
                    occurred_at=stamp(minute * 60),
                    at=AT,
                )
            )
        assert (
            live(readers.get(), "codex", Instant(AT.value + timedelta(minutes=4)))[0]["tokensPerSecond"]
            == "17.1"
        )
        writer.execute(lambda w: begin_run(w.connection))
        frozen = live(readers.get(), "codex", AT)[0]
        assert frozen["frozen"] and frozen["tokensPerSecond"] == "17.1"
        raw = readers.get().execute("SELECT buckets_json,timing_json FROM output_live").fetchone()
        assert json.loads(raw[0]) == {} and json.loads(raw[1]) == {"rate": "17.1", "timed": True}
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        later = Instant(AT.value + timedelta(minutes=11))
        writer.execute(lambda w: close_quiet(w.connection, later))
        assert live(readers.get(), "codex", later)[0]["tokensPerSecond"] == "17.1"
        assert readers.get().execute("SELECT timed_output,decode_ms FROM output_reference").fetchone()[:] == (
            "1200",
            "70000",
        )


def test_speed_window_limits_old_samples_and_reports_fallback_basis_correctly(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        samples: list[tuple[str, int | None, int | None]] = [(stamp(0), 600, 60_000)]
        samples.extend((stamp(i * 60), 600, 10_000) for i in range(1, 11))
        writer.execute(
            lambda w: record(
                w.connection,
                harness="dsh",
                model_key="m",
                display_name="M",
                output=None,
                occurred_at=stamp(600),
                at=AT,
                samples=samples,
            )
        )
        assert live(readers.get(), "dsh", AT)[0]["tokensPerSecond"] == "60.0"
        writer.execute(
            lambda w: record(
                w.connection,
                harness="dsh",
                model_key="m",
                display_name="M",
                output=None,
                occurred_at=stamp(1200),
                at=AT,
                samples=[(stamp(i * 60), 600, None) for i in range(11, 21)],
            )
        )
        fallback = live(readers.get(), "dsh", AT)[0]
        assert fallback["tokensPerSecond"] == "10.0" and fallback["basis"] == "minute_estimate"


def test_billed_delayed_output_uses_the_same_session_gap_without_ignoring_new_untimed_tokens(
    tmp_path: Path,
) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        first = replace(usage(1, at=AT, tokens=Tokens(output=600)), generation_milliseconds=60_000)
        collect(writer, (first,), at=AT)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
        assert live(readers.get(), "codex", AT)[0]["tokensPerSecond"] == "10.0"
        later = Instant(AT.value + timedelta(minutes=2))
        second = usage(2, at=later, tokens=Tokens(output=7200))
        assert first.session_id is not None and first.session_id == second.session_id
        collect(writer, (second,), at=later)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=later))
        current = live(readers.get(), "codex", later)[0]
        assert current["tokensPerSecond"] == "65.0" and current["basis"] == "minute_estimate"
        assert current["outputTokens"] == "7800"
        metadata = json.loads(readers.get().execute("SELECT timing_json FROM output_live").fetchone()[0])[
            "!window"
        ]
        assert len(metadata["minutes"]) == 2 and len(metadata["cursors"]) == 1
        assert first.session_id not in json.dumps(metadata)
        # A replay cannot allocate the completed interval a second time.
        collect(writer, (second,), at=later)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=later))
        assert live(readers.get(), "codex", later)[0]["tokensPerSecond"] == "65.0"


def test_an_existing_minute_window_upgrades_without_losing_its_previous_output(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        writer.execute(
            lambda w: record(
                w.connection,
                harness="codex",
                model_key="m",
                display_name="M",
                output=600,
                occurred_at=stamp(0),
                at=AT,
                generation_milliseconds=60_000,
            )
        )

        # Recreate the released JSON shape with no session-window metadata.
        writer.execute(
            lambda w: w.connection.execute(
                "UPDATE output_live SET timing_json=?", (json.dumps({stamp(0): ["600", "60000"]}),)
            )
        )
        writer.execute(
            lambda w: record(
                w.connection,
                harness="codex",
                model_key="m",
                display_name="M",
                output=1200,
                occurred_at=stamp(60),
                at=AT,
            )
        )
        assert live(readers.get(), "codex", AT)[0]["outputTokens"] == "1800"
        writer.execute(lambda w: close_quiet(w.connection, Instant(AT.value + timedelta(minutes=11))))
        assert readers.get().execute("SELECT output_tokens FROM output_reference").fetchone()[0] == "1800"


def test_zero_output_after_freezing_keeps_the_last_value_instead_of_clearing_it(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        writer.execute(
            lambda w: record(
                w.connection,
                harness="codex",
                model_key="m",
                display_name="M",
                output=600,
                occurred_at=stamp(0),
                at=AT,
                generation_milliseconds=10_000,
            )
        )
        later = Instant(AT.value + timedelta(minutes=11))
        writer.execute(lambda w: close_quiet(w.connection, later))
        writer.execute(
            lambda w: record(
                w.connection,
                harness="codex",
                model_key="m",
                display_name="M",
                output=0,
                occurred_at=stamp(660),
                at=later,
            )
        )
        current = live(readers.get(), "codex", later)[0]
        assert current["tokensPerSecond"] == "60.0" and current["frozen"]


def test_interaction_exclusion_survives_inbox_restart_without_losing_billing(tmp_path: Path) -> None:
    ctx = CodexContext(
        file_session_id="s",
        raw_file_session_id="s",
        saw_session_meta=True,
        originator_ok=True,
        kind="desktop",
    )

    def turn(second: int, name: str) -> None:
        parse_record(
            {"type": "turn_context", "timestamp": stamp(second), "payload": {"turn_id": name, "model": "M"}},
            ctx,
        )

    def request(second: int, name: str, count: int) -> UsageEvent:
        event = parse_record(
            {
                "type": "token_usage_record",
                "timestamp": stamp(second),
                "payload": {"turn_id": name, "response_id": str(second), "usage": {"output_tokens": count}},
            },
            ctx,
        )
        assert isinstance(event, UsageEvent)
        return event

    path = tmp_path / "timing.db"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        turn(0, "first")
        first = request(20, "first", 1200)
        collect(writer, (first,), at=parse_instant(stamp(20)))
        writer.execute(
            lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=parse_instant(stamp(20)))
        )
        assert live(readers.get(), "codex", AT)[0]["tokensPerSecond"] == "60.0"
        turn(100, "second")
        parse_record({"type": "event_msg", "timestamp": stamp(110), "payload": {"type": "user_message"}}, ctx)
        ctx = CodexContext.from_dict(ctx.to_dict())
        second = request(120, "second", 1200)
        assert OUTPUT_TIMING_UNCERTAIN in second.issues
        collect(writer, (second,), at=parse_instant(stamp(120)))
    # Pending events retain the timing flag through durable JSON encoding.
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        writer.execute(
            lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=parse_instant(stamp(120)))
        )
        current = live(readers.get(), "codex", AT)[0]
        assert current["tokensPerSecond"] == "60.0" and current["outputTokens"] == "1200"
        window = json.loads(readers.get().execute("SELECT timing_json FROM output_live").fetchone()[0])[
            "!window"
        ]
        assert window["cursors"] == {}  # No inference can bridge the ambiguous sample.
        parse_record(
            {"type": "response_item", "timestamp": stamp(200), "payload": {"type": "function_call_output"}},
            ctx,
        )
        third = request(220, "second", 600)
        collect(writer, (third, second), at=parse_instant(stamp(220)))  # Replay remains idempotent.
        writer.execute(
            lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=parse_instant(stamp(220)))
        )
        current = live(readers.get(), "codex", AT)[0]
        assert current["tokensPerSecond"] == "45.0" and current["outputTokens"] == "1800"
        rows = readers.get().execute("SELECT output,pico FROM ledger").fetchall()
        assert len(rows) == 3 and sum(row[0] for row in rows) == 3000
        assert sum(int(row[1]) for row in rows) == 3 * 84_000_000
        writer.execute(lambda w: close_quiet(w.connection, parse_instant(stamp(900))))
        row = readers.get().execute("SELECT timed_output,decode_ms FROM output_reference").fetchone()
        assert tuple(row) == ("1800", "40000")
