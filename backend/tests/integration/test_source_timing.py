"""Native optional timing reaches the counter; buffered bursts never change billing."""

import sqlite3
from contextlib import closing
from pathlib import Path

from htd.adapters.collectors.zcode_reader import ZcodeReader
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.output_speed import live, record
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import CheckpointRecord
from htd.domain.common.time import parse_instant

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/zcode"
AT = parse_instant("2026-10-21T00:01:00Z")


def test_zcode_reads_native_timing_columns_without_requiring_them_in_older_schemas(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.executescript((FIXTURES / "schema.sql").read_text())
        for name in ("started_at", "first_token_at", "duration_ms", "time_to_first_token_ms"):
            db.execute(f"ALTER TABLE model_usage ADD COLUMN {name} INTEGER")
        db.execute(
            "INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "row",
                "s",
                "r",
                0,
                "model",
                "completed",
                1792540860000,
                '{"outputTokens":1400,"reasoningTokens":800}',
                1792540800000,
                1792540820000,
                60_000,
                20_000,
            ),
        )
        db.commit()
    result = ZcodeReader().read(str(source), CheckpointRecord(), parse_instant("2026-10-21T00:00:00Z"))
    assert len(result.events) == 1 and not result.diagnostics
    event = result.events[0]
    assert event.generation_milliseconds == 40_000
    with WriteExecutor(tmp_path / "data.sqlite3") as writer, ReadPool(writer.path) as readers:
        writer.execute(
            lambda w: record(
                w.connection,
                harness="zcode",
                model_key="model",
                display_name="Model",
                output=event.tokens.output,
                occurred_at=event.occurred_at,
                at=AT,
                generation_milliseconds=event.generation_milliseconds,
            )
        )
        speed = live(readers.get(), "zcode", AT)[0]
        assert speed["tokensPerSecond"] == "35.0" and speed["basis"] == "generation"


def test_buffered_dsh_bursts_never_create_or_poison_persistent_speed(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer, ReadPool(writer.path) as readers:
        for output, duration in ((93, 3), (600, 10_000), (365, 7)):
            writer.execute(
                lambda w, count=output, ms=duration: record(
                    w.connection,
                    harness="dsh",
                    model_key="model",
                    display_name="Model",
                    output=count,
                    occurred_at="2026-10-21T00:01:00.000Z",
                    at=AT,
                    generation_milliseconds=ms,
                )
            )
            speeds = live(readers.get(), "dsh", AT)
            if output == 93:
                assert not speeds
            else:
                assert speeds[0]["tokensPerSecond"] == "60.0"
                assert speeds[0]["outputTokens"] == "600"
