"""Session rates survive rollovers/restarts and publish one replaceable daily sample."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.output_speed import begin_run, close_quiet, live, ranking, record, references
from htd.adapters.sqlite.remote_store import SqliteRemoteStore
from htd.adapters.sqlite.statistics_store import SqliteStatisticsStore
from htd.adapters.sqlite.writer import WriteExecutor
from htd.domain.common.time import parse_instant
from htd.domain.usage.model import OUTPUT_TIMING_UNCERTAIN, Tokens

from .collection_helpers import collect, quote, usage


def test_sessions_and_models_are_independent_and_average_is_not_the_recent_rate(tmp_path: Path) -> None:
    at = parse_instant("2026-10-20T10:00:00Z")
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:

        def add(session: str, model: str, stamp: str, output: int, duration: int) -> None:
            writer.execute(
                lambda w: record(
                    w.connection,
                    harness="codex",
                    model_key=model,
                    display_name=model,
                    output=None,
                    occurred_at=stamp,
                    at=parse_instant(stamp),
                    samples=[(stamp, output, duration)],
                    session_ids=[session],
                )
            )

        add("a", "m", "2026-10-20T10:00:00.000Z", 600, 60_000)
        for minute in (1, 2, 3):
            add("a", "m", f"2026-10-20T10:0{minute}:00.000Z", 600, 10_000)
        add("b", "m", "2026-10-20T10:04:00.000Z", 600, 20_000)
        add("a", "other", "2026-10-20T10:05:00.000Z", 400, 10_000)
        values = live(readers.get(), "codex", at)
        assert len(values) == 3
        a = next(row for row in values if row["outputTokens"] == "2400")
        assert a["tokensPerSecond"] == "60.0" and a["sessionAverageTps"] == "26.7"
        assert len({row["sessionKey"] for row in values}) == 2
        assert {row["sessionAverageTps"] for row in values} == {"26.7", "30.0", "40.0"}
        # An hour of silence cannot discard the older effective output minutes.
        add("a", "m", "2026-10-20T12:00:00.000Z", 600, 10_000)
        current = next(row for row in live(readers.get(), "codex", at) if row["outputTokens"] == "3000")
        assert current["tokensPerSecond"] == "60.0" and current["sessionAverageTps"] == "30.0"
        for row in readers.get().execute("SELECT window_json FROM output_session"):
            assert len(json.loads(row[0])["minutes"]) <= 3


def test_same_session_resumes_after_restart_without_duplicate_samples_or_replay(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    at = parse_instant("2026-10-20T10:00:00Z")
    first = replace(usage(1, at=at, tokens=Tokens(output=600)), generation_milliseconds=60_000)
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        collect(writer, (first,), at=at)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=at))
        writer.execute(lambda w: close_quiet(w.connection, parse_instant("2026-10-20T10:11:00Z")))
        assert tuple(readers.get().execute("SELECT tps,samples FROM output_daily_speed").fetchone()) == (
            "10.0",
            1,
        )
        writer.execute(lambda w: begin_run(w.connection))
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        later = parse_instant("2026-10-20T12:00:00Z")
        second = replace(usage(2, at=later, tokens=Tokens(output=1200)), generation_milliseconds=20_000)
        collect(writer, (first, second), at=later)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=later))
        current = live(readers.get(), "codex", later)[0]
        assert current["outputTokens"] == "1800" and current["sessionAverageTps"] == "22.5"
        writer.execute(lambda w: close_quiet(w.connection, parse_instant("2026-10-20T12:11:00Z")))
        assert tuple(readers.get().execute("SELECT tps,samples FROM output_daily_speed").fetchone()) == (
            "22.5",
            1,
        )
        assert ranking(readers.get(), "codex", ("model",)) == {"model": "22.5"}
        assert readers.get().execute("SELECT COUNT(*) FROM output_reference").fetchone()[0] == 0


def test_daily_reference_averages_sessions_and_replaces_their_cumulative_value(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        at = parse_instant("2026-10-20T10:00:00Z")
        writer.execute(
            lambda w: record(
                w.connection,
                harness="codex",
                model_key="m",
                display_name="M",
                output=None,
                occurred_at="2026-10-20T10:00:00.000Z",
                at=at,
                samples=[
                    ("2026-10-20T10:00:00.000Z", 600, 60_000),
                    ("2026-10-20T10:00:00.000Z", 6000, 60_000),
                ],
                session_ids=["one", "two"],
            )
        )
        writer.execute(lambda w: close_quiet(w.connection, parse_instant("2026-10-20T10:11:00Z")))
        assert tuple(readers.get().execute("SELECT tps,samples FROM output_daily_speed").fetchone()) == (
            "55.0",
            2,
        )
        # Midnight and repeated flushes must not register another observation.
        tomorrow = parse_instant("2026-10-21T10:11:00Z")
        writer.execute(lambda w: close_quiet(w.connection, tomorrow))
        writer.execute(lambda w: close_quiet(w.connection, tomorrow))
        result = references(
            readers.get(), "codex", "2026-10-01T00:00:00.000Z", "2026-10-21T23:00:00.000Z", "m"
        )
        assert len(result) == 1 and result[0]["tokensPerSecond"] == "55.0" and result[0]["samples"] == 2


def test_long_generation_and_cross_day_session_keep_complete_lifetime_totals(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        for stamp, count, duration in (
            ("2026-10-20T10:00:00.000Z", 144000, 7_200_000),
            ("2026-10-21T10:00:00.000Z", 1200, 20_000),
        ):
            writer.execute(
                lambda w, stamp=stamp, count=count, duration=duration: record(
                    w.connection,
                    harness="dsh",
                    model_key="m",
                    display_name="M",
                    output=None,
                    occurred_at=stamp,
                    at=parse_instant(stamp),
                    samples=[(stamp, count, duration)],
                    session_ids=["s"],
                )
            )
        current = live(readers.get(), "dsh", parse_instant("2026-10-21T10:00:00Z"))[0]
        assert current["outputTokens"] == "145200" and current["sessionAverageTps"] == "20.1"
        writer.execute(lambda w: close_quiet(w.connection, parse_instant("2026-10-21T10:11:00Z")))
        assert [
            tuple(row)
            for row in readers.get().execute("SELECT tps,samples FROM output_daily_speed ORDER BY day")
        ] == [("20.1", 1)]


def test_unreliable_zero_and_unknown_outputs_preserve_speed_but_not_inference(tmp_path: Path) -> None:
    at = parse_instant("2026-10-20T10:00:00Z")
    events = (
        replace(usage(1, at=at, tokens=Tokens(output=600)), generation_milliseconds=10_000),
        replace(usage(2, at=at, tokens=Tokens(output=600)), issues=(OUTPUT_TIMING_UNCERTAIN,)),
        usage(3, at=at, tokens=Tokens(output=0)),
    )
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, events, at=at)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=at))
        current = live(readers.get(), "codex", at)[0]
        assert current["outputTokens"] == "600" and current["sessionAverageTps"] == "60.0"
        row = readers.get().execute("SELECT window_json,session_key FROM output_session").fetchone()
        assert not json.loads(row[0])["cursors"]
        assert row[1] == hashlib.sha256(b"session").hexdigest()[:16]
        assert "session" not in row[0]


def test_reupgrade_keeps_daily_samples_added_by_the_released_binary(tmp_path: Path) -> None:
    at = parse_instant("2026-10-20T10:00:00Z")
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:

        def add(session: str, elapsed: int) -> None:
            writer.execute(
                lambda w: record(
                    w.connection,
                    harness="codex",
                    model_key="m",
                    display_name="M",
                    output=None,
                    occurred_at="2026-10-20T10:00:00.000Z",
                    at=at,
                    samples=[("2026-10-20T10:00:00.000Z", 600, elapsed)],
                    session_ids=[session],
                )
            )
            writer.execute(lambda w: close_quiet(w.connection, parse_instant("2026-10-20T10:11:00Z")))

        add("a", 60_000)
        writer.execute(lambda w: w.connection.execute("UPDATE output_daily_speed SET tps='20.0',samples=2"))
        writer.execute(lambda w: w.connection.execute("UPDATE output_rank_speed SET total_tps='20.0'"))
        add("b", 20_000)
        assert tuple(readers.get().execute("SELECT tps,samples FROM output_daily_speed").fetchone()) == (
            "23.3",
            3,
        )
        assert ranking(readers.get(), "codex", ("m",)) == {"m": "23.3"}


def test_monitor_reuses_the_session_inventory_activity_and_existing_quiet_job(tmp_path: Path) -> None:
    at = parse_instant("2026-10-20T10:00:00Z")
    later = parse_instant("2026-10-20T10:09:00Z")
    events = (
        replace(usage(1, at=at, session="a", tokens=Tokens(output=600)), generation_milliseconds=10_000),
        replace(usage(2, at=at, session="b", tokens=Tokens(output=600)), generation_milliseconds=20_000),
    )
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        collect(writer, events, at=at)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=at))
        collect(writer, (usage(3, at=later, session="a", model="other", tokens=Tokens(output=0)),), at=later)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=later))
        now = parse_instant("2026-10-20T10:11:00Z")
        statements: list[str] = []
        with readers.connection() as traced:
            traced.set_trace_callback(statements.append)
        try:
            monitor = SqliteStatisticsStore(readers).monitor_data("codex", now)
        finally:
            with readers.connection() as traced:
                traced.set_trace_callback(None)
        assert len([sql for sql in statements if "FROM session_directory WHERE harness=" in sql]) == 1
        assert len(monitor["sessionHitRates"]) == 2
        assert len(monitor["outputSpeeds"]) == 2
        identity = next(
            row["sessionKey"]
            for row in monitor["sessionHitRates"]
            if row["sessionRef"] == monitor["selectedSessionRef"]
        )
        assert all(row["sessionKey"] == identity and not row["frozen"] for row in monitor["outputSpeeds"])
        # A zero-output request in another model keeps the same session active.
        store = SqliteRemoteStore(writer, readers)
        store.flush_pending(now)
        assert tuple(readers.get().execute("SELECT tps,samples FROM output_daily_speed").fetchone()) == (
            "30.0",
            1,
        )
        store.flush_pending(parse_instant("2026-10-20T10:20:00Z"))
        assert tuple(readers.get().execute("SELECT tps,samples FROM output_daily_speed").fetchone()) == (
            "45.0",
            2,
        )
