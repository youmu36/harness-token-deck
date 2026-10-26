"""Request batches and completed write tasks must not retain unbounded source objects."""

import gc
import json
import weakref
from pathlib import Path
from threading import Event

import pytest
from htd.adapters.collectors.claude_reader import ClaudeReader
from htd.adapters.collectors.codex_reader import CodexReader
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.application.ports import CheckpointRecord
from htd.domain.common.testing import FakeClock

from .scan_helpers import AT, count, headers, request, scanner, seed_prices, sessions


@pytest.mark.parametrize("reader_type", [CodexReader, ClaudeReader])
def test_small_request_bursts_are_bounded_and_resume_after_restart(
    tmp_path: Path, reader_type: type[CodexReader]
) -> None:
    path = tmp_path / "burst.jsonl"
    count = 1003
    with path.open("wb") as stream:
        if reader_type is CodexReader:
            stream.write(headers())
        for index in range(count):
            if reader_type is CodexReader:
                stream.write(request(index, AT))
            else:
                stream.write(
                    (
                        json.dumps(
                            {
                                "type": "assistant",
                                "timestamp": "2026-09-01T00:00:00Z",
                                "sessionId": "session",
                                "message": {
                                    "id": f"message-{index}",
                                    "model": "Model",
                                    "usage": {"input_tokens": 10, "output_tokens": 2},
                                },
                            }
                        )
                        + "\n"
                    ).encode()
                )
    checkpoint = CheckpointRecord()
    identities: list[str | None] = []
    sizes: list[int] = []
    while True:
        batch = reader_type().read(str(path), checkpoint, AT)
        assert not batch.retry
        assert len(batch.events) <= 500
        sizes.append(len(batch.events))
        identities.extend(event.request_id for event in batch.events)
        checkpoint = CheckpointRecord(json.loads(json.dumps(batch.checkpoint)))
        if not batch.more:
            break
    assert sizes == [500, 500, 3]
    assert len(identities) == len(set(identities)) == count
    assert reader_type().read(str(path), checkpoint, AT).events == ()


def test_idle_writer_releases_completed_task_result_closure_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Payload:
        pass

    references: list[weakref.ReferenceType[object]] = []
    idle = Event()

    def factory():
        captured = Payload()

        def task(work: UnitOfWork) -> Payload:
            event, result = Payload(), Payload()
            references.extend(weakref.ref(value) for value in (captured, event, result, work))
            work.emit(event)
            return result

        return task

    class ObservedWriter(WriteExecutor):
        def __init__(self, path: Path) -> None:
            super().__init__(path, publish=lambda event: None)
            original = self._queue.get

            def get(block: bool = True, timeout: float | None = None):
                if references:
                    idle.set()
                return original(block=block, timeout=timeout)

            monkeypatch.setattr(self._queue, "get", get)

    with ObservedWriter(tmp_path / "ledger.sqlite3") as writer:
        result = writer.execute(factory())
        assert idle.wait(2)
        del result
        gc.collect()
        assert all(reference() is None for reference in references)


def test_bounded_source_batches_bill_every_request_once_across_restart(tmp_path: Path) -> None:
    home = tmp_path / "home"
    source = sessions(home) / "burst.jsonl"
    source.write_bytes(headers() + b"".join(request(index, AT) for index in range(1003)))
    path = tmp_path / "ledger.sqlite3"
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        writer.execute(seed_prices)
        scan = scanner(writer, readers, home, FakeClock(AT))
        assert scan.run().more
        assert count(writer, "identity") == count(writer, "ledger") == 500
    with WriteExecutor(path) as writer, ReadPool(path) as readers:
        scan = scanner(writer, readers, home, FakeClock(AT))
        for _ in range(10):
            if not scan.run().more:
                break
        assert count(writer, "identity") == count(writer, "ledger") == 1003
        assert count(writer, "inbox") == 0
        scan.run()
        assert count(writer, "ledger") == 1003
