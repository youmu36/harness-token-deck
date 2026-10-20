# pyright: reportPrivateUsage=false
# Tests intentionally exercise the bridge lifecycle hooks hidden from JavaScript.

"""P4.1 command validation, safe failures and bounded invocation contracts."""

import json
from collections.abc import Iterator
from threading import Event

import pytest
from htu.application.events import InProcessEventBus
from htu.application.ports import UsageChanged
from htu.domain.common.errors import DomainError
from htu.domain.usage.model import Harness
from htu.ipc.app import register_app_commands
from htu.ipc.bridge import Bridge
from htu.ipc.dto.app import HelloResponse, SyncResponse
from htu.ipc.dto.base import Envelope, Failure, RateStr, Revisions, StrictModel, Success
from htu.ipc.events import EventForwarder
from htu.ipc.router import Router
from pydantic import TypeAdapter


class EmptyRequest(StrictModel):
    pass


class Result(StrictModel):
    rate: RateStr | None = None


class NestedRequest(StrictModel):
    patch: EmptyRequest


@pytest.fixture
def router() -> Iterator[Router]:
    with Router() as value:
        yield value


def test_handshake_sync_and_only_public_bridge_method(router: Router) -> None:
    bus = InProcessEventBus()
    events = EventForwarder(bus)
    revisions: Revisions = {"usage": 4}
    register_app_commands(
        router, app_version="0.4.0", data_dir="D:/fixture", revisions=lambda: revisions, events=events
    )
    bridge = Bridge(router, events)
    try:
        assert [name for name in dir(bridge) if not name.startswith("_")] == ["call"]
        hello = bridge.call("app.hello", {"clientProtocol": 1})
        checked = TypeAdapter[Envelope[HelloResponse]](Envelope[HelloResponse]).validate_python(hello)
        assert isinstance(checked, Success) and checked.data.appVersion == "0.4.0"
        assert checked.data.protocol == 1 and checked.data.mode == "desktop"
        assert checked.data.capabilities == ["app.hello", "app.openPath", "app.pickPath", "app.sync"]
        assert checked.data.dataDir == "D:/fixture" and checked.data.revisions == revisions
        bus.publish(UsageChanged((Harness.CODEX,), 5))
        revisions["usage"] = 5
        synced = TypeAdapter[Envelope[SyncResponse]](Envelope[SyncResponse]).validate_python(
            bridge.call("app.sync", {})
        )
        assert isinstance(synced, Success) and synced.data.revisions == {"usage": 5}
        assert synced.data.pendingEvents[0].model_dump() == {
            "name": "usage.changed",
            "payload": {"harnesses": ["codex"], "revision": 5},
        }
        assert len(events.snapshot()) == 1  # Sync summarizes without consuming recovery events.
        assert router.commands["app.hello"].kind == router.commands["app.sync"].kind == "read"
    finally:
        bridge._close()


@pytest.mark.parametrize(
    "command,payload,code,field",
    [
        ("unknown.command", {}, "UNKNOWN_COMMAND", "command"),
        (123, {}, "INVALID_REQUEST", "command"),
        ("test.empty", [], "INVALID_REQUEST", "payload"),
        ("test.empty", {"unexpected": "SQL secret"}, "INVALID_REQUEST", "unexpected"),
        ("test.nested", {"patch": {"unexpected": "SQL secret"}}, "INVALID_REQUEST", "patch.unexpected"),
    ],
)
def test_invalid_requests_have_fields_without_echoing_input(
    router: Router, command: object, payload: object, code: str, field: str
) -> None:
    router.register("test.empty", request=EmptyRequest, response=Result, kind="read")(lambda _: Result())
    router.register("test.nested", request=NestedRequest, response=Result, kind="write")(lambda _: Result())
    envelope = router.call(command, payload)
    checked = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(envelope)
    assert isinstance(checked, Failure) and checked.error.code == code and checked.error.field == field
    assert not checked.error.retryable
    assert "SQL secret" not in json.dumps(envelope)


@pytest.mark.parametrize(
    "payload", [{"clientProtocol": True}, {"clientProtocol": "1"}, {"clientProtocol": 1.0}]
)
def test_handshake_uses_strict_json_integer(router: Router, payload: dict[str, object]) -> None:
    events = EventForwarder(InProcessEventBus())
    register_app_commands(
        router, app_version="0.4.0", data_dir="fixture", revisions=lambda: {}, events=events
    )
    response = TypeAdapter[Envelope[HelloResponse]](Envelope[HelloResponse]).validate_python(
        router.call("app.hello", payload)
    )
    assert (
        isinstance(response, Failure)
        and response.error.code == "INVALID_REQUEST"
        and response.error.field == "clientProtocol"
    )
    events.close()


def test_protocol_mismatch_is_explicit(router: Router) -> None:
    events = EventForwarder(InProcessEventBus())
    register_app_commands(
        router, app_version="0.4.0", data_dir="fixture", revisions=lambda: {}, events=events
    )
    result = TypeAdapter[Envelope[HelloResponse]](Envelope[HelloResponse]).validate_python(
        router.call("app.hello", {"clientProtocol": 2})
    )
    assert isinstance(result, Failure) and result.error.code == "PROTOCOL_MISMATCH"
    events.close()


