"""Minimal real-process smoke of the shared router and line framing."""

import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from uuid import uuid4

from htd.application.events import InProcessEventBus
from htd.ipc.app import register_app_commands
from htd.ipc.bridge import Bridge
from htd.ipc.dto.app import HelloResponse, SyncResponse
from htd.ipc.dto.base import Envelope, Failure, Revisions, Success
from htd.ipc.dto.collection import CollectionStatusResponse, SetPausedResponse
from htd.ipc.dto.preferences import PreferencesResponse
from htd.ipc.events import EventForwarder
from htd.ipc.headless import serve
from htd.ipc.router import Router
from pydantic import TypeAdapter


def initial_revisions() -> Revisions:
    return {"preferences": 0, "pricing": 0, "wallet": 0, "sources": 0, "usage": 0}


class FlushedOutput(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushed: list[str] = []

    def flush(self) -> None:
        self.flushed.append(self.getvalue())
        super().flush()


def test_headless_invalid_lines_do_not_stop_next_request() -> None:
    events = EventForwarder(InProcessEventBus())
    with Router() as router:
        register_app_commands(
            router, app_version="0.4.0", data_dir="fixture", revisions=initial_revisions, events=events
        )
        requests = StringIO(
            'not json\n[]\n{"command":"app.sync","payload":{},"extra":true}\n'
            '{"command":"app.sync","payload":{"number":NaN}}\n'
            '{"command":"app.sync","payload":{"number":1e999}}\n'
            '{"command":"app.sync","command":"app.hello","payload":{}}\n'
            '{"command":"app.sync","payload":{}}\n'
        )
        output = FlushedOutput()
        serve(Bridge(router, events), requests, output)
        replies = [
            TypeAdapter[Envelope[SyncResponse]](Envelope[SyncResponse]).validate_json(line)
            for line in output.getvalue().splitlines()
        ]
        assert len(replies) == 7 and len(output.flushed) == 7
        assert [len(value.splitlines()) for value in output.flushed] == list(range(1, 8))
        assert all(
            isinstance(reply, Failure) and reply.error.code == "INVALID_REQUEST" for reply in replies[:6]
        )
        assert isinstance(replies[6], Success)
    events.close()


def test_headless_process_uses_only_stdout_json() -> None:
    project = Path(__file__).resolve().parents[3]
    configured = Path(os.environ["HTD_DATA_DIR"])
    process = subprocess.run(
        [sys.executable, "-m", "htd", "--headless-bridge"],
        input='{"command":"app.hello","payload":{"clientProtocol":1}}\n'
        '{"command":"missing.command","payload":{}}\n'
        '{"command":"app.sync","payload":{}}\n',
        text=True,
        capture_output=True,
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(project / "backend/src")},
        timeout=5,
        check=False,
    )
    assert process.returncode == 0 and process.stderr == ""
    lines = process.stdout.splitlines()
    assert len(lines) == 3
    hello = TypeAdapter[Envelope[HelloResponse]](Envelope[HelloResponse]).validate_json(lines[0])
    assert (
        isinstance(hello, Success)
        and hello.data.dataDir == str(configured.resolve())
        and hello.data.appVersion == (project / "VERSION").read_text(encoding="utf-8").strip()
    )
    assert json.loads(lines[1])["error"]["code"] == "UNKNOWN_COMMAND"
    synced = TypeAdapter[Envelope[SyncResponse]](Envelope[SyncResponse]).validate_json(lines[2])
    assert (
        isinstance(synced, Success)
        and synced.data.revisions["sources"] >= hello.data.revisions["sources"] >= 1
        and synced.data.revisions["preferences"] == 0
        and synced.data.revisions["usage"] == 0
    )
    assert (configured / "htd.sqlite3").is_file()
    assert "preferences.update" in hello.data.capabilities
    assert "stats.summary" in hello.data.capabilities


def test_headless_preference_and_pause_survive_process_restart() -> None:
    project = Path(__file__).resolve().parents[3]

    def exchange(requests: list[dict[str, object]]) -> list[dict[str, object]]:
        process = subprocess.run(
            [sys.executable, "-m", "htd", "--headless-bridge"],
            input="".join(json.dumps(request) + "\n" for request in requests),
            text=True,
            capture_output=True,
            cwd=project,
            env={**os.environ, "PYTHONPATH": str(project / "backend/src")},
            timeout=10,
            check=False,
        )
        assert process.returncode == 0, process.stderr
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        assert len(replies) == len(requests) and all(reply["ok"] for reply in replies), replies
        return replies

    preference_payload = {
        "patch": {"theme": "dark", "lastPage": "settings", "historyPrompted": True},
        "expectedRevision": 0,
        "operationId": str(uuid4()),
    }
    pause_payload = {"paused": True, "operationId": str(uuid4())}
    first = exchange(
        [
            {"command": "preferences.update", "payload": preference_payload},
            {"command": "collection.setPaused", "payload": pause_payload},
        ]
    )
    second = exchange(
        [
            {"command": "preferences.get", "payload": {}},
            {"command": "collection.status", "payload": {}},
            {"command": "preferences.update", "payload": preference_payload},
            {"command": "collection.setPaused", "payload": pause_payload},
        ]
    )
    original = Success[PreferencesResponse].model_validate(first[0]).data
    restored = Success[PreferencesResponse].model_validate(second[0]).data
    assert original == restored and restored.revision == 1 and restored.preferences.theme == "dark"
    assert restored.preferences.lastPage == "settings" and restored.preferences.historyPrompted
    status = Success[CollectionStatusResponse].model_validate(second[1]).data
    assert status.paused and status.state == "paused" and status.nextPeriodicAt is None
    assert second[2] == first[0]
    assert (
        Success[SetPausedResponse].model_validate(second[3]).data
        == Success[SetPausedResponse].model_validate(first[1]).data
    )
