"""Recent provider discovery, named aliases and Claude collection in isolated sources."""

import json
import shutil
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from htu.adapters.collectors.claude_reader import ClaudeReader
from htu.adapters.collectors.discovery import Discovery
from htu.adapters.sqlite.connection import open_connection
from htu.adapters.sqlite.migrate import MIGRATIONS, migrate
from htu.application.ports import CheckpointRecord
from htu.application.remote.ports import MemoryCredentialStore
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import parse_instant
from htu.domain.sources.claude import parse_record
from htu.domain.usage.model import Diagnostic, Harness, Skip, Tokens, UsageEvent

from .scan_helpers import seed_prices

AT = parse_instant("2026-10-14T00:00:00Z")


def record(
    request: str = "msg-1",
    *,
    timestamp: str = "2026-10-13T12:00:00Z",
    provider: str | None = "https://shop-one.example",
) -> dict[str, object]:
    return {
        "type": "assistant",
        "sessionId": "session",
        "uuid": str(uuid4()),
        "timestamp": timestamp,
        "providerName": provider,
        "message": {
            "id": request,
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "PRIVATE BODY NEVER SAVED"}],
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 200,
                "output_tokens": 20,
            },
        },
    }


def write_records(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_claude_request_identity_and_cache_semantics() -> None:
    row = record()
    event = parse_record(row)
    assert isinstance(event, UsageEvent)
    assert event.harness == Harness.CLAUDE and event.tokens == Tokens(600, 300, 200, 20)
    assert (
        event.provider_name == "https://shop-one.example"
        and "cache_write_pricing_unsupported" not in event.issues
    )
    second = parse_record({**row, "uuid": str(uuid4())})
    assert isinstance(second, UsageEvent) and event.dedupe_key == second.dedupe_key
    copied = parse_record({**row, "sessionId": "forked-session"})
    assert isinstance(copied, UsageEvent) and copied.dedupe_key == event.dedupe_key
    assert "PRIVATE BODY" not in repr(event)
    message = cast(dict[str, object], row["message"])
    message["model"] = "<synthetic>"
    assert isinstance(parse_record(row), Skip)
    message["model"] = "claude"
    cast(dict[str, object], message["usage"])["input_tokens"] = True
    assert isinstance(parse_record(row), Diagnostic)


def test_claude_reader_partial_append_and_historical_boundary(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    write_records(path, [record(timestamp="2026-10-09T00:00:00Z"), record("msg-2")])
    reader = ClaudeReader()
    start = parse_instant("2026-10-11T00:00:00Z")
    first = reader.read(str(path), CheckpointRecord(), start)
    assert len(first.events) == 1 and first.events[0].request_id == "msg-2"
    third = json.dumps(record("msg-3")).encode()
    with path.open("ab") as stream:
        stream.write(third[:20])
    partial = reader.read(str(path), CheckpointRecord(first.checkpoint), start)
    assert not partial.events
    with path.open("ab") as stream:
        stream.write(third[20:] + b"\n")
    completed = reader.read(str(path), CheckpointRecord(partial.checkpoint), start)
    assert len(completed.events) == 1 and completed.events[0].request_id == "msg-3"
    assert "PRIVATE BODY" not in json.dumps(completed.checkpoint)


@pytest.fixture
def provider_app(tmp_path: Path) -> Iterator[Application]:
    roots = {h: tmp_path / h.value for h in Harness}
    roots[Harness.CLAUDE].mkdir()
    write_records(
        roots[Harness.CLAUDE] / "project/session.jsonl",
        [
            record(),
            record("msg-2", provider="https://shop-two.example/v1/"),
            record("old", timestamp="2026-10-10T00:00:00Z", provider="old-shop"),
            record("future", timestamp="2026-10-15T00:00:00Z", provider="future-shop"),
            record("unknown", provider=None),
            record(),
        ],
    )
    discovery = Discovery(
        home=tmp_path, environ={f"HTU_SOURCE_ROOT_{h.value.upper()}": str(path) for h, path in roots.items()}
    )
    with Application(
        tmp_path / "data",
        clock=FakeClock(AT),
        discovery=discovery,
        credentials=MemoryCredentialStore(),
        start_scheduler=False,
    ) as app:
        yield app


def scanned(app: Application) -> tuple[str, list[dict[str, object]]]:
    result = app.router.call("wallet.scanProviders", {"operationId": str(uuid4())})
    assert result["ok"]
    task_id = cast(str, cast(dict[str, object], result["data"])["taskId"])
    for _ in range(100):
        task = app.task_pool.get(task_id)
        if task["status"] == "completed":
            value = cast(dict[str, object], task["result"])
            assert value["missingProviderCount"] == 1
            return task_id, cast(list[dict[str, object]], value["providers"])
        assert task["status"] not in ("failed", "cancelled"), task
        time.sleep(0.01)
    raise AssertionError("Provider scan did not complete")


def test_provider_scan_requires_names_and_saves_atomic_aliases(provider_app: Application) -> None:
    app = provider_app
    before = app.writer.execute(lambda w: list(w.connection.execute("SELECT * FROM checkpoint")))
    task_id, candidates = scanned(app)
    assert [p["identifier"] for p in candidates] == ["https://shop-one.example", "https://shop-two.example"]
    assert not app.wallet.list(True)["vendors"]
    assert app.writer.execute(lambda w: list(w.connection.execute("SELECT * FROM checkpoint"))) == before
    blank = app.router.call(
        "wallet.createScannedVendors",
        {
            "operationId": str(uuid4()),
            "taskId": task_id,
            "providers": [{"id": candidates[0]["id"], "name": "   "}],
        },
    )
    assert not blank["ok"] and not app.wallet.list(True)["vendors"]
    duplicate = app.router.call(
        "wallet.createScannedVendors",
        {
            "operationId": str(uuid4()),
            "taskId": task_id,
            "providers": [{"id": p["id"], "name": "Same"} for p in candidates],
        },
    )
    assert not duplicate["ok"] and not app.wallet.list(True)["vendors"]
    assert (
        app.writer.execute(
            lambda w: w.connection.execute("SELECT COUNT(*) FROM provider_alias").fetchone()[0]
        )
        == 0
    )
    request = {
        "operationId": str(uuid4()),
        "taskId": task_id,
        "providers": [{"id": p["id"], "name": f"Named {index}"} for index, p in enumerate(candidates)],
    }
    saved = app.router.call("wallet.createScannedVendors", request)
    assert saved["ok"] and app.router.call("wallet.createScannedVendors", request) == saved
    assert len(cast(list[object], app.wallet.list(True)["vendors"])) == 2
    _, again = scanned(app)
    assert again == []
    assert isinstance(app.clock, FakeClock)
    app.clock.advance(1_900_000)
    assert app.router.call("wallet.createScannedVendors", request) == saved  # receipt survives task expiry
    assert not app.router.call("wallet.createScannedVendors", {**request, "operationId": str(uuid4())})["ok"]
    raw = app.writer.execute(lambda w: "\n".join(w.connection.iterdump()))
    assert "PRIVATE BODY" not in raw and "old-shop" not in raw


def test_claude_live_collection_never_imports_before_start_and_deduplicates(
    provider_app: Application,
) -> None:
    app = provider_app
    source = next(s for s in app.store.sources().sources if s.harness == Harness.CLAUDE)
    write_records(Path(source.path) / "project/session.jsonl", [record()])
    app.scan.collect()
    source = next(s for s in app.store.sources().sources if s.harness == Harness.CLAUDE)
    path = Path(source.path) / "live.jsonl"
    write_records(
        path,
        [record("live", timestamp="2026-10-14T00:00:00Z"), record("live", timestamp="2026-10-14T00:00:00Z")],
    )
    app.scan.collect()
    assert (
        app.writer.execute(
            lambda w: w.connection.execute("SELECT COUNT(*) FROM inbox WHERE harness='claude'").fetchone()[0]
        )
        == 1
    )
    app.scan.collect()
    assert (
        app.writer.execute(
            lambda w: w.connection.execute("SELECT COUNT(*) FROM identity WHERE harness='claude'").fetchone()[
                0
            ]
        )
        == 1
    )

    app.writer.execute(seed_prices)
    assert app.scan.billing.run().batch.billed == 1
    summary = app.router.call(
        "stats.summary",
        {
            "harness": "claude",
            "start": "2026-10-14T00:00:00.000Z",
            "end": "2026-10-14T00:01:00.000Z",
            "offsetMinutes": 0,
            "grain": "hour",
        },
    )
    assert summary["ok"]
    metrics = cast(dict[str, object], cast(dict[str, object], summary["data"])["metrics"])
    assert metrics["recordCount"] == 1 and metrics["inputTokens"] == 600 and metrics["cacheReadTokens"] == 300
    assert metrics["outputTokens"] == 20 and metrics["totalTokens"] == 620


def test_scan_excludes_manual_vendor_website_and_rechecks_stale_candidates(provider_app: Application) -> None:
    app = provider_app
    task_id, candidates = scanned(app)
    created = app.router.call(
        "wallet.createVendor",
        {"operationId": str(uuid4()), "name": "Manual", "url": "https://SHOP-ONE.example:443/v1/"},
    )
    assert created["ok"]
    stale = app.router.call(
        "wallet.createScannedVendors",
        {
            "operationId": str(uuid4()),
            "taskId": task_id,
            "providers": [{"id": candidates[0]["id"], "name": "Duplicate"}],
        },
    )
    error = stale.get("error")
    assert not stale["ok"] and isinstance(error, dict) and error.get("code") == "PROVIDER_ALREADY_SAVED"
    _, again = scanned(app)
    assert [p["url"] for p in again] == ["https://shop-two.example"]


def test_published_database_upgrade_preserves_references_and_guards(tmp_path: Path) -> None:
    old = tmp_path / "old-migrations"
    old.mkdir()
    for script in MIGRATIONS.glob("*.sql"):
        if script.name < "0012":
            shutil.copyfile(script, old / script.name)
    path = tmp_path / "ledger.sqlite3"
    migrate(path, migrations=old)
    with closing(open_connection(path)) as db:
        db.execute("INSERT INTO vendor VALUES('vendor','Kept','active','2026-10-01T00:00:00.000Z',1)")
        db.execute(
            "INSERT INTO wallet_entry(id,vendor_id,kind,amount_pico,occurred_at,seq,note) "
            "VALUES('entry','vendor','adjustment','123','2026-10-01T00:00:00.000Z',1,'calibration')"
        )
        db.execute("INSERT INTO calibration VALUES('entry','123','0',0,'2026-10-01T00:00:00.000Z')")
        db.execute(
            "INSERT INTO identity VALUES('codex',?,'2026-10-01T00:00:00.000Z',0,"
            "'2026-10-01T00:00:00.000Z','live')",
            ("a" * 64,),
        )
        db.execute(
            "INSERT INTO inbox VALUES('codex',?,'2026-10-01T00:00:00.000Z','{}','2026-10-01T00:00:00.000Z')",
            ("a" * 64,),
        )
        before = {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
            for table in ("vendor", "wallet_entry", "calibration", "identity", "inbox")
        }
    migrate(path)
    with closing(open_connection(path)) as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert before == {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")] for table in before
        }
        with pytest.raises(Exception, match="append_only"):
            db.execute("UPDATE wallet_entry SET amount_pico='0'")
    assert list((tmp_path / "backups").glob("*.sqlite3"))
