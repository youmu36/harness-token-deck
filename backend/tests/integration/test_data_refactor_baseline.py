"""Independent Token/money vectors: interruption changes timing, never reliable usage."""

import json
from pathlib import Path

import pytest
from htd.adapters.collectors.codex_reader import CodexReader
from htd.application.ports import CheckpointRecord

from .scan_helpers import AT, headers, request


@pytest.mark.parametrize("lifecycle", [None, "task_complete", "turn_aborted"])
def test_reliable_request_survives_completion_abort_absence_and_replay(
    tmp_path: Path, lifecycle: str | None
) -> None:
    path = tmp_path / "session.jsonl"
    status = (
        b""
        if lifecycle is None
        else (
            json.dumps(
                {
                    "timestamp": "2026-09-01T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": lifecycle, "turn_id": "turn-1"},
                }
            )
            + "\n"
        ).encode()
    )
    record = json.loads(request(1, AT))
    record["payload"]["usage"] = {
        "input_tokens": 1000,
        "cached_input_tokens": 200,
        "cache_write_input_tokens": 0,
        "output_tokens": 500,
        "reasoning_output_tokens": 100,
    }
    path.write_bytes(headers() + status + (json.dumps(record) + "\n").encode())
    batch = CodexReader().read(str(path), CheckpointRecord(), AT)
    assert len(batch.events) == 1
    event = batch.events[0]
    # Source fixture: total input includes cached input, output includes reasoning.
    assert event.tokens.input == 1000 and event.tokens.cache_read == 200
    assert event.tokens.output == 500 and event.tokens.reasoning == 100
    replay = CodexReader().read(str(path), CheckpointRecord(batch.checkpoint), AT)
    assert replay.events == ()