def test_domain_conflict_metadata_and_null_response_are_preserved(router: Router) -> None:
    @router.register("test.conflict", request=EmptyRequest, response=Result, kind="write")
    def conflict(request: EmptyRequest) -> Result:
        raise DomainError(
            "WALLET_REVISION_CONFLICT", "钱包余额已更新", "expected_revision", conflict_revision=8
        )

    result = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(router.call("test.conflict", {}))
    assert isinstance(result, Failure) and result.error.code == "WALLET_REVISION_CONFLICT"
    assert result.error.field == "expectedRevision" and result.error.conflictRevision == 8
    assert result.error.logId is None and not result.error.retryable
    router.register("test.null", request=EmptyRequest, response=Result, kind="read")(lambda _: Result())
    assert router.call("test.null", {}) == {"ok": True, "data": {"rate": None}}


def test_unexpected_exception_is_logged_without_leaking_details(
    router: Router, caplog: pytest.LogCaptureFixture
) -> None:
    @router.register("test.fail", request=EmptyRequest, response=Result, kind="read")
    def fail(request: EmptyRequest) -> Result:
        raise RuntimeError("SELECT password FROM secrets -- raw network response")

    envelope = router.call("test.fail", {})
    result = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(envelope)
    assert isinstance(result, Failure) and result.error.code == "INTERNAL_ERROR" and result.error.logId
    assert result.error.logId in result.error.message and result.error.logId in caplog.text
    assert "SELECT password" in caplog.text
    assert "SELECT" not in json.dumps(envelope) and "Traceback" not in json.dumps(envelope)


def test_invalid_response_is_internal_error(router: Router) -> None:
    @router.register("test.badResponse", request=EmptyRequest, response=Result, kind="read")
    def bad_response(request: EmptyRequest) -> Result:
        return Result.model_construct(rate=123)

    result = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(
        router.call("test.badResponse", {})
    )
    assert isinstance(result, Failure) and result.error.code == "INTERNAL_ERROR"


@pytest.mark.parametrize("kind,code", [("read", "READ_TIMEOUT"), ("write", "WRITE_TIMEOUT")])
def test_timeout_does_not_claim_running_work_was_rolled_back(kind: str, code: str) -> None:
    release = Event()
    completed = Event()
    with Router(workers=1, capacity=1) as router:

        @router.register(
            "test.slow",
            request=EmptyRequest,
            response=Result,
            kind="write" if kind == "write" else "read",
            timeout=0.02,
        )
        def slow(request: EmptyRequest) -> Result:
            release.wait(1)
            completed.set()
            return Result()

        try:
            result = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(
                router.call("test.slow", {})
            )
            assert isinstance(result, Failure) and result.error.code == code and result.error.retryable
            busy = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(
                router.call("test.slow", {})
            )
            assert isinstance(busy, Failure) and busy.error.code == "SERVER_BUSY" and busy.error.retryable
            assert not completed.is_set()
        finally:
            release.set()
    assert completed.is_set()


def test_handler_timeout_error_is_not_a_router_wait_timeout(router: Router) -> None:
    @router.register("test.timeoutError", request=EmptyRequest, response=Result, kind="write")
    def timeout_error(request: EmptyRequest) -> Result:
        raise TimeoutError("handler failure")

    result = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(
        router.call("test.timeoutError", {})
    )
    assert isinstance(result, Failure) and result.error.code == "INTERNAL_ERROR"


def test_queued_timeout_cancels_before_handler_can_run() -> None:
    started, release, queued_ran = Event(), Event(), Event()
    with Router(workers=1, capacity=2) as router:

        @router.register("test.block", request=EmptyRequest, response=Result, kind="read", timeout=0.02)
        def block(request: EmptyRequest) -> Result:
            started.set()
            release.wait(1)
            return Result()

        @router.register("test.queued", request=EmptyRequest, response=Result, kind="write", timeout=0.02)
        def queued(request: EmptyRequest) -> Result:
            queued_ran.set()
            return Result()

        try:
            router.call("test.block", {})
            assert started.is_set()
            result = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(
                router.call("test.queued", {})
            )
            assert isinstance(result, Failure) and result.error.code == "WRITE_TIMEOUT"
        finally:
            release.set()
    assert not queued_ran.is_set()


def test_invalid_domain_error_metadata_cannot_escape_bridge(router: Router) -> None:
    @router.register("test.badError", request=EmptyRequest, response=Result, kind="read")
    def bad_error(request: EmptyRequest) -> Result:
        raise DomainError("INVALID_CODE", "invalid conflict", conflict_revision=-1)

    result = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(router.call("test.badError", {}))
    assert isinstance(result, Failure) and result.error.code == "INTERNAL_ERROR"


def test_registry_rejects_duplicates_and_close_is_structured(router: Router) -> None:
    decorator = router.register("test.empty", request=EmptyRequest, response=Result, kind="read")
    decorator(lambda _: Result())
    with pytest.raises(ValueError, match="already registered"):
        decorator(lambda _: Result())
    with pytest.raises(ValueError, match="finite and positive"):
        router.register(
            "test.invalid", request=EmptyRequest, response=Result, kind="read", timeout=float("inf")
        )
    router.close()
    result = TypeAdapter[Envelope[Result]](Envelope[Result]).validate_python(router.call("test.empty", {}))
    assert isinstance(result, Failure) and result.error.code == "APP_SHUTTING_DOWN" and result.error.retryable
