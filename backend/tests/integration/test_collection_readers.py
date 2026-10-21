"""Real temporary source files and databases exercise the P3.1 reader boundaries."""

import io
import json
import os
import sqlite3
from itertools import pairwise
from pathlib import Path
from threading import Event, Thread
from typing import BinaryIO, cast

import pytest
import zstandard
from htd.adapters.collectors import codex_reader
from htd.adapters.collectors.codex_reader import CodexReader
from htd.adapters.collectors.discovery import Discovery, source_key
from htd.adapters.collectors.dsh_reader import DshReader
from htd.adapters.collectors.shared_file import open_shared
from htd.adapters.collectors.zcode_reader import ZcodeReader
from htd.application.ports import CheckpointRecord
from htd.domain.common.time import parse_instant
from htd.domain.usage.model import Harness, UsageEvent

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
START = parse_instant("2026-08-01T00:00:00Z")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def database(path: Path, *, native: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        (FIXTURES / "zcode" / ("schema-native.sql" if native else "schema.sql")).read_text(encoding="utf-8")
    )
    return connection


def insert(connection: sqlite3.Connection, index: int, status: str = "completed") -> None:
    connection.execute(
        "INSERT INTO model_usage(id,session_id,logical_request_id,model_id,status,completed_at,"
        "raw_usage_json) "
        "VALUES(?,?,?,'Model',?,?,?)",
        (
            str(index),
            "session",
            str(index),
            status,
            1788220801000,
            json.dumps({"inputTokens": 100, "cacheReadTokens": 40, "outputTokens": 10}),
        ),
    )


def test_discovery_defaults_roots_and_whitelist(tmp_path: Path) -> None:
    sessions = tmp_path / ".codex" / "sessions"
    archive = sessions.parent / "archived_sessions"
    nested = sessions / "nested"
    nested.mkdir(parents=True)
    archive.mkdir()
    (nested / "b.jsonl").touch()
    (archive / "a.jsonl").touch()
    (sessions / "not.jsonl.zstd").touch()
    (sessions / ".credentials.yaml").write_text("must never be read")
    discovery = Discovery(home=tmp_path, environ={})
    assert discovery.default_path(Harness.CODEX) == source_key(sessions)
    assert discovery.default_path(Harness.ZCODE).endswith("db.sqlite")
    assert discovery.default_path(Harness.DSH).endswith("sessions")
    assert discovery.discover(Harness.CODEX, str(sessions)).paths == tuple(
        sorted((source_key(nested / "b.jsonl"), source_key(archive / "a.jsonl")))
    )
    assert len(discovery.discover(Harness.DSH, str(sessions)).paths) == 2
    assert not discovery.available(Harness.CODEX, "relative")
    assert not discovery.available(Harness.ZCODE, str(sessions))
    assert not discovery.discover(Harness.DSH, str(tmp_path / "missing")).available
    env = {"CODEX_HOME": str(tmp_path / "custom"), "HTD_SOURCE_ROOT_DSH": str(sessions)}
    overrides = Discovery(home=tmp_path, environ=env)
    assert overrides.default_path(Harness.CODEX) == source_key(tmp_path / "custom" / "sessions")
    assert overrides.default_path(Harness.DSH) == source_key(sessions)


def test_discovery_skips_links_and_junctions(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.jsonl").touch()
    if os.name == "nt":
        # Junction creation does not require Windows Developer Mode or admin privileges.
        import subprocess

        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)], check=True, capture_output=True
        )
    else:
        (root / "link").symlink_to(outside, target_is_directory=True)
    assert Discovery(environ={}).discover(Harness.CODEX, str(root)).paths == ()
    assert not Discovery(environ={}).available(Harness.DSH, str(root / "link"))


