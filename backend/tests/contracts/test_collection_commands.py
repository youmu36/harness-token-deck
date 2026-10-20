"""P4.2 conflicts, atomic receipts, path semantics and real scan/restart behavior."""

import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from htu.adapters.sqlite.sources_repo import SourcesRepository
from htu.application.ports import CollectionCompleted, UnmeteredRequest
from htu.bootstrap import Application
from htu.domain.common.errors import DomainError
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import format_instant
from htu.domain.usage.model import Harness
from htu.ipc.dto.base import Failure
from htu.ipc.dto.collection import (
    CollectionStatusResponse,
    ScanNowResponse,
    SetPausedResponse,
    SourcesResponse,
    ValidatePathResponse,
)
from htu.ipc.dto.preferences import PreferencesResponse
from pydantic import JsonValue

from backend.tests.integration.scan_helpers import AT, headers, request, seed_prices

from .command_helpers import failure as command_failure
from .command_helpers import invoke


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Application]:
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as application:
        yield application


def test_preferences_concurrent_revision_and_post_commit_event(app: Application) -> None:
    initial = invoke(app, "preferences.get", {}, PreferencesResponse)
    assert initial.preferences.theme == "system" and initial.preferences.lastPage == "codex"
    patches: list[dict[str, object]] = [
        {"patch": {"theme": theme}, "expectedRevision": initial.revision, "operationId": str(uuid4())}
        for theme in ("dark", "light")
    ]

    def send(payload: dict[str, object]) -> dict[str, JsonValue]:
        return app.router.call("preferences.update", payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(send, patches))
    assert sum(bool(reply["ok"]) for reply in replies) == 1
    failure = next(reply for reply in replies if not reply["ok"])
    assert failure["error"] == {
        "code": "PREFERENCES_REVISION_CONFLICT",
        "message": "偏好设置已更新，请刷新后重试",
        "field": "expectedRevision",
        "retryable": False,
        "conflictRevision": 1,
    }
    assert app.revisions()["preferences"] == 1
    assert any(event.name == "preferences.changed" for event in app.events.snapshot())
    winner = next(payload for payload, reply in zip(patches, replies, strict=True) if reply["ok"])
    replay = app.router.call("preferences.update", winner)
    assert replay == next(reply for reply in replies if reply["ok"])
    assert app.revisions()["preferences"] == 1


def test_preferences_merge_filters_noop_and_operation_reuse(app: Application) -> None:
    def update(patch: dict[str, object], revision: int) -> PreferencesResponse:
        return invoke(
            app,
            "preferences.update",
            {"patch": patch, "expectedRevision": revision, "operationId": str(uuid4())},
            PreferencesResponse,
        )

    first = update({"filters": {"codex": {"range": "7days"}}}, 0)
    second = update({"filters": {"dsh": {"range": "30days"}}, "collapsed": True}, first.revision)
    assert second.preferences.filters["codex"].range == "7days"
    assert second.preferences.filters["dsh"].range == "30days"
    assert update({}, second.revision).revision == second.revision
    operation = str(uuid4())
    payload = {"patch": {"theme": "dark"}, "expectedRevision": second.revision, "operationId": operation}
    invoke(app, "preferences.update", payload, PreferencesResponse)
    assert app.router.call("preferences.update", payload | {"patch": {"theme": "light"}})["error"] is not None
    error = command_failure(app, "collection.setPaused", {"paused": True, "operationId": operation})
    assert error.code == "OPERATION_ID_REUSED"
    assert invoke(app, "collection.status", {}, CollectionStatusResponse).state == "idle"


@pytest.mark.parametrize(
    "patch",
    [
        {"theme": "neon"},
        {"collapsed": 1},
        {"theme": None},
        {"unknown": True},
        {"filters": {"unknown": {}}},
        {"filters": {"codex": {"extra": True}}},
        {"filters": {"codex": {"range": "custom"}}},
    ],
)
def test_invalid_preferences_do_not_write(app: Application, patch: dict[str, object]) -> None:
    reply = app.router.call(
        "preferences.update",
        {
            "patch": patch,
            "expectedRevision": 0,
            "operationId": str(uuid4()),
        },
    )
    assert Failure.model_validate(reply).error.code == "INVALID_REQUEST"
    assert app.revisions()["preferences"] == 0


