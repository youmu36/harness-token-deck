"""Tool continuation, child sessions and parser upgrades use real isolated collectors."""

import json
from pathlib import Path
from typing import cast

from htd.adapters.collectors.codex_reader import PARSER_REVISION, CodexReader
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import CheckpointRecord
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import parse_instant
from htd.domain.usage.model import Harness

from .scan_helpers import AT, FIXTURES, count, headers, request, scanner, seed_prices, sessions


def test_upgrade_replays_old_rejected_child_checkpoint_and_preserves_start(tmp_path: Path) -> None:
    path = tmp_path / "child.jsonl"
    path.write_bytes((FIXTURES / "codex/fork-subagent.jsonl").read_bytes())
    first = CodexReader().read(str(path), CheckpointRecord(), AT)
    assert len(first.events) == 1
    old = dict(first.checkpoint)
    old.pop("parserRevision")
    old["context"] = {
        **cast(dict[str, object], old["context"]),
        "kind": None,
        "modelId": None,
        "turnId": None,
        "sawRequestUsage": False,
    }
    old["diagnostics"] = ["desktop_source_unverified"]
    replay = CodexReader().read(str(path), CheckpointRecord(old), AT)
    assert replay.events == first.events
    assert replay.diagnostics == ()
    assert replay.checkpoint["parserRevision"] == PARSER_REVISION
    assert CodexReader().read(str(path), CheckpointRecord(replay.checkpoint), AT).events == ()
    later = parse_instant("2026-09-02T00:00:00Z")
    assert CodexReader().read(str(path), CheckpointRecord(old), later).events == ()


def test_tool_requests_child_and_archived_replays_are_billed_exactly_once(tmp_path: Path) -> None:
    root = sessions(tmp_path)
    tools = b'{"type":"response_item","payload":{"type":"function_call","name":"tool"}}\n'
    parent = root / "parent.jsonl"
    parent.write_bytes(headers() + request(1, AT) + tools + request(2, AT))
    child = root / "child.jsonl"
    child.write_bytes((FIXTURES / "codex/fork-subagent.jsonl").read_bytes())
    archive = root.parent / "archived_sessions"
    archive.mkdir()
    (archive / "parent-copy.jsonl").write_bytes(parent.read_bytes())
    database = tmp_path / "ledger.sqlite"
    with WriteExecutor(database) as writer, ReadPool(database) as pool:
        writer.execute(seed_prices)
        scan = scanner(writer, pool, tmp_path, FakeClock(AT))
        result = scan.run()
        codex = next(source for source in result.sources if source.harness == Harness.CODEX)
        assert codex.inserted == 3 and codex.duplicates == 2
        assert count(writer, "ledger") == 3
    with WriteExecutor(database) as writer, ReadPool(database) as pool:
        restarted = scanner(writer, pool, tmp_path, FakeClock(AT))
        restarted.run()
        assert count(writer, "ledger") == 3
        parent.write_bytes(parent.read_bytes() + tools + request(3, AT))
        restarted.run()
        assert count(writer, "ledger") == 4


def test_snapshot_only_gap_visible_then_clears_after_request_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.jsonl"
    snapshot = b'{"type":"event_msg","payload":{"type":"token_count","info":{}}}\n'
    path.write_bytes(headers() + snapshot)
    first = CodexReader().read(str(path), CheckpointRecord(), AT)
    assert first.diagnostics == ("request_usage_format_unverified",)
    assert first.events == ()
    path.write_bytes(path.read_bytes() + request(1, AT))
    second = CodexReader().read(str(path), CheckpointRecord(json.loads(json.dumps(first.checkpoint))), AT)
    assert len(second.events) == 1 and second.diagnostics == ()
    third = CodexReader().read(str(path), CheckpointRecord(second.checkpoint), AT)
    assert third.events == () and third.diagnostics == ()


def test_old_snapshot_only_files_do_not_degrade_the_live_collection_window(tmp_path: Path) -> None:
    path = tmp_path / "old-snapshot.jsonl"
    snapshot: dict[str, object] = {
        "timestamp": "2026-08-31T23:59:59Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {}},
    }
    path.write_bytes(headers() + (json.dumps(snapshot) + "\n").encode())
    first = CodexReader().read(str(path), CheckpointRecord(), AT)
    assert first.diagnostics == () and first.events == ()
    snapshot["timestamp"] = "2026-09-01T00:00:00Z"
    path.write_bytes(path.read_bytes() + (json.dumps(snapshot) + "\n").encode())
    second = CodexReader().read(str(path), CheckpointRecord(first.checkpoint), AT)
    assert second.diagnostics == ("request_usage_format_unverified",)
    assert second.events == ()


def test_reinstalled_desktop_replays_rejected_revision_three_and_bills_once(tmp_path: Path) -> None:
    root = sessions(tmp_path)
    path = root / "new-desktop.jsonl"
    path.write_bytes(headers().replace(b"Codex Desktop", b"codex_work_desktop") + request(1, AT))
    batch = CodexReader().read(str(path), CheckpointRecord(), AT)
    assert len(batch.events) == 1 and batch.events[0].source_kind == "desktop"
    rejected = dict(batch.checkpoint)
    rejected["parserRevision"] = 3
    rejected["context"] = {
        **cast(dict[str, object], rejected["context"]),
        "originatorOk": False,
        "kind": None,
        "modelId": None,
        "turnId": None,
        "sawRequestUsage": False,
    }
    rejected["diagnostics"] = ["desktop_source_unverified"]
    replay = CodexReader().read(str(path), CheckpointRecord(rejected), AT)
    assert replay.events == batch.events and replay.diagnostics == ()
    assert CodexReader().read(str(path), CheckpointRecord(replay.checkpoint), AT).events == ()
    assert (
        CodexReader()
        .read(str(path), CheckpointRecord(rejected), parse_instant("2026-09-02T00:00:00Z"))
        .events
        == ()
    )
    database = tmp_path / "ledger.sqlite"
    with WriteExecutor(database) as writer, ReadPool(database) as pool:
        writer.execute(seed_prices)
        scan = scanner(writer, pool, tmp_path, FakeClock(AT))
        scan.run()
        scan.run()
        assert count(writer, "ledger") == 1
    with WriteExecutor(database) as writer, ReadPool(database) as pool:
        scanner(writer, pool, tmp_path, FakeClock(AT)).run()
        assert count(writer, "ledger") == 1