def test_codex_c01_c02_half_line_context_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    lines = fixture("codex/desktop.jsonl").splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:2]) + lines[2].rstrip(b"\n"))
    first = CodexReader().read(str(path), CheckpointRecord(), START)
    assert first.events == ()
    assert first.checkpoint["offset"] == len(b"".join(lines[:2]))
    with path.open("ab") as stream:
        stream.write(b"\n")
    restored = CheckpointRecord(json.loads(json.dumps(first.checkpoint)))
    second = CodexReader().read(str(path), restored, START)
    assert len(second.events) == 1 and second.events[0].model_id == "gpt-6-astra"
    assert CodexReader().read(str(path), CheckpointRecord(second.checkpoint), START).events == ()
    assert not CodexReader().read(str(path), CheckpointRecord(), parse_instant("2026-09-01T00:00:02Z")).events


@pytest.mark.parametrize(
    "name,kind",
    [
        ("desktop.jsonl", "desktop"),
        ("fork-subagent.jsonl", "desktop_subagent"),
        ("repeated-snapshots.jsonl", "desktop"),
    ],
)
def test_codex_request_only_and_subagent(tmp_path: Path, name: str, kind: str) -> None:
    path = tmp_path / name
    path.write_bytes(fixture("codex/" + name))
    batch = CodexReader().read(str(path), CheckpointRecord(), START)
    assert len(batch.events) == 1 and batch.events[0].source_kind == kind
    assert not batch.diagnostics


@pytest.mark.parametrize("rewrite", ["shorter", "same_size"])
def test_codex_anchor_rewrite_bad_line_clears_model(tmp_path: Path, rewrite: str) -> None:
    path = tmp_path / "session.jsonl"
    original = fixture("codex/desktop.jsonl")
    path.write_bytes(original)
    first = CodexReader().read(str(path), CheckpointRecord(), START)
    changed = original.replace(b"response-1", b"response-2")
    if rewrite == "shorter":
        lines = changed.splitlines(keepends=True)
        changed = b"".join(lines[:2]) + b"broken\n" + lines[2]
    path.write_bytes(changed)
    second = CodexReader().read(str(path), CheckpointRecord(first.checkpoint), START)
    assert "source_rewritten" in second.diagnostics
    assert second.events[0].request_id == "response-2"
    if rewrite == "shorter":
        assert "invalid_json" in second.diagnostics and second.events[0].model_id is None


@pytest.mark.parametrize(
    "state", [{}, {"offset": True}, {"offset": 0, "anchor": "bad", "context": {}, "diagnostics": []}]
)
def test_codex_invalid_checkpoint_rescans(tmp_path: Path, state: dict[str, object]) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(fixture("codex/desktop.jsonl"))
    result = CodexReader().read(str(path), CheckpointRecord(state), START)
    assert "invalid_checkpoint" in result.diagnostics and len(result.events) == 1


