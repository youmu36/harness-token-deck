"""Real host with test-owned observations; no extra production pipe or JavaScript methods."""

# pyright: reportPrivateUsage=false

import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

import htd.__main__ as entry
from htd.adapters.desktop.native import NativeWindow
from htd.adapters.desktop.single_instance import SingleInstance
from htd.adapters.desktop.tray import Tray
from htd.application.ports import UsageChanged
from htd.bootstrap import Application
from htd.domain.usage.model import Harness


def main() -> int:
    directory = Path(os.environ["HTD_DATA_DIR"])
    output = directory / "observations.jsonl"
    lock = threading.Lock()
    application: Application | None = None

    def record(kind: str, **values: object) -> None:
        with lock, output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, **values}, ensure_ascii=True) + "\n")

    def instance(root: Path) -> SingleInstance:
        return SingleInstance(root, name=sys.argv[1], pipe_name=sys.argv[1])

    def build(root: Path, **kwargs: Any) -> Application:
        nonlocal application
        application = Application(root, **kwargs)
        return application

    original_hide = NativeWindow.hide
    original_native_init = NativeWindow.__init__
    original_poll = NativeWindow.poll
    original_retry = NativeWindow.retry
    original_restore = NativeWindow.restore
    original_evaluate = NativeWindow.evaluate
    original_tray_init = Tray.__init__
    original_tray_close = Tray.close
    original_tray_update = Tray.update
    tray_instance: Tray | None = None
    event_read: Any = None
    shell_read: Any = None

    def native_init(native: NativeWindow, *args: Any, **kwargs: Any) -> None:
        original_native_init(native, *args, **kwargs)
        record("window", handle=int(native.form.Handle.ToInt64()))

    def poll(native: NativeWindow) -> None:
        nonlocal event_read, shell_read
        command_file = directory / "native-command.txt"
        if command_file.is_file():
            command = command_file.read_text(encoding="utf-8")
            command_file.unlink()
            if command == "retry":
                native._failed()
            elif command == "events":
                event_read = native.form.webview.CoreWebView2.ExecuteScriptAsync(
                    "window.__desktopObserved || []"
                )
            elif command == "shell":
                shell_read = native.form.webview.CoreWebView2.ExecuteScriptAsync(
                    "({title:document.querySelector('h1')?.textContent, "
                    "navigation:document.querySelectorAll('nav a').length, "
                    "environment:document.querySelector('.environment')?.textContent})"
                )
            elif command == "navigate-settings":
                native.form.webview.CoreWebView2.ExecuteScriptAsync(
                    "document.querySelector('nav a[aria-label=\"设置\"]')?.click()"
                )
            elif command == "navigate-wallet":
                native.form.webview.CoreWebView2.ExecuteScriptAsync(
                    "document.querySelector('nav a[aria-label=\"供应商与钱包\"]')?.click()"
                )
            elif command == "quit-host":
                native.form.webview.CoreWebView2.ExecuteScriptAsync(
                    "window.chrome.webview.postMessage('htd.quit')"
                )
            elif command == "preferences":
                assert application is not None
                result = application.bridge.call("preferences.get", {})
                record("preferences", snapshot=result)
            elif tray_instance is not None:
                tray_instance._menu.Items[1 if command == "scan" else 2].PerformClick()
        original_poll(native)
        if event_read is not None and event_read.IsCompleted:
            record("received", events=json.loads(event_read.Result))
            event_read = None
        if shell_read is not None and shell_read.IsCompleted:
            record("shell", view=json.loads(shell_read.Result))
            shell_read = None

    def error_dialog(native: NativeWindow, directory: Path) -> bool:
        record("dialog", choice="retry")
        return True

    def retry(native: NativeWindow) -> None:
        original_retry(native)
        record("retry")

    def restore(native: NativeWindow) -> None:
        original_restore(native)
        api: Any = ctypes.WinDLL("user32", use_last_error=True)
        api.GetForegroundWindow.restype = wintypes.HWND
        record(
            "restored",
            handle=int(native.form.Handle.ToInt64()),
            foreground=api.GetForegroundWindow(),
            focused=bool(native.form.ContainsFocus),
        )

    def hidden(native: NativeWindow) -> None:
        original_hide(native)
        assert application is not None
        for revision in range(1, 51):
            application.bus.publish(UsageChanged((Harness.CODEX,), revision))
        record("hidden", visible=bool(native.form.Visible))

    def evaluate(native: NativeWindow, script: str) -> Any:
        if "const events = " in script:
            payload = json.loads(script.split("const events = ", 1)[1].split("; for (", 1)[0])
            record("evaluate", visible=bool(native.form.Visible), events=payload)
            return original_evaluate(native, script)
        observed = (
            "if (!window.__desktopObserved) { window.__desktopObserved = []; "
            "for (const name of ['usage.changed', 'app.visibility']) "
            "window.addEventListener(name, event => "
            "window.__desktopObserved.push({name, payload:event.detail})); }"
        )
        result = original_evaluate(native, observed if script == "void 0" else script)
        if script == "void 0":

            def ready(future: Any) -> None:
                if future.exception() is None:
                    settings = native.form.webview.CoreWebView2.Settings
                    record("ready", devtools=bool(settings.AreDevToolsEnabled))

            result.add_done_callback(ready)
        return result

    def tray_init(tray: Tray, **kwargs: Any) -> None:
        nonlocal tray_instance
        original_tray_init(tray, **kwargs)
        tray_instance = tray
        record("tray", visible=bool(tray._tray.Visible))

    def tray_close(tray: Tray) -> None:
        disposed = tray._disposed
        original_tray_close(tray)
        if not disposed:
            record("tray-disposed", disposed=tray._disposed, visible=bool(tray._tray.Visible))

    def tray_update(tray: Tray, **kwargs: Any) -> None:
        original_tray_update(tray, **kwargs)
        record("tray-state", paused=bool(tray._pause.Checked), busy=not bool(tray._pause.Enabled))

    patched_entry: Any = entry
    patched_entry.SingleInstance = instance
    patched_entry.Application = build
    patched_native: Any = NativeWindow
    patched_native.__init__ = native_init
    patched_native.poll = poll
    patched_native.retry = retry
    patched_native.restore = restore
    patched_native.error_dialog = error_dialog
    patched_native.hide = hidden
    patched_native.evaluate = evaluate
    patched_tray: Any = Tray
    patched_tray.__init__ = tray_init
    patched_tray.close = tray_close
    patched_tray.update = tray_update
    return entry.main([])


if __name__ == "__main__":
    raise SystemExit(main())
