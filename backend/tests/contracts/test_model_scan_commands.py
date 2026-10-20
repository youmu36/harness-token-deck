"""Background historical scans reuse all real readers without live ingestion side effects."""

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from htu.adapters.collectors.codex_reader import CodexReader
from htu.application.ports import CheckpointRecord, ReadBatch, TaskDone
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.usage.model import Harness
from htu.ipc.dto.collection import SourcesResponse
from htu.ipc.dto.pricing import PoolResponse
from htu.ipc.dto.tasks import TaskResponse, TaskStartedResponse

from backend.tests.integration.scan_helpers import FIXTURES, headers, request

from .command_helpers import failure, invoke
from .management_helpers import price

AT = parse_instant("2026-09-30T00:00:00Z")


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Application]:
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as instance:
        yield instance


def scan(app: Application, harness: str = "all") -> TaskResponse:
    done = Event()
    unsubscribe = app.bus.subscribe(lambda event: done.set() if isinstance(event, TaskDone) else None)
    try:
        started = invoke(
            app, "models.scan", {"harness": harness, "operationId": str(uuid4())}, TaskStartedResponse
        )
        assert done.wait(2)
        result = invoke(app, "tasks.get", {"taskId": started.taskId}, TaskResponse)
        assert result.status == "completed", result
        return result
    finally:
        unsubscribe()


def test_real_historical_scan_all_readers_flags_and_no_live_writes(app: Application, tmp_path: Path) -> None:
    boundary = Instant(AT.value - timedelta(days=3))
    root = Path(os.environ["HTU_SOURCE_ROOT_CODEX"])
    (root / "inside.jsonl").write_bytes(headers() + request(1, boundary))
    # These model contexts cannot leak into the in-range result.
    (root / "old.jsonl").write_bytes(
        headers().replace(b"gpt-6-astra", b"Old")
        + request(2, Instant(boundary.value - timedelta(milliseconds=1)))
    )
    (root / "future.jsonl").write_bytes(headers().replace(b"gpt-6-astra", b"Future") + request(3, AT))
    dsh_root = Path(os.environ["HTU_SOURCE_ROOT_DSH"])
    dsh = [
        json.loads(line)
        for line in (FIXTURES / "dsh/session.jsonl").read_text(encoding="utf-8").splitlines()[:4]
    ]
    for record in dsh:
        record["time"] = int(boundary.value.timestamp() * 1000)
    (dsh_root / "one.jsonl").write_text("\n".join(json.dumps(r) for r in dsh), encoding="utf-8")
    source_db = tmp_path / "source.sqlite3"
    with sqlite3.connect(source_db) as connection:
        connection.executescript((FIXTURES / "zcode/schema.sql").read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO model_usage VALUES ('id','session','request',0,'ZModel','completed',?,?)",
            (int(boundary.value.timestamp() * 1000), json.dumps({"inputTokens": 10, "outputTokens": 1})),
        )
    invoke(
        app,
        "sources.setPath",
        {
            "harness": "zcode",
            "path": str(source_db),
            "expectedRevision": app.revisions()["sources"],
            "operationId": str(uuid4()),
        },
        SourcesResponse,
    )
    price(app, "models.add", modelIds=["gpt-6-astra", "deepseek-v4-flash"])
    price(app, "models.remove", modelId="deepseek-v4-flash")
    before = app.revisions()
    sources_before = invoke(app, "sources.list", {}, SourcesResponse)
    result = scan(app)
    assert result.result is not None
    assert result.result["start"] == format_instant(boundary) and result.result["end"] == format_instant(AT)
    assert result.result["models"] == [
        {"modelId": "deepseek-v4-flash", "inPool": False, "excluded": True},
        {"modelId": "gpt-6-astra", "inPool": True, "excluded": False},
        {"modelId": "ZModel", "inPool": False, "excluded": False},
    ]
    assert app.revisions() == before and invoke(app, "sources.list", {}, SourcesResponse) == sources_before
    for table in ("identity", "inbox", "ledger", "checkpoint"):
        assert app.readers.get().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert len(invoke(app, "models.pool", {}, PoolResponse).models) == 1
    assert any(event.name == "task.done" for event in app.events.snapshot())


