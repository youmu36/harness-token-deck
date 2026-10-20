# pyright: reportPrivateUsage=false
# Tests intentionally exercise the bridge lifecycle hooks hidden from JavaScript.

"""Event buffering, latest-payload replacement and safe JavaScript transport."""

import json
from concurrent.futures import Future

import pytest
from htu.application.events import InProcessEventBus
from htu.application.ports import (
    CollectionCompleted,
    PreferencesChanged,
    PricingChanged,
    SourcesChanged,
    UsageChanged,
    WalletChanged,
)
from htu.domain.usage.model import Harness
from htu.ipc.bridge import Bridge
from htu.ipc.dto.events import CollectionTickEvent
from htu.ipc.events import EventForwarder
from htu.ipc.router import Router


def records(script: str) -> list[dict[str, object]]:
    return json.loads(script.split("const events = ", 1)[1].split("; for (", 1)[0])


def test_hidden_events_replace_latest_then_recover_in_one_push() -> None:
    bus = InProcessEventBus()
    events = EventForwarder(bus)
    scripts: list[str] = []
    with Router() as router:
        bridge = Bridge(router, events)
        bridge._ready(scripts.append)
        bridge._set_visible(False)
        bus.publish(UsageChanged((Harness.CODEX,), 1))
        bus.publish(UsageChanged((Harness.DSH,), 2))
        bus.publish(WalletChanged(("vendor",), 3))
        bus.publish(PricingChanged(4))
        bus.publish(PreferencesChanged(5))
        bus.publish(SourcesChanged(6))
        assert scripts == []
        snapshot = {item.name: item.model_dump()["payload"] for item in events.snapshot()}
        assert snapshot["usage.changed"] == {"harnesses": ["dsh"], "revision": 2}
        bridge._set_visible(True)
        assert len(scripts) == 1
        delivered = {item["name"]: item["payload"] for item in records(scripts[0])}
        assert delivered == {**snapshot, "app.visibility": {"visible": True}}
        assert events.snapshot() == []
        bridge._set_visible(True)
        assert len(scripts) == 1
        bus.publish(UsageChanged((Harness.ZCODE,), 7))
        assert len(scripts) == 2
        assert records(scripts[1]) == [
            {"name": "usage.changed", "payload": {"harnesses": ["zcode"], "revision": 7}}
        ]
        bridge._close()
        bus.publish(UsageChanged((Harness.CODEX,), 8))
        assert len(scripts) == 2


def test_not_ready_buffers_and_failed_sink_keeps_latest_for_retry(caplog: pytest.LogCaptureFixture) -> None:
    bus = InProcessEventBus()
    events = EventForwarder(bus)
    scripts: list[str] = []
    bus.publish(WalletChanged(('v"; window.danger(); //\u2028',), 1))

    def fail(script: str) -> None:
        bus.publish(WalletChanged(("latest",), 2))
        raise RuntimeError("window gone")

    events.attach(fail)
    assert "Bridge event delivery failed" in caplog.text
    assert events.snapshot()[0].model_dump()["payload"] == {"vendorIds": ["latest"], "revision": 2}
    events.attach(scripts.append)
    assert len(scripts) == 1
    bus.publish(WalletChanged(('v"; window.danger(); //\u2028',), 3))
    assert records(scripts[-1])[0]["payload"] == {
        "vendorIds": ['v"; window.danger(); //\u2028'],
        "revision": 3,
    }
    assert "\\u2028" in scripts[-1] and "\u2028" not in scripts[-1]
    events.close()


def test_collection_completion_uses_injected_committed_snapshot() -> None:
    bus = InProcessEventBus()
    events = EventForwarder(
        bus,
        collection_tick=lambda event: CollectionTickEvent(
            status={"codex": "watching"},
            nextPeriodicAt="2026-10-01T00:00:30.000Z",
            lastCompletedAt="2026-10-01T00:00:00.000Z",
            trigger=event.trigger,
        ),
    )
    scripts: list[str] = []
    events.attach(scripts.append)
    bus.publish(CollectionCompleted("manual", 3))
    pushed = records(scripts[0])
    assert pushed[0] == {"name": "sources.changed", "payload": {"revision": 3}}
    assert pushed[1]["name"] == "collection.tick"
    assert pushed[1]["payload"] == {
        "status": {"codex": "watching"},
        "nextPeriodicAt": "2026-10-01T00:00:30.000Z",
        "lastCompletedAt": "2026-10-01T00:00:00.000Z",
        "trigger": "manual",
    }
    events.close()


def test_event_bus_isolates_subscriber_failure_and_unsubscribe(caplog: pytest.LogCaptureFixture) -> None:
    bus = InProcessEventBus()
    received: list[object] = []

    def bad(event: object) -> None:
        raise RuntimeError("bad subscriber")

    remove = bus.subscribe(bad)
    stop = bus.subscribe(received.append)
    bus.publish(1)
    assert received == [1] and "Event subscriber failed" in caplog.text
    remove()
    remove()
    stop()
    bus.publish(2)
    assert received == [1]


def test_gui_dispatch_rechecks_visibility_and_coalesces_fifty_hidden_events() -> None:
    bus = InProcessEventBus()
    events = EventForwarder(bus)
    queued: list[object] = []
    scripts: list[str] = []
    events.attach(scripts.append, dispatch=queued.append)
    bus.publish(UsageChanged((Harness.CODEX,), 0))
    events.set_visible(False)
    for revision in range(1, 51):
        bus.publish(UsageChanged((Harness.CODEX,), revision))
    assert len(queued) == 1 and not scripts
    action = queued.pop()
    assert callable(action)
    action()
    assert not scripts
    events.set_visible(True)
    action = queued.pop()
    assert callable(action)
    action()
    assert len(scripts) == 1
    assert records(scripts[0]) == [
        {"name": "usage.changed", "payload": {"harnesses": ["codex"], "revision": 50}},
        {"name": "app.visibility", "payload": {"visible": True}},
    ]
    events.close()


def test_async_script_failure_retains_latest_payload_until_ready() -> None:
    bus = InProcessEventBus()
    events = EventForwarder(bus)
    future: Future[object] = Future()
    events.attach(lambda script: future)
    bus.publish(UsageChanged((Harness.CODEX,), 1))
    bus.publish(UsageChanged((Harness.DSH,), 2))
    future.set_exception(RuntimeError("receiver absent"))
    scripts: list[str] = []
    events.attach(scripts.append)
    assert records(scripts[0]) == [
        {"name": "usage.changed", "payload": {"harnesses": ["dsh"], "revision": 2}}
    ]
    events.close()