def test_codex_small_batch_and_line_limits(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(fixture("codex/desktop.jsonl"))
    reader = CodexReader(line_limit=400, batch_limit=400)
    checkpoint = CheckpointRecord()
    events: list[UsageEvent] = []
    rounds = 0
    while True:
        result = reader.read(str(path), checkpoint, START)
        checkpoint = CheckpointRecord(result.checkpoint)
        events.extend(result.events)
        rounds += 1
        if not result.more:
            break
        assert rounds < 10
    assert rounds > 1 and len(events) == 1
    limited = CodexReader(line_limit=100, batch_limit=200).read(str(path), CheckpointRecord(), START)
    assert limited.checkpoint["offset"] == 0 and limited.diagnostics == ("source_line_too_large",)
    assert not limited.more


def test_codex_length_snapshot_during_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "session.jsonl"
    data = fixture("codex/desktop.jsonl")
    path.write_bytes(data)
    appended = data.splitlines(keepends=True)[2].replace(b"response-1", b"response-2")

    class AppendingReader(io.BufferedReader):
        appended = False

        def readline(self, size: int | None = -1, /) -> bytes:
            if not self.appended:
                with path.open("ab") as output:
                    output.write(appended)
                self.appended = True
            return super().readline(size)

    def opened(name: str) -> BinaryIO:
        return cast(BinaryIO, AppendingReader(io.FileIO(name, "rb")))

    monkeypatch.setattr(codex_reader, "open_shared", opened)
    result = CodexReader().read(str(path), CheckpointRecord(), START)
    assert len(result.events) == 1 and result.checkpoint["offset"] == len(data)
    monkeypatch.setattr(codex_reader, "open_shared", open_shared)
    next_batch = CodexReader().read(str(path), CheckpointRecord(result.checkpoint), START)
    assert len(next_batch.events) == 1 and next_batch.events[0].request_id == "response-2"


@pytest.mark.parametrize("shorten", [False, True])
def test_codex_rewrite_during_read_discards_buffered_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shorten: bool,
) -> None:
    path = tmp_path / "session.jsonl"
    data = fixture("codex/desktop.jsonl")
    path.write_bytes(data)

    class RewritingReader(io.BufferedReader):
        rewritten = False

        def readline(self, size: int | None = -1, /) -> bytes:
            line = super().readline(size)
            if not self.rewritten:
                changed = data[:100] if shorten else data.replace(b"response-1", b"response-2")
                path.write_bytes(changed)
                self.rewritten = True
            return line

    def opened(name: str) -> BinaryIO:
        return cast(BinaryIO, RewritingReader(io.FileIO(name, "rb")))

    monkeypatch.setattr(codex_reader, "open_shared", opened)
    result = CodexReader().read(str(path), CheckpointRecord(), START)
    assert result.retry and not result.events and result.checkpoint == {}
    assert result.diagnostics == ("source_changed_during_read",)