def test_model_scan_batches_local_checkpoint_and_operation_retry(app: Application) -> None:
    root = Path(os.environ["HTU_SOURCE_ROOT_CODEX"])
    (root / "many.jsonl").write_bytes(headers() + request(1, Instant(AT.value - timedelta(seconds=1))))
    app.model_scan.readers[Harness.CODEX] = CodexReader(line_limit=500, batch_limit=600)
    done = Event()
    app.bus.subscribe(lambda event: done.set() if isinstance(event, TaskDone) else None)
    payload = {"harness": "codex", "operationId": str(uuid4())}
    first = invoke(app, "models.scan", payload, TaskStartedResponse)
    assert done.wait(2)
    assert invoke(app, "models.scan", payload, TaskStartedResponse) == first
    result = invoke(app, "tasks.get", {"taskId": first.taskId}, TaskResponse)
    assert result.result and result.result["models"] == [
        {"modelId": "gpt-6-astra", "inPool": False, "excluded": False}
    ]
    assert failure(app, "models.scan", payload | {"harness": "dsh"}).code == "OPERATION_ID_REUSED"
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(1800000)
    assert failure(app, "tasks.get", {"taskId": first.taskId}).code == "TASK_NOT_FOUND"


def test_scan_cancellation_no_partial_model_or_collection_changes(app: Application) -> None:
    root = Path(os.environ["HTU_SOURCE_ROOT_CODEX"])
    (root / "one.jsonl").write_bytes(headers())
    entered, release, done = Event(), Event(), Event()

    class BlockingReader:
        def read(self, path: str, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch:
            entered.set()
            assert release.wait(2)
            return ReadBatch({})

    app.model_scan.readers[Harness.CODEX] = BlockingReader()
    app.bus.subscribe(lambda event: done.set() if isinstance(event, TaskDone) else None)
    started = invoke(
        app, "models.scan", {"harness": "codex", "operationId": str(uuid4())}, TaskStartedResponse
    )
    try:
        assert entered.wait(2)
        invoke(app, "tasks.cancel", {"taskId": started.taskId, "operationId": str(uuid4())}, TaskResponse)
    finally:
        release.set()
    assert done.wait(2)
    cancelled = invoke(app, "tasks.get", {"taskId": started.taskId}, TaskResponse)
    assert cancelled.status == "cancelled" and cancelled.error and cancelled.error.code == "TASK_CANCELLED"
    assert invoke(app, "models.pool", {}, PoolResponse).models == []


def test_scan_failure_is_sanitized_and_task_capacity_released_on_receipt_failure(app: Application) -> None:
    app.writer.execute(
        lambda work: work.connection.execute(
            "CREATE TRIGGER injected BEFORE INSERT ON operation BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    )
    assert failure(app, "models.scan", {"operationId": str(uuid4())}).code == "INTERNAL_ERROR"
    assert app.task_pool._tasks == {}  # pyright: ignore[reportPrivateUsage]
    app.writer.execute(lambda work: work.connection.execute("DROP TRIGGER injected"))
    root = Path(os.environ["HTU_SOURCE_ROOT_CODEX"])
    (root / "one.jsonl").write_bytes(headers())

    class FailingReader:
        def read(self, path: str, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch:
            raise RuntimeError("secret file content")

    app.model_scan.readers[Harness.CODEX] = FailingReader()
    done = Event()
    app.bus.subscribe(lambda event: done.set() if isinstance(event, TaskDone) else None)
    started = invoke(
        app, "models.scan", {"harness": "codex", "operationId": str(uuid4())}, TaskStartedResponse
    )
    assert done.wait(2)
    result = invoke(app, "tasks.get", {"taskId": started.taskId}, TaskResponse)
    assert result.status == "failed" and result.error and result.error.logId
    assert "secret" not in result.model_dump_json()