def test_set_path_preserves_start_and_extra_stats_cleared(app: Application, tmp_path: Path) -> None:
    source = app.store.sources().sources[0]
    app.writer.execute(
        lambda work: SourcesRepository(work.connection).begin_scan(work, Harness.CODEX, source.path, at=AT)
    )
    app.writer.execute(
        lambda work: SourcesRepository(work.connection).finish_scan(
            work,
            Harness.CODEX,
            source.path,
            at=AT,
            status="watching",
            diagnostics=(),
            unmetered_requests=(UnmeteredRequest("2026-09-01", 2),),
        )
    )
    extras = invoke(app, "sources.list", {}, SourcesResponse).sources[0].unmeteredRequests
    assert extras is not None and extras[0].count == 2 and extras[0].kind == "title_generation"
    missing = tmp_path / "later"
    payload = {
        "harness": "codex",
        "path": str(missing),
        "operationId": str(uuid4()),
        "expectedRevision": app.revisions()["sources"],
    }
    changed = invoke(app, "sources.setPath", payload, SourcesResponse)
    codex = changed.sources[0]
    assert codex.startedAt == format_instant(AT) and codex.status == "unavailable"
    assert codex.unmeteredRequests is None
    assert invoke(app, "sources.setPath", payload, SourcesResponse) == changed
    conflict = app.router.call("sources.setPath", payload | {"operationId": str(uuid4())})
    assert Failure.model_validate(conflict).error.code == "SOURCES_REVISION_CONFLICT"
    missing.mkdir()
    validation = invoke(
        app, "sources.validatePath", {"harness": "codex", "path": str(missing)}, ValidatePathResponse
    )
    assert validation.available
    assert app.revisions()["sources"] == changed.revision


@pytest.mark.parametrize(("harness", "path"), [("codex", "relative"), ("dsh", ""), ("bad", "C:\\missing")])
def test_path_validation_rejects_request(app: Application, harness: str, path: str) -> None:
    assert (
        command_failure(app, "sources.validatePath", {"harness": harness, "path": path}).code
        == "INVALID_REQUEST"
    )


def test_auto_detect_only_valid_defaults_and_same_path_noop(app: Application, tmp_path: Path) -> None:
    before = app.revisions()["sources"]
    changed = invoke(
        app,
        "sources.setPath",
        {
            "harness": "codex",
            "path": str(tmp_path / "missing"),
            "operationId": str(uuid4()),
            "expectedRevision": before,
        },
        SourcesResponse,
    )
    located = invoke(
        app,
        "sources.autoDetect",
        {
            "operationId": str(uuid4()),
            "expectedRevision": changed.revision,
        },
        SourcesResponse,
    )
    assert located.sources[0].path.lower() == os.environ["HTU_SOURCE_ROOT_CODEX"].lower()
    assert located.sources[1].status == "unavailable"  # Zcode default is a directory in this fixture.
    assert (
        invoke(
            app,
            "sources.autoDetect",
            {
                "operationId": str(uuid4()),
                "expectedRevision": located.revision,
            },
            SourcesResponse,
        ).revision
        == located.revision
    )


def test_pause_receipt_restart_and_scan_rejection(tmp_path: Path) -> None:
    data = tmp_path / "data"
    operation = str(uuid4())
    payload = {"paused": True, "operationId": operation}
    with Application(data, start_scheduler=False) as first:
        saved = invoke(first, "collection.setPaused", payload, SetPausedResponse)
        assert invoke(first, "collection.setPaused", payload, SetPausedResponse) == saved
    with Application(data) as second:
        state = invoke(second, "collection.status", {}, CollectionStatusResponse)
        assert state.paused and state.state == "paused" and state.nextPeriodicAt is None
        assert all(source.startedAt is None for source in state.sources)
        assert invoke(second, "collection.setPaused", payload, SetPausedResponse) == saved
        assert (
            command_failure(second, "collection.scanNow", {"operationId": str(uuid4())}).code
            == "COLLECTION_PAUSED"
        )


def test_scan_now_returns_after_real_billing_and_tick(app: Application) -> None:
    path = Path(os.environ["HTU_SOURCE_ROOT_CODEX"]) / "one.jsonl"
    path.write_bytes(headers() + request(1, AT))
    app.writer.execute(seed_prices)
    app.scheduler.start()
    payload = {"operationId": str(uuid4())}
    scanned = invoke(app, "collection.scanNow", payload, ScanNowResponse)
    sources = invoke(app, "sources.list", {}, SourcesResponse)
    assert sources.sources[0].ledgerCount == 1 and sources.sources[0].inboxCount == 0
    assert scanned.completedAt == format_instant(AT)
    assert scanned.nextPeriodicAt == "2026-09-01T00:00:30.000Z"
    assert invoke(app, "collection.scanNow", payload, ScanNowResponse) == scanned
    events = {event.name: event.model_dump(mode="json")["payload"] for event in app.events.snapshot()}
    assert events["usage.changed"]["revision"] == 1
    assert events["collection.tick"]["lastCompletedAt"] == scanned.completedAt
    assert events["collection.tick"]["nextPeriodicAt"] == scanned.nextPeriodicAt


