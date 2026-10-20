"""Controller failure and timeout paths without native dialogs or real GUI waits."""

# pyright: reportPrivateUsage=false

import json
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from htu.adapters.desktop.controller import DesktopController, page_from_url
from htu.adapters.desktop.native import NativeWindow, fits
from htu.adapters.sqlite.window_state import WindowGeometry


def controller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[DesktopController, Any]:
    native: Any = MagicMock()
    native.form.webview.CoreWebView2 = None
    native.geometry.return_value = WindowGeometry(10, 20, 1280, 820)
    native.error_dialog.return_value = False
    native.receiver_ready.return_value = Future()
    native.forms.FormWindowState.Normal = "normal"
    native.form.WindowState = "normal"

    def create(*args: object, **kwargs: object) -> Any:
        return native

    monkeypatch.setattr("htu.adapters.desktop.controller.NativeWindow", create)
    monkeypatch.setattr("htu.adapters.desktop.controller.Tray", MagicMock())
    window: Any = MagicMock()
    instance: Any = MagicMock()
    instance.next_command.return_value = None
    store: Any = MagicMock()
    store.load.return_value = None
    bridge: Any = MagicMock()
    bridge.call.return_value = {"ok": True, "data": {"paused": False}}
    result = DesktopController(window, bridge, instance, tmp_path, shutdown=MagicMock(), window_store=store)
    result.setup()
    return result, native


def test_close_is_cancelled_only_after_hide_then_quit_waits_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host, native = controller(monkeypatch, tmp_path)
    try:
        host.handle("started")
        assert host.closing() is False
        assert host.lifecycle.state == "hidden"
        native.hide.assert_called_once()
        host.bridge._set_visible.assert_called_with(False)  # type: ignore[attr-defined]
        host.handle("activate")
        native.restore.assert_called_once()
        host.handle("quit")
        assert host._shutdown is not None
        host._shutdown.result(timeout=2)
        host.tick()
        assert host.lifecycle.state == "stopped"
        assert host.closing() is True
        assert host.tray is not None
        host.instance.close.assert_called_once()  # type: ignore[attr-defined]
        native.form.Close.assert_called_once()
    finally:
        host.close()


def test_failed_hide_starts_real_exit_cleanup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    host, native = controller(monkeypatch, tmp_path)
    try:
        native.hide.side_effect = RuntimeError("synthetic hide failure")
        host.handle("started")
        assert host.closing() is False
        assert host.lifecycle.state == "closing"
        assert host._shutdown is not None
        host._shutdown.result(timeout=2)
        host.tick()
        assert host.lifecycle.state == "stopped" and host.closing()
    finally:
        host.close()


def test_native_hide_hides_the_webview_before_its_parent_window() -> None:
    native = NativeWindow.__new__(NativeWindow)
    native.form = MagicMock()
    native.form.Visible = True
    native.power = MagicMock()
    native.timer = MagicMock()
    events: list[str] = []

    def hide() -> None:
        native.form.Visible = False
        events.append("hide-window")

    native.power.hide.side_effect = lambda: events.append("hide-webview")
    native.form.Hide.side_effect = hide
    native.hide()
    assert events == ["hide-webview", "hide-window"]
    assert native.timer.Interval == 500


def test_close_waits_for_preferences_and_unchecked_tray_setting_exits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host, native = controller(monkeypatch, tmp_path)
    try:
        host.handle("started")
        host.host_action("htu.ready")
        host.bridge.call.return_value = {"ok": True, "data": {"preferences": {"closeToTray": False}}}  # type: ignore[attr-defined]
        assert host.closing() is False
        native.flush_preferences.assert_called_once()
        assert host.lifecycle.state == "visible"
        host.host_action("htu.preferencesFlushed")
        assert host.lifecycle.state == "closing"
        native.hide.assert_not_called()
        assert host._shutdown is not None
        host._shutdown.result(timeout=2)
        host.tick()
        assert host.lifecycle.state == "stopped"
    finally:
        host.close()


def test_failed_preference_flush_preserves_the_visible_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host, native = controller(monkeypatch, tmp_path)
    try:
        host.handle("started")
        host.host_action("htu.ready")
        assert host.closing() is False
        host.host_action("htu.preferencesFlushFailed")
        assert host.lifecycle.state == "visible"
        native.hide.assert_not_called()
    finally:
        host.close()