def test_shared_reader_with_continuous_append_and_delete_sharing(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_bytes(fixture("codex/desktop.jsonl"))
    ready, stop = Event(), Event()
    errors: list[Exception] = []

    def append() -> None:
        try:
            with path.open("ab") as output:
                ready.set()
                while not stop.wait(0.001):
                    output.write(b"{}\n")
                    output.flush()
        except Exception as error:
            errors.append(error)

    thread = Thread(target=append)
    thread.start()
    try:
        assert ready.wait(5)
        assert len(CodexReader().read(str(path), CheckpointRecord(), START).events) == 1
    finally:
        stop.set()
        thread.join(5)
    assert not errors and not thread.is_alive()
    with open_shared(str(path)) as stream:
        path.rename(tmp_path / "renamed.jsonl")
        assert stream.read(1) == b"{"


@pytest.mark.parametrize("frames", [0, 1, 4])
def test_dsh_plain_and_all_concatenated_frames(tmp_path: Path, frames: int) -> None:
    data = fixture("dsh/session.jsonl")
    path = tmp_path / "session.jsonl.zstd"
    if frames:
        boundaries = [len(data) * index // frames for index in range(frames + 1)]
        compressor = zstandard.ZstdCompressor()
        data = b"".join(compressor.compress(data[left:right]) for left, right in pairwise(boundaries))
    path.write_bytes(data)
    result = DshReader().read(str(path), CheckpointRecord(), START)
    assert len(result.events) == 2 and not result.diagnostics
    assert sum(e.tokens.input or 0 for e in result.events) == 21933
    assert sum(e.tokens.cache_read or 0 for e in result.events) == 10112
    assert sum(e.tokens.output or 0 for e in result.events) == 1379
    assert not DshReader().read(str(path), CheckpointRecord(), parse_instant("2026-09-01T00:00:00Z")).events


def test_dsh_bad_line_clears_model_and_invalid_checkpoint_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    lines = fixture("dsh/session.jsonl").splitlines()
    path.write_bytes(b"\n".join([*lines[:-1], b"broken", lines[-1]]))
    result = DshReader().read(str(path), CheckpointRecord(diagnostics=("invalid_checkpoint",)), START)
    assert len(result.events) == 2 and result.events[-1].model_id is None
    assert result.diagnostics == ("invalid_checkpoint", "invalid_json")
    assert result.checkpoint == {}


@pytest.mark.parametrize("compressed", [False, True])
def test_dsh_limits_and_decode_failure(tmp_path: Path, compressed: bool) -> None:
    path = tmp_path / "session.jsonl"
    data = fixture("dsh/session.jsonl")
    path.write_bytes(zstandard.ZstdCompressor().compress(data) if compressed else data)
    truncated = DshReader(byte_limit=len(data) - 20).read(str(path), CheckpointRecord(), START)
    assert len(truncated.events) == 1 and truncated.diagnostics == ("source_truncated",)
    assert not truncated.more
    path.write_bytes(zstandard.ZstdCompressor().compress(data))
    oversized = DshReader(container_limit=10).read(str(path), CheckpointRecord(), START)
    assert not oversized.events and oversized.diagnostics == ("source_file_too_large",)
    path.write_bytes(b"\x28\xb5\x2f\xfdgarbage")
    assert DshReader().read(str(path), CheckpointRecord(), START).diagnostics == ("source_decode_failed",)


@pytest.mark.parametrize("native", [False, True])
def test_zcode_paging_running_retry_and_native_fallback(tmp_path: Path, native: bool) -> None:
    path = tmp_path / "source.sqlite"
    with database(path, native=native) as connection:
        for index in range(520):
            insert(connection, index, "running" if index == 0 else "completed")
    connection.close()
    reader = ZcodeReader()
    first = reader.read(str(path), CheckpointRecord(), START)
    assert len(first.events) == 499 and first.more and first.checkpoint["pending"] == [1]
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE model_usage SET status='cancelled',raw_usage_json=NULL WHERE rowid=1")
        if native:
            connection.execute(
                "UPDATE model_usage SET input_tokens=80,output_tokens=7,reasoning_tokens=2,"
                "cache_read_input_tokens=30,cache_creation_input_tokens=0 WHERE rowid=1"
            )
    second = reader.read(str(path), CheckpointRecord(first.checkpoint), START)
    assert len(second.events) == 21 and not second.more and second.checkpoint["pending"] == []
    assert second.events[0].status == "cancelled"
    assert second.events[0].tokens.input == (80 if native else None)
    assert second.events[0].tokens.cache_read == (30 if native else None)


def test_zcode_rewrite_pending_removed_and_bad_terminal(tmp_path: Path) -> None:
    path = tmp_path / "source.sqlite"
    with database(path) as connection:
        insert(connection, 0, "running")
        insert(connection, 1)
        insert(connection, 2, "unknown-terminal")
        insert(connection, 3)
        connection.execute("UPDATE model_usage SET raw_usage_json='{bad' WHERE rowid=2")
    connection.close()
    first = ZcodeReader().read(str(path), CheckpointRecord(), START)
    assert (
        len(first.events) == 1
        and first.checkpoint["pending"] == [1]
        and first.checkpoint["quarantined"] == [2, 3]
    )
    assert set(first.diagnostics) == {"invalid_usage_json", "source_status_unsupported"}
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM model_usage WHERE rowid=1")
    second = ZcodeReader().read(str(path), CheckpointRecord(first.checkpoint), START)
    assert "pending_request_removed" in second.diagnostics
    third = ZcodeReader().read(str(path), CheckpointRecord(second.checkpoint), START)
    assert "pending_request_removed" not in third.diagnostics
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE model_usage SET id='replacement' WHERE rowid=4")
    rewritten = ZcodeReader().read(str(path), CheckpointRecord(third.checkpoint), START)
    assert "source_rewritten" in rewritten.diagnostics and len(rewritten.events) == 1


def test_zcode_missing_schema_busy_and_read_only(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    assert ZcodeReader().read(str(path), CheckpointRecord(), START).diagnostics == (
        "source_database_unavailable",
    )
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE model_usage(id TEXT)")
    assert ZcodeReader().read(str(path), CheckpointRecord(), START).diagnostics == (
        "source_schema_unsupported",
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        blocked = ZcodeReader().read(str(path), CheckpointRecord(), START)
        assert blocked.diagnostics == ("source_database_busy",) and blocked.retry
    finally:
        connection.rollback()
        connection.close()
