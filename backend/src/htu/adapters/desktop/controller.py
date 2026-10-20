"""Serialize lifecycle actions on WinForms; storage shutdown runs without blocking the GUI."""

# pyright: reportPrivateUsage=false
# Bridge hooks and pywebview's ready event are intentionally hidden from JavaScript.

import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import JsonValue

from htu.adapters.desktop.lifecycle import DesktopLifecycle, Event
from htu.adapters.desktop.native import NativeWindow
from htu.adapters.desktop.single_instance import SingleInstance
from htu.adapters.desktop.tray import Tray, callback
from htu.adapters.sqlite.window_state import WindowGeometry
from htu.ipc.bridge import Bridge

_LOG = logging.getLogger(__name__)


def page_from_url(url: str) -> str | None:
    path = urlsplit(url).fragment.split("?")[0]
    pages = {
        "/overview": "overview",
        "/harness/claude": "claude",
        "/harness/codex": "codex",
        "/harness/zcode": "zcode",
        "/harness/dsh": "dsh",
        "/wallet": "wallet",
        "/pricing": "pricing",
        "/pricing/versions": "pricing",
        "/models": "pricing",
        "/settings": "settings",
        "/settings/background": "settings",
    }
    return "wallet" if path.startswith("/wallet/") and path.count("/") == 2 else pages.get(path)


class WindowStore(Protocol):
    def load(self) -> WindowGeometry | None: ...


