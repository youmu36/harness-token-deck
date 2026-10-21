"""Lifecycle transitions, repeat inputs and startup activation merging."""

import pytest
from htd.adapters.desktop.lifecycle import DesktopLifecycle, Event, State


@pytest.mark.parametrize("event", ["activate", "quit", "webview_failed", "close_requested"])
def test_starting_control_events(event: Event) -> None:
    lifecycle = DesktopLifecycle()
    actions = lifecycle.handle(event)
    if event == "activate":
        for _ in range(10):
            assert lifecycle.handle("activate") == []
        assert lifecycle.handle("started") == ["restore"]
        lifecycle.handle("focused")
        assert lifecycle.handle("activate") == []
    else:
        assert actions == [{"quit": "shutdown", "webview_failed": "error", "close_requested": "hide"}[event]]
        assert lifecycle.handle(event) == []


def test_hide_confirmation_minimize_focus_restore_and_idempotence() -> None:
    lifecycle = DesktopLifecycle()
    assert lifecycle.handle("started") == ["visibility"]
    assert lifecycle.handle("started") == []
    lifecycle.handle("focused")
    assert lifecycle.handle("activate") == []
    lifecycle.handle("deactivated")
    assert lifecycle.handle("activate") == ["focus"]
    assert lifecycle.handle("activate") == []
    assert lifecycle.handle("minimize") == [] and lifecycle.state == "minimized"
    assert lifecycle.handle("minimize") == []
    lifecycle.handle("restored")
    assert lifecycle.state == "visible"
    lifecycle.handle("minimize")
    assert lifecycle.handle("activate") == ["restore"]
    assert lifecycle.handle("close_requested") == ["hide"]
    assert lifecycle.state == "visible"
    assert lifecycle.handle("close_requested") == []
    lifecycle.handle("hidden")
    assert lifecycle.state == "hidden"
    assert lifecycle.handle("close_requested") == []
    assert lifecycle.handle("activate") == ["restore"]
    assert lifecycle.handle("activate") == []


@pytest.mark.parametrize("state", ["starting", "visible", "hidden", "minimized", "failed"])
def test_quit_and_stopped_are_terminal_and_idempotent(state: State) -> None:
    lifecycle = DesktopLifecycle()
    lifecycle.state = state
    assert lifecycle.handle("quit") == ["shutdown"]
    assert lifecycle.state == "closing"
    for event in ("quit", "activate", "close_requested", "webview_failed", "retry"):
        assert lifecycle.handle(event) == []
    lifecycle.handle("stopped")
    assert lifecycle.state == "stopped" and lifecycle.handle("activate") == []


def test_hide_failure_exits_and_failure_retry_requires_explicit_input() -> None:
    lifecycle = DesktopLifecycle()
    lifecycle.handle("started")
    lifecycle.handle("close_requested")
    assert lifecycle.handle("hide_failed") == ["shutdown"]
    lifecycle = DesktopLifecycle()
    assert lifecycle.handle("webview_failed") == ["error"]
    assert lifecycle.handle("activate") == []
    assert lifecycle.handle("retry") == ["retry"]
    assert lifecycle.state == "starting"
    assert lifecycle.handle("started") == ["restore"]
