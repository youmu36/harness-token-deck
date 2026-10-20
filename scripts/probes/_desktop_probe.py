"""Bounded native probes; .NET objects intentionally use dynamic typing."""

import argparse
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from _static_server import StaticOnlyServer


class Bridge:
    def call(self, command: str, payload: dict[str, object]) -> dict[str, object]:
        if command != "ping" or payload:
            return {"ok": False}
        return {"ok": True, "data": "pong"}


def run(mode: str) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(f".verify/probe-{mode}.json"))
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report: dict[str, Any] = {"mode": mode, "passed": False, "frozen": bool(getattr(sys, "frozen", False))}
    frozen_root = getattr(sys, "_MEIPASS", None)
    root = Path(frozen_root) if frozen_root else Path(__file__).resolve().parents[2]
    html = root / ("web/index.html" if frozen_root else "backend/src/htu/web/index.html")
    if not html.is_file():
        raise FileNotFoundError(f"Build frontend first: {html}")
    webview: Any = importlib.import_module("webview")
    window: Any = webview.create_window("HTU P0 Probe", str(html), js_api=Bridge(), width=720, height=480)
    allow_exit = threading.Event()
    closed_count = 0
    tray: Any = None
    native: Any = None
    system: Any = None
    forms: Any = None

    def gui(action: Callable[[], Any]) -> Any:
        return native.Invoke(system.Func[system.Object](action))

    def closing() -> bool:
        nonlocal closed_count
        if allow_exit.is_set():
            return True
        native.Hide()
        closed_count += 1
        return False

    def worker() -> None:
        nonlocal native, system, forms, tray
        try:
            if not window.events.loaded.wait(20):
                raise TimeoutError("WebView2 did not load in 20 seconds")
            report["load_seconds"] = round(time.monotonic() - started, 3)
            winforms: Any = importlib.import_module("webview.platforms.winforms")
            native = winforms.BrowserView.instances[window.uid]
            system = importlib.import_module("System")
            forms = importlib.import_module("System.Windows.Forms")
            window.events.closing += closing
            window.evaluate_js("""
                window.__probe = {};
                window.pywebview.api.call('ping', {}).then(result => { window.__probe.ping = result; });
            """)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                ping = window.evaluate_js("window.__probe.ping || null")
                if ping:
                    report["ping"] = ping
                    break
                time.sleep(0.05)
            assert report.get("ping") == {"ok": True, "data": "pong"}
            report["public_methods"] = window.evaluate_js("Object.keys(window.pywebview.api)")
            assert report["public_methods"] == ["call"]
            assert window.evaluate_js("document.querySelector('h1').textContent") == "Harness Token Usage"
            server = webview.http.global_server
            report["http_bind"] = list(server.httpd.server_address)
            report["static_root"] = server.root_path
            assert server.httpd.server_address[0] == "127.0.0.1"
            for endpoint, method in (("/js_api/probe", "POST"), ("/assets/", "GET")):
                try:
                    with urlopen(Request(server.address.rstrip("/") + endpoint, method=method), timeout=3):
                        raise AssertionError(f"Unexpected HTTP endpoint {endpoint}")
                except HTTPError as error:
                    report[f"http_{method}_{endpoint}"] = error.code
                    assert error.code in {403, 405, 501}
            window.evaluate_js(
                "window.__htu = { emit: (name, payload) => { window.__probe.event = {name,payload}; } }"
            )
            pushed = threading.Event()

            def push() -> None:
                window.evaluate_js("window.__htu.emit('probe.tick', {value: 1})")
                pushed.set()

            timer = threading.Timer(3, push)
            timer.start()
            assert pushed.wait(8), "Backend push timed out"
            report["push"] = window.evaluate_js("window.__probe.event")
            assert report["push"] == {"name": "probe.tick", "payload": {"value": 1}}

            if mode == "tray":
                drawing: Any = importlib.import_module("System.Drawing")

                def restore(*_: Any) -> None:
                    native.Show()
                    native.WindowState = forms.FormWindowState.Normal
                    native.Activate()

                def quit_probe(*_: Any) -> None:
                    allow_exit.set()
                    native.Close()

                def create_tray() -> None:
                    nonlocal tray
                    report["gui_thread"] = int(system.Threading.Thread.CurrentThread.ManagedThreadId)
                    tray = forms.NotifyIcon()
                    tray.Icon = drawing.SystemIcons.Application
                    tray.Text = "HTU P0 Probe"
                    menu = forms.ContextMenuStrip()
                    menu.Items.Add("打开").Click += restore
                    menu.Items.Add("退出").Click += quit_probe
                    tray.ContextMenuStrip = menu
                    tray.DoubleClick += restore
                    tray.Visible = True

                gui(create_tray)
                for _ in range(20):
                    gui(lambda: native.Hide())
                    assert not gui(lambda: native.Visible)
                    gui(lambda: tray.ContextMenuStrip.Items[0].PerformClick())
                    assert gui(lambda: native.Visible)
                    gui(lambda: native.Close())
                    assert not gui(lambda: native.Visible)
                    gui(restore)
                    assert gui(lambda: native.Visible)
                report["cycles"] = 20
                report["tray_visible"] = bool(gui(lambda: tray.Visible))
                report["restore_paths"] = ["menu PerformClick", "shared double-click callback"]
            elif mode == "suspend":
                gui(lambda: native.webview.Hide())
                task: Any = gui(lambda: native.webview.CoreWebView2.TrySuspendAsync())
                deadline = time.monotonic() + 10
                while not task.IsCompleted and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert task.IsCompleted, "TrySuspendAsync timed out"
                report["suspend_result"] = bool(task.Result)
                assert report["suspend_result"]
                gui(lambda: native.Hide())

                def resume() -> None:
                    native.webview.CoreWebView2.Resume()
                    native.webview.Show()
                    native.Show()

                gui(resume)
                assert window.evaluate_js("1 + 1") == 2
                report["resume_js"] = True
            else:
                gui(lambda: native.Close())
                assert not gui(lambda: native.Visible)
                gui(lambda: native.Show())
                assert gui(lambda: native.Visible)
            report["cancelled_closes"] = closed_count
            report["passed"] = True
        except Exception:
            report["error"] = traceback.format_exc()
        finally:
            allow_exit.set()
            if tray is not None:

                def dispose() -> None:
                    tray.Visible = False
                    tray.Dispose()

                gui(dispose)
                report["tray_disposed"] = True
            window.destroy()

    with tempfile.TemporaryDirectory(prefix="htu-probe-") as temporary:
        os.environ["HTU_DATA_DIR"] = temporary
        try:
            webview.start(
                worker,
                gui="edgechromium",
                http_server=True,
                server=StaticOnlyServer,
                storage_path=temporary,
                private_mode=True,
            )
        except Exception:
            report["passed"] = False
            report["error"] = traceback.format_exc()
        finally:
            server = webview.http.global_server
            if server is not None:
                server.stop()
    report["total_seconds"] = round(time.monotonic() - started, 3)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["passed"] else 1