class DesktopController:
    def __init__(
        self,
        window: Any,
        bridge: Bridge,
        instance: SingleInstance,
        directory: Path,
        *,
        shutdown: Callable[[WindowGeometry | None], None],
        window_store: WindowStore,
        release_memory: Callable[[], None] = lambda: None,
    ) -> None:
        self.window, self.bridge, self.instance, self.directory = window, bridge, instance, directory
        self.shutdown, self.window_store = shutdown, window_store
        self.release_memory = release_memory
        self.lifecycle = DesktopLifecycle()
        self.native: NativeWindow | None = None
        self.tray: Tray | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="htu-desktop-work")
        self._shutdown_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="htu-desktop-exit")
        self._shutdown: Future[None] | None = None
        self._menu_work: Future[dict[str, JsonValue]] | None = None
        self._paused = False
        self._receiver: Future[object] | None = None
        self._receiver_due = 0.0
        self._attached = False
        self._core_ready = False
        self._frontend_ready = False
        self._flush_event: Event | None = None
        self._flush_approved = False
        self._last_page: str | None = None
        self._last_route: str | None = None
        self._deadline = time.monotonic() + 15
        self._allow_close = False
        self._stopped = threading.Event()
        self._geometry = window_store.load()
        window.events.before_show += self.setup
        window.events.closing += self.closing
        window.events.closed += self._stopped.set
        window.events.before_load += self.unready

    def setup(self) -> None:
        self.native = NativeWindow(self.window, self.tick, lambda: self.handle("webview_failed"))
        self.native.host_action = self.host_action
        self.bridge._set_system_actions(self.native.pick_path, self.open_path)
        self.native.place(self._geometry)
        self.native.form.Resize += self.resized
        self.native.form.Activated += callback(lambda: self.handle("focused"))
        self.native.form.Deactivate += callback(lambda: self.handle("deactivated"))
        self.native.form.FormClosed += callback(self.closed)
        self.tray = Tray(
            activate=lambda: self.handle("activate"),
            scan=lambda: self.command("collection.scanNow"),
            pause=lambda value: self.command("collection.setPaused", paused=value),
            quit_app=lambda: self.handle("quit"),
            refresh=self.refresh_menu,
        )
        self._menu_work = self._pool.submit(self.bridge.call, "collection.status", {})

    def unready(self) -> None:
        self.bridge._unready()
        self._attached = False
        self._frontend_ready = False

    def host_action(self, action: str) -> None:
        if action in {"htu.preferencesFlushed", "htu.preferencesFlushFailed"}:
            event, self._flush_event = self._flush_event, None
            if event is not None and action == "htu.preferencesFlushed":
                self._flush_approved = True
                try:
                    self.handle(event)
                finally:
                    self._flush_approved = False
            return
        if action in {"htu.ready", "htu.unready"}:
            self._frontend_ready = action == "htu.ready"
            if self._frontend_ready and self.native is not None:
                self.native.power.startup_ready()
        elif action == "htu.openLogDir":
            try:
                os.startfile(self.directory / "logs")
            except OSError:
                _LOG.exception("打开日志目录失败")
        elif action == "htu.quit":
            self.handle("quit")

    def open_path(self, kind: str) -> None:
        target = self.directory if kind == "dataDir" else self.directory / "logs"
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def capture_page(self) -> str | None:
        if self._frontend_ready and self.native is not None:
            core = self.native.form.webview.CoreWebView2
            if core is None:
                return self._last_page
            url = str(core.Source)
            self._last_page = page_from_url(url)
            self._last_route = urlsplit(url).fragment.split("?")[0] if self._last_page else None
        return self._last_page

    def save_page(self, page: str | None) -> None:
        if page is None:
            return
        # A native hide/quit must persist without depending on suspended JavaScript.
        for _ in range(2):
            snapshot = self.bridge.call("preferences.get", {})
            data = snapshot.get("data")
            if not snapshot.get("ok") or not isinstance(data, dict):
                _LOG.warning("无法读取上次页面偏好: %s", snapshot)
                return
            preferences = data.get("preferences")
            patch = {"lastPage": page}
            if self._last_route:
                patch["lastRoute"] = self._last_route
            if isinstance(preferences, dict) and all(preferences.get(k) == v for k, v in patch.items()):
                return
            result = self.bridge.call(
                "preferences.update",
                {
                    "operationId": str(uuid4()),
                    "expectedRevision": data.get("revision"),
                    "patch": patch,
                },
            )
            if result.get("ok"):
                return
            error = result.get("error")
            if not isinstance(error, dict) or error.get("code") != "PREFERENCES_REVISION_CONFLICT":
                _LOG.warning("保存上次页面失败: %s", result)
                return
        _LOG.warning("保存上次页面时偏好持续变化")

    def refresh_menu(self) -> None:
        if self._menu_work is None and self.lifecycle.state not in {"closing", "stopped", "failed"}:
            self._menu_work = self._pool.submit(self.bridge.call, "collection.status", {})
            if self.tray is not None:
                self.tray.update(paused=self._paused, busy=True)

    def command(self, command: str, **payload: object) -> None:
        if self.lifecycle.state in {"closing", "stopped", "failed"} or self._menu_work is not None:
            return
        payload["operationId"] = str(uuid4())
        self._menu_work = self._pool.submit(self.bridge.call, command, payload)
        if self.tray is not None:
            self.tray.update(paused=self._paused, busy=True)

    def evaluate(self, script: str) -> Future[object]:
        if self.native is None:
            raise RuntimeError("桌面窗口尚未初始化")
        future = self.native.evaluate(script)

        def completed(future: Future[object]) -> None:
            if future.exception() is not None:
                self._attached = False
                self._receiver_due = time.monotonic() + 0.25

        future.add_done_callback(completed)
        return future

    def handle(self, event: Event) -> None:
        native = self.native
        if native is None:
            return
        if (
            event in {"close_requested", "quit"}
            and self._frontend_ready
            and self.lifecycle.state in {"visible", "minimized"}
            and not self._flush_approved
        ):
            if self._flush_event is None:
                self._flush_event = event
                try:
                    native.flush_preferences()
                    return
                except Exception:
                    self._flush_event = None
                    _LOG.exception("无法请求界面偏好保存，继续原生清理")
            else:
                if event == "quit":
                    self._flush_event = event
                return
        for action in self.lifecycle.handle(event):
            if action in {"restore", "focus"}:
                if action == "restore":
                    native.restore()
                else:
                    native.focus()
                self.bridge._set_visible(True)
            elif action == "visibility":
                self.bridge._set_visible(True)
            elif action == "hide":
                try:
                    # Block queued deliveries before hiding; rollback is an exit path on failure.
                    self.bridge._set_visible(False)
                    native.hide()
                    self._pool.submit(self._hidden, self.capture_page())
                    self._geometry = native.geometry()
                    self.lifecycle.handle("hidden")
                except Exception:
                    _LOG.exception("窗口隐藏失败，退出应用")
                    self.handle("hide_failed")
            elif action == "shutdown":
                self.capture_page()
                self.bridge._unready()
                try:
                    self._geometry = native.geometry()
                except Exception:
                    _LOG.exception("读取窗口尺寸失败，继续退出")
                self._shutdown = self._shutdown_pool.submit(self._cleanup)
            elif action == "error":
                self.unready()
                self.handle("retry" if native.error_dialog(self.directory) else "quit")
            elif action == "retry":
                self._deadline = time.monotonic() + 15
                self._core_ready = False
                self._receiver = None
                native.retry()

    def _hidden(self, page: str | None) -> None:
        self.save_page(page)
        self.release_memory()

    def _cleanup(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
        self.save_page(self._last_page)
        self.shutdown(self._geometry)

    def closed(self) -> None:
        if self.native is not None:
            self.native.close()
        if self.tray is not None:
            self.tray.close()
        self._stopped.set()

    def closing(self) -> bool:
        if self._allow_close:
            return True
        snapshot = self.bridge.call("preferences.get", {})
        data = snapshot.get("data")
        preferences = data.get("preferences") if isinstance(data, dict) else None
        close_to_tray = preferences.get("closeToTray", True) if isinstance(preferences, dict) else True
        self.handle("close_requested" if close_to_tray else "quit")
        if self._flush_event is not None:
            return False
        # No close cancellation until the actual Hide has succeeded.
        return self.lifecycle.state not in {"hidden", "closing"}

    def resized(self, *_: Any) -> None:
        if self.native is None:
            return
        state = self.native.form.WindowState
        if state == self.native.forms.FormWindowState.Minimized:
            self.handle("minimize")
        elif state == self.native.forms.FormWindowState.Normal:
            self.handle("restored")

    def tick(self) -> None:
        native = self.native
        if native is None:
            return
        try:
            native.poll()
            if self._shutdown is not None and self._shutdown.done():
                try:
                    self._shutdown.result()
                except Exception:
                    _LOG.exception("退出清理失败，保留资源等待重试")
                    native.shutdown_dialog(self.directory)
                    # Keep waiting for storage; never abandon an active transaction.
                    self._shutdown = self._shutdown_pool.submit(self._cleanup)
                    return
                self.bridge._close()
                if self.tray is not None:
                    self.tray.close()
                    _LOG.info("托盘图标已释放")
                self.instance.close()
                self._allow_close = True
                self.lifecycle.handle("stopped")
                native.form.Close()
                return
            command = self.instance.next_command(timeout=0)
            if command == "test.trayOpen" and self.tray is not None:
                self.tray.open()
            elif command == "activate":
                self.handle("activate")
            elif command == "quit":
                self.handle("quit")
            if self.lifecycle.state in {"closing", "stopped", "failed"}:
                return
            if self._menu_work is not None and self._menu_work.done():
                result = self._menu_work.result()
                self._menu_work = None
                data = result.get("data")
                if result.get("ok") and isinstance(data, dict):
                    self._paused = data.get("paused", self._paused) is True
                    if self.tray is not None:
                        self.tray.update(paused=self._paused)
                else:
                    _LOG.warning("托盘命令失败: %s", result)
                self._menu_work = (
                    self._pool.submit(self.bridge.call, "collection.status", {})
                    if not isinstance(data, dict) or "paused" not in data
                    else None
                )
            if not self._core_ready:
                if native.form.webview.CoreWebView2 is not None:
                    self._core_ready = True
                    self.handle("started")
                elif time.monotonic() >= self._deadline:
                    self.handle("webview_failed")
            visible = self.lifecycle.state in {"visible", "minimized"}
            if self._core_ready and visible and not self._attached:
                if self._receiver is not None and self._receiver.done():
                    try:
                        self._receiver.result()
                    except Exception:
                        self._receiver_due = time.monotonic() + 0.25
                    else:
                        self._attached = True
                        self.bridge._ready(self.evaluate, dispatch=native.dispatch)
                    self._receiver = None
                elif self._receiver is None and time.monotonic() >= self._receiver_due:
                    self._receiver = native.receiver_ready()
        except Exception:
            _LOG.exception("桌面生命周期操作失败")
            self.handle("webview_failed")

    def close(self) -> None:
        self.bridge._close()
        self._shutdown_pool.shutdown(wait=True, cancel_futures=True)
        self._pool.shutdown(wait=True, cancel_futures=True)