def test_failed_receipt_rolls_back_preference_and_events(app: Application) -> None:
    app.writer.execute(
        lambda work: work.connection.execute(
            "CREATE TRIGGER fail_receipt BEFORE INSERT ON operation BEGIN SELECT RAISE(ABORT,'receipt'); END"
        )
    )
    reply = app.router.call(
        "preferences.update",
        {
            "patch": {"theme": "dark"},
            "expectedRevision": 0,
            "operationId": str(uuid4()),
        },
    )
    assert Failure.model_validate(reply).error.code == "INTERNAL_ERROR"
    assert invoke(app, "preferences.get", {}, PreferencesResponse).preferences.theme == "system"
    assert app.revisions()["preferences"] == 0 and app.events.snapshot() == []
    failed_pause = app.router.call("collection.setPaused", {"paused": True, "operationId": str(uuid4())})
    assert Failure.model_validate(failed_pause).error.code == "INTERNAL_ERROR"
    state = invoke(app, "collection.status", {}, CollectionStatusResponse)
    assert not state.paused and state.state == "idle"


def test_path_saved_after_startup_is_not_relocated_on_first_scan(app: Application, tmp_path: Path) -> None:
    missing = tmp_path / "create-later"
    invoke(
        app,
        "sources.setPath",
        {
            "harness": "codex",
            "path": str(missing),
            "operationId": str(uuid4()),
            "expectedRevision": app.revisions()["sources"],
        },
        SourcesResponse,
    )
    app.scheduler.start()
    invoke(app, "collection.scanNow", {"operationId": str(uuid4())}, ScanNowResponse)
    source = invoke(app, "sources.list", {}, SourcesResponse).sources[0]
    assert source.path.lower() == str(missing).lower() and source.status == "unavailable"


def test_close_timeout_keeps_storage_available_for_retry(
    app: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = app.scheduler.close
    attempts = 0

    def close(timeout: float = 10) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DomainError("COLLECTION_CLOSE_TIMEOUT", "采集仍在完成当前事务")
        original(timeout)

    monkeypatch.setattr(app.scheduler, "close", close)
    with pytest.raises(DomainError, match="采集仍在完成当前事务"):
        app.close()
    assert invoke(app, "preferences.get", {}, PreferencesResponse).revision == 0
    app.close()


def test_concurrent_operation_reuse_does_not_change_pause(
    app: Application, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    operation = str(uuid4())
    original = app.scheduler.set_paused

    def set_paused(paused: bool, *, persist: Callable[[], None] | None = None) -> None:
        entered.set()
        assert release.wait(2)
        original(paused, persist=persist)

    monkeypatch.setattr(app.scheduler, "set_paused", set_paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            app.router.call, "collection.setPaused", {"paused": True, "operationId": operation}
        )
        try:
            assert entered.wait(2)
            invoke(
                app,
                "preferences.update",
                {
                    "patch": {"theme": "dark"},
                    "expectedRevision": 0,
                    "operationId": operation,
                },
                PreferencesResponse,
            )
        finally:
            release.set()
        assert Failure.model_validate(future.result(2)).error.code == "OPERATION_ID_REUSED"
    state = invoke(app, "collection.status", {}, CollectionStatusResponse)
    assert not state.paused and state.state == "idle"


def test_tick_uses_completion_time_after_post_commit_subscribers(app: Application) -> None:
    assert isinstance(app.clock, FakeClock)
    clock = app.clock

    def advance(event: object) -> None:
        if isinstance(event, CollectionCompleted):
            clock.advance(7)

    app.bus.subscribe(advance)
    app.scheduler.start()
    result = invoke(app, "collection.scanNow", {"operationId": str(uuid4())}, ScanNowResponse)
    events = {event.name: event.model_dump(mode="json")["payload"] for event in app.events.snapshot()}
    assert events["collection.tick"]["lastCompletedAt"] == result.completedAt
    assert events["collection.tick"]["nextPeriodicAt"] == result.nextPeriodicAt
