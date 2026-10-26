"""Dynamic WinForms/WebView2 interop kept out of the lifecycle and IPC layers."""

import importlib
import json
import logging
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from htd.adapters.desktop._win32 import foreground
from htd.adapters.desktop.tray import callback
from htd.adapters.desktop.webview2 import WebViewPower
from htd.adapters.sqlite.window_state import WindowGeometry

_LOG = logging.getLogger(__name__)
_HOST_ACTIONS = frozenset(
    {
        "htd.openLogDir",
        "htd.quit",
        "htd.ready",
        "htd.startupSettled",
        "htd.unready",
        "htd.preferencesFlushed",
        "htd.preferencesFlushFailed",
    }
)


def fits(geometry: WindowGeometry, screens: list[WindowGeometry]) -> bool:
    return (
        geometry.width >= 1024
        and geometry.height >= 680
        and any(
            geometry.x >= screen.x
            and geometry.y >= screen.y
            and geometry.x + geometry.width <= screen.x + screen.width
            and geometry.y + geometry.height <= screen.y + screen.height
            for screen in screens
        )
    )


class NativeWindow:
    def __init__(self, window: Any, tick: Callable[[], None], failed: Callable[[], None]) -> None:
        self.window = window
        self.form: Any = window.native
        self.forms: Any = importlib.import_module("System.Windows.Forms")
        self.system: Any = importlib.import_module("System")
        self._failed = failed
        self._scripts: list[tuple[Any, Future[object]]] = []
        self._core: Any = None
        self._init_failed = False
        self._closed = False
        self.host_action: Callable[[str], None] = lambda action: None
        self._host_core: Any = None
        self._script_filter_bound = False
        self.power = WebViewPower(self.form.webview)
        self._watch_initialization()
        self.timer: Any = self.forms.Timer()
        self.timer.Interval = 50
        self.timer.Tick += callback(tick)
        self.timer.Start()

    def dispatch(self, action: Callable[[], None]) -> None:
        self.form.BeginInvoke(self.system.Action(action))

    def pick_path(self, kind: str, initial: str | None) -> str | None:
        future: Future[str | None] = Future()

        def show() -> None:
            dialog = self.forms.OpenFileDialog() if kind == "file" else self.forms.FolderBrowserDialog()
            try:
                if initial:
                    path = Path(initial)
                    folder = path if path.is_dir() else path.parent
                    if folder.is_dir():
                        if kind == "file":
                            dialog.InitialDirectory = str(folder)
                            if path.is_file():
                                dialog.FileName = path.name
                        else:
                            dialog.SelectedPath = str(folder)
                if kind == "file":
                    dialog.Title = "选择文件"
                else:
                    # A redirected/offline Desktop is not a reliable picker root.
                    dialog.RootFolder = self.system.Environment.SpecialFolder.MyComputer
                    dialog.Description = "选择采集来源目录"
                if dialog.ShowDialog(self.form) == self.forms.DialogResult.OK:
                    future.set_result(str(dialog.FileName if kind == "file" else dialog.SelectedPath))
                else:
                    future.set_result(None)
            except Exception as error:
                future.set_exception(error)
            finally:
                dialog.Dispose()

        self.dispatch(show)
        return future.result(timeout=590)

    def place(self, geometry: WindowGeometry | None) -> None:
        if geometry is None:
            return
        scale = float(self.form.DeviceDpi) / 96
        screens: list[WindowGeometry] = []
        for screen in self.forms.Screen.AllScreens:
            area = screen.WorkingArea
            screens.append(
                WindowGeometry(
                    int(area.X / scale),
                    int(area.Y / scale),
                    int(area.Width / scale),
                    int(area.Height / scale),
                )
            )
        if fits(geometry, screens):
            drawing: Any = importlib.import_module("System.Drawing")
            self.form.StartPosition = self.forms.FormStartPosition.Manual
            self.form.Bounds = drawing.Rectangle(
                int(geometry.x * scale),
                int(geometry.y * scale),
                int(geometry.width * scale),
                int(geometry.height * scale),
            )

    def geometry(self) -> WindowGeometry:
        area = (
            self.form.Bounds
            if self.form.WindowState == self.forms.FormWindowState.Normal
            else self.form.RestoreBounds
        )
        scale = float(self.form.DeviceDpi) / 96
        return WindowGeometry(
            int(area.X / scale), int(area.Y / scale), int(area.Width / scale), int(area.Height / scale)
        )

    def focus(self) -> None:
        self.form.Activate()
        self.form.BringToFront()
        if not foreground(int(self.form.Handle.ToInt64())):
            _LOG.info("系统未授予窗口前台激活权限")
        self.form.webview.Focus()

    def restore(self) -> None:
        self.timer.Interval = 50
        self.form.Show()
        self.form.WindowState = self.forms.FormWindowState.Normal
        self.power.restore()
        self.focus()

    def hide(self) -> None:
        self.power.hide()
        self.form.Hide()
        if self.form.Visible:
            raise RuntimeError("窗口隐藏失败")
        self.timer.Interval = 500

    def _watch_initialization(self) -> None:
        def initialized(sender: Any, args: Any) -> None:
            if not args.IsSuccess:
                self._init_failed = True
                _LOG.error("WebView2 初始化失败: %s", args.InitializationException)
            else:
                self._bind_host_controls()

        self.form.webview.CoreWebView2InitializationCompleted += initialized

    def _bind_host_controls(self) -> None:
        core = self.form.webview.CoreWebView2
        if core is None or core == self._host_core:
            return
        self._host_core = core
        if not self._script_filter_bound:
            # pywebview expects three-element API messages and tries to unpack our strings.
            self.form.webview.WebMessageReceived -= self.form.browser.on_script_notify
            self.form.webview.WebMessageReceived += self._script_message
            self._script_filter_bound = True
        core.WebMessageReceived += self._host_message

    def _script_message(self, sender: Any, args: Any) -> None:
        try:
            message = json.loads(str(args.WebMessageAsJson))
        except (ValueError, TypeError):
            message = None
        if isinstance(message, str) and message in _HOST_ACTIONS:
            return
        self.form.browser.on_script_notify(sender, args)

    def _host_message(self, sender: Any, args: Any) -> None:
        try:
            # Match the loaded local document; external frames cannot request host actions.
            if str(args.Source).split("#")[0] != str(self.window.real_url).split("#")[0]:
                return
            message = json.loads(str(args.WebMessageAsJson))
            if isinstance(message, str) and message in _HOST_ACTIONS:
                self.host_action(message)
        except Exception:
            _LOG.warning("忽略无效的桌面控制消息", exc_info=True)

    def poll(self) -> None:
        self.power.poll()
        core = self.form.webview.CoreWebView2
        if core is not None and core != self._core:
            self._core = core
            self.power.configure()
            core.ProcessFailed += callback(self._failed)
            self._bind_host_controls()
        if self._init_failed:
            self._init_failed = False
            self._failed()
        for task, future in self._scripts[:]:
            if task.IsCompleted:
                self._scripts.remove((task, future))
                try:
                    if task.Result != "true":
                        raise RuntimeError("前端事件接收器尚未就绪")
                    future.set_result(None)
                except Exception as error:
                    future.set_exception(error)

    def evaluate(self, script: str) -> Future[object]:
        checked = (
            "(() => { if (typeof window.__htd?.emit !== 'function') return false; "
            + script
            + "; return true; })()"
        )
        future: Future[object] = Future()
        task = self.form.webview.CoreWebView2.ExecuteScriptAsync(checked)
        self._scripts.append((task, future))
        return future

    def flush_preferences(self) -> None:
        self.form.webview.CoreWebView2.ExecuteScriptAsync(
            "Promise.resolve().then(() => window.__htdFlushPreferences?.()).then("
            "() => chrome.webview.postMessage('htd.preferencesFlushed'),"
            "() => chrome.webview.postMessage('htd.preferencesFlushFailed'))"
        )

    def receiver_ready(self) -> Future[object]:
        return self.evaluate("void 0")

    def error_dialog(self, directory: Path) -> bool:
        drawing: Any = importlib.import_module("System.Drawing")
        dialog: Any = self.forms.Form()
        dialog.Text = "Harness Token Deck"
        dialog.ClientSize = drawing.Size(560, 175)
        dialog.FormBorderStyle = self.forms.FormBorderStyle.FixedDialog
        dialog.StartPosition = self.forms.FormStartPosition.CenterParent
        dialog.MaximizeBox = dialog.MinimizeBox = False
        dialog.ShowInTaskbar = False
        label: Any = self.forms.Label()
        label.Text = f"WebView2 初始化失败、超时或进程已停止。\n日志: {directory / 'logs/htd.log'}"
        label.Location = drawing.Point(16, 16)
        label.Size = drawing.Size(528, 105)
        dialog.Controls.Add(label)
        for text, result, x in (
            ("重试", self.forms.DialogResult.Retry, 352),
            ("退出", self.forms.DialogResult.Cancel, 452),
        ):
            button: Any = self.forms.Button()
            button.Text = text
            button.Location = drawing.Point(x, 132)
            button.Size = drawing.Size(90, 28)
            button.DialogResult = result
            dialog.Controls.Add(button)
            if text == "退出":
                dialog.CancelButton = button
            else:
                dialog.AcceptButton = button
        self.timer.Stop()
        try:
            return dialog.ShowDialog(self.form) == self.forms.DialogResult.Retry
        finally:
            dialog.Dispose()
            self.timer.Start()

    def shutdown_dialog(self, directory: Path) -> None:
        self.timer.Stop()
        try:
            self.forms.MessageBox.Show(
                self.form,
                f"当前事务仍在结束，将继续等待。\n日志: {directory / 'logs/htd.log'}",
                "Harness Token Deck",
                self.forms.MessageBoxButtons.OK,
                self.forms.MessageBoxIcon.Error,
            )
        finally:
            self.timer.Start()

    def retry(self) -> None:
        self.power.close()
        for _, future in self._scripts:
            if not future.done():
                future.set_exception(RuntimeError("WebView2 正在重新初始化"))
        self._scripts.clear()
        edge: Any = importlib.import_module("webview.platforms.edgechromium")
        old = self.form.webview
        cache = self.form.browser.user_data_folder
        self.form.Controls.Remove(old)
        old.Dispose()
        self.window.events.loaded.clear()
        self.window.events._pywebviewready.clear()
        self.form.browser = edge.EdgeChrome(self.form, self.window, cache)
        self.form.webview = self.form.browser.webview
        self.power = WebViewPower(self.form.webview)
        self._core = None
        self._watch_initialization()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.power.close()
        self.timer.Stop()
        self.timer.Dispose()
        for _, future in self._scripts:
            if not future.done():
                future.set_exception(RuntimeError("窗口正在关闭"))
        self._scripts.clear()
