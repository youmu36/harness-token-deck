"""NotifyIcon ownership and callbacks; construction and disposal run on the GUI thread."""

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any


def callback(action: Callable[[], None]) -> Callable[..., None]:
    def invoke(*_: Any) -> None:
        action()

    return invoke


def icon_path() -> Path:
    return Path(__file__).with_name("resources") / "app.ico"


class Tray:
    def __init__(
        self,
        *,
        activate: Callable[[], None],
        scan: Callable[[], None],
        pause: Callable[[bool], None],
        quit_app: Callable[[], None],
        refresh: Callable[[], None],
    ) -> None:
        forms: Any = importlib.import_module("System.Windows.Forms")
        drawing: Any = importlib.import_module("System.Drawing")
        self._icon: Any = drawing.Icon(str(icon_path()))
        self._menu: Any = forms.ContextMenuStrip()
        self._tray: Any = forms.NotifyIcon()
        self._disposed = False
        self._tray.Icon = self._icon
        self._tray.Text = "Harness Token Deck"
        self._tray.ContextMenuStrip = self._menu
        self._menu.Opening += callback(refresh)
        self._menu.Items.Add("打开窗口").Click += callback(activate)
        self._scan: Any = self._menu.Items.Add("立即更新")
        self._scan.Click += callback(scan)
        self._pause: Any = self._menu.Items.Add("暂停采集")
        self._pause.Click += callback(lambda: pause(not bool(self._pause.Checked)))
        self._menu.Items.Add(forms.ToolStripSeparator())
        self._menu.Items.Add("退出").Click += callback(quit_app)
        self._tray.DoubleClick += callback(activate)
        self._tray.Visible = True

    def open(self) -> None:
        self._menu.Items[0].PerformClick()

    def update(self, *, paused: bool, busy: bool = False) -> None:
        self._pause.Checked = paused
        self._pause.Enabled = not busy
        self._scan.Enabled = not paused and not busy

    def close(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._tray.Visible = False
        self._tray.Dispose()
        self._menu.Dispose()
        self._icon.Dispose()