@pytest.mark.parametrize("retry", [True, False])
def test_initialization_deadline_uses_native_retry_or_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    retry: bool,
) -> None:
    host, native = controller(monkeypatch, tmp_path)
    try:
        assert host._deadline > 0
        native.error_dialog.return_value = retry
        monkeypatch.setattr("htu.adapters.desktop.controller.time.monotonic", lambda: host._deadline + 1)
        host.tick()
        native.error_dialog.assert_called_once()
        if retry:
            native.retry.assert_called_once()
            assert host.lifecycle.state == "starting"
        else:
            assert host.lifecycle.state == "closing" and host._shutdown is not None
            host._shutdown.result(timeout=2)
            host.tick()
            assert host.lifecycle.state == "stopped"
    finally:
        host.close()


def test_out_of_bounds_or_undersized_geometry_resets_to_default() -> None:
    screens = [WindowGeometry(-1920, 0, 1920, 1080), WindowGeometry(0, 0, 1920, 1040)]
    assert fits(WindowGeometry(-1800, 50, 1280, 820), screens)
    assert not fits(WindowGeometry(1900, 0, 1280, 820), screens)
    assert not fits(WindowGeometry(0, 0, 1023, 680), screens)


def test_native_hide_persists_route_only_after_frontend_handshake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host, native = controller(monkeypatch, tmp_path)
    try:
        native.form.webview.CoreWebView2 = MagicMock(
            Source="http://127.0.0.1:1/index.html#/settings?theme=dark"
        )
        host.bridge.call.reset_mock()  # type: ignore[attr-defined]
        assert host.capture_page() is None
        host.host_action("htu.ready")
        assert host.capture_page() == "settings"
        responses: list[dict[str, Any]] = [
            {"ok": True, "data": {"preferences": {"lastPage": "codex"}, "revision": 4}},
            {"ok": False, "error": {"code": "PREFERENCES_REVISION_CONFLICT"}},
            {"ok": True, "data": {"preferences": {"lastPage": "codex"}, "revision": 5}},
            {"ok": True, "data": {}},
        ]
        host.bridge.call.side_effect = responses  # type: ignore[attr-defined]
        host.save_page(host.capture_page())
        bridge: Any = host.bridge
        writes: list[Any] = [
            call for call in bridge.call.call_args_list if call.args[0] == "preferences.update"
        ]
        assert len(writes) == 2
        assert writes[0].args[1]["expectedRevision"] == 4
        assert writes[1].args[1]["expectedRevision"] == 5
        assert writes[1].args[1]["patch"] == {"lastPage": "settings", "lastRoute": "/settings"}
        assert writes[0].args[1]["operationId"] != writes[1].args[1]["operationId"]
    finally:
        host.close()


@pytest.mark.parametrize(
    "path,page",
    [
        ("/models", "pricing"),
        ("/wallet/vendor?theme=light", "wallet"),
        ("/harness/dsh", "dsh"),
        ("/harness/claude", "claude"),
        ("/unknown", None),
        ("/wallet/one/two", None),
    ],
)
def test_native_route_persistence_maps_only_allowed_pages(path: str, page: str | None) -> None:
    assert page_from_url("http://127.0.0.1/index.html#" + path) == page


def test_host_controls_accept_only_fixed_messages_from_the_loaded_local_page() -> None:
    native: Any = object.__new__(NativeWindow)
    native.window = MagicMock(real_url="http://127.0.0.1:1/index.html#?theme=dark")
    native.host_action = MagicMock()
    args: Any = MagicMock(Source="https://example.com/")
    args.WebMessageAsJson = json.dumps("htu.quit")
    native._host_message(None, args)
    native.host_action.assert_not_called()
    args.Source = "http://127.0.0.1:1/index.html#/settings"
    for action in ("htu.ready", "htu.unready", "htu.openLogDir", "htu.quit"):
        args.WebMessageAsJson = json.dumps(action)
        native._host_message(None, args)
    assert native.host_action.call_count == 4
    args.WebMessageAsJson = json.dumps("open:C:/arbitrary")
    native._host_message(None, args)
    assert native.host_action.call_count == 4
