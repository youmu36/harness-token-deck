"""Isolated three-source WebView2 acceptance fixture, driven through local CDP."""

# pyright: reportPrivateUsage=false

import argparse
import importlib
import json
import os
import re
import sqlite3
from contextlib import suppress
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import htu.__main__ as entry
from htu.adapters.desktop._win32 import native_function
from htu.adapters.desktop.native import NativeWindow
from htu.adapters.desktop.single_instance import SingleInstance
from htu.application.remote.ports import MemoryCredentialStore
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant
from htu.domain.remote.model import BalanceKind, BalanceObservation, RemoteAccount

from backend.tests.contracts.management_helpers import group, price, vendor, wallet
from backend.tests.integration.scan_helpers import FIXTURES, headers, request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--history", action="store_true", help="Keep the first-launch history prompt")
    parser.add_argument(
        "--remote-test", action="store_true", help="Read the user-provided key only in memory"
    )
    args = parser.parse_args()
    startup_key = os.environ.get("HTU_PROBE_STARTUP_KEY")
    if startup_key:
        import htu.adapters.desktop.startup as startup

        startup.KEY = startup_key
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "htu.sqlite3").exists():
        raise ValueError("Use a new acceptance directory")
    for key in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "CODEX_HOME"):
        path = directory / key.lower()
        path.mkdir()
        os.environ[key] = str(path)
    os.environ["HTU_DATA_DIR"] = str(directory)
    os.environ["HTU_DEBUG"] = "0"
    roots = {
        "codex": directory / "codex",
        "zcode": directory / "zcode.sqlite",
        "dsh": directory / "dsh",
        "claude": directory / "claude",
    }
    for name, path in roots.items():
        os.environ[f"HTU_SOURCE_ROOT_{name.upper()}"] = str(path)
        if name != "zcode":
            path.mkdir()
    (roots["codex"] / "fixture.jsonl").write_bytes(headers())
    with sqlite3.connect(roots["zcode"]) as connection:
        connection.executescript((FIXTURES / "zcode/schema.sql").read_text(encoding="utf-8"))
    dsh_rows = [json.loads(line) for line in (FIXTURES / "dsh/session.jsonl").read_bytes().splitlines()]
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    with Application(directory, clock=FakeClock(Instant(start)), start_scheduler=False) as app:
        group_id, version_id = group(app, "Fixture pricing")
        first = version_id
        model_ids = ["gpt-6-astra", "deepseek-v4-flash", "claude-sonnet-4-6"]
        for number, rate in ((1, "1"), (2, "3")):
            if number == 2:
                added = price(app, "pricing.addVersion", groupId=group_id, number=2, copyFromVersionId=first)
                assert added.versionId
                version_id = added.versionId
            price(
                app,
                "pricing.saveVersionPrices",
                versionId=version_id,
                models=[
                    {"modelId": model, "input": rate, "cache": rate, "output": rate} for model in model_ids
                ],
            )
        for name in roots:
            price(app, "pricing.switchCurrent", harness=name, groupId=group_id, versionId=first)
        supplier = vendor(app, "Fixture vendor", "-1.239")
        wallet(
            app,
            "wallet.linkGroups",
            vendorId=supplier.id,
            groupIds=[group_id],
            expectedRevision=app.revisions()["wallet"],
        )
        result = app.bridge.call(
            "preferences.update",
            {
                "operationId": str(uuid4()),
                "expectedRevision": 0,
                "patch": {
                    "timezone": "UTC",
                    "theme": "light",
                    "historyPrompted": not args.history,
                    "guideSeen": True,
                },
            },
        )
        assert result["ok"], result
        app.scan.collect()
    index = 0

    def append(at: Instant | None = None) -> None:
        nonlocal index
        index += 1
        at = at or Instant(datetime.now(UTC))
        with (roots["codex"] / "fixture.jsonl").open("ab") as stream:
            stream.write(request(index, at))
        with sqlite3.connect(roots["zcode"]) as connection:
            connection.execute(
                "INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(index),
                    "fixture",
                    str(index),
                    0,
                    "gpt-6-astra",
                    "completed",
                    int(at.value.timestamp() * 1000),
                    json.dumps({"inputTokens": 100, "cacheReadTokens": 40, "outputTokens": 10}),
                ),
            )
        rows = json.loads(json.dumps(dsh_rows[:4]))
        rows[0]["id"] = f"fixture-dsh-{index}"
        for row in rows:
            row["time"] = int(at.value.timestamp() * 1000)
        (roots["dsh"] / f"fixture-{index}.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )

        claude = {
            "type": "assistant",
            "sessionId": "fixture-claude",
            "timestamp": at.value.isoformat(),
            "providerName": "https://fixture-claude.example",
            "message": {
                "id": f"claude-{index}",
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 300,
                    "cache_creation_input_tokens": 200,
                    "output_tokens": 20,
                },
            },
        }
        with (roots["claude"] / "fixture.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(claude) + "\n")

    if args.history:
        append(Instant(start - timedelta(hours=2)))
    append(
        Instant(datetime.now(UTC) - timedelta(minutes=3))
        if os.environ.get("HTU_PROBE_SYNTHETIC_REMOTE") == "1"
        else None
    )
    original_poll = NativeWindow.poll
    pending: Any = None
    pending_id: str | None = None
    capture_stream: Any = None
    focus_window: Any = None

    def poll(native: NativeWindow) -> None:
        nonlocal pending, pending_id, capture_stream, focus_window
        if pending is not None and pending.IsCompleted:
            result = json.loads(str(pending.Result)) if capture_stream is None else {"captured": True}
            if capture_stream is not None:
                capture_stream.Dispose()
                capture_stream = None
            (directory / "probe-result.json").write_text(
                json.dumps({"id": pending_id, "result": result}), encoding="utf-8"
            )
            pending = None
        command = directory / "probe-command.json"
        if pending is None and native.form.webview.CoreWebView2 is not None and command.exists():
            payload = json.loads(command.read_text(encoding="utf-8"))
            command.unlink()
            if payload["action"] == "append":
                append()
            elif payload["action"] == "background":
                if focus_window is None:
                    focus_window = native.forms.Form()
                    focus_window.Text = "HTU isolated focus probe"
                    focus_window.ShowInTaskbar = False
                    focus_window.Opacity = 0
                focus_window.Show()
                focus_window.Activate()
            elif payload["action"] == "hide":
                native.form.Close()
            elif payload["action"] == "restore":
                if focus_window is not None:
                    focus_window.Hide()
                cast(Any, native.host_action).__self__.handle("activate")
            elif payload["action"] == "state":
                (directory / "probe-result.json").write_text(
                    json.dumps(
                        {
                            "id": payload["id"],
                            "result": {
                                "visible": bool(native.form.Visible),
                                "focused": bool(native.form.ContainsFocus),
                                "windowState": str(native.form.WindowState),
                                "webviewVisible": bool(native.form.webview.Visible),
                                "webviewFocused": bool(native.form.webview.ContainsFocus),
                                "foregroundWindow": int(
                                    native_function("user32", "GetForegroundWindow", (), wintypes.HWND)() or 0
                                ),
                                "windowHandle": int(native.form.Handle.ToInt64()),
                                "memoryTarget": str(
                                    getattr(
                                        native.form.webview.CoreWebView2,
                                        "MemoryUsageTargetLevel",
                                        "unavailable",
                                    )
                                ),
                                "backgroundFocused": bool(
                                    focus_window is not None and focus_window.ContainsFocus
                                ),
                                "suspended": bool(
                                    getattr(native.form.webview.CoreWebView2, "IsSuspended", False)
                                ),
                                "backgroundProcesses": len(native.power._background),
                                "processes": process_states(native),
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                original_poll(native)
                return
            elif payload["action"] == "startup-state" and startup_key:
                import winreg

                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, startup_key) as key:
                        value = winreg.QueryValueEx(key, "HarnessTokenUsage")[0]
                except FileNotFoundError:
                    value = None
                (directory / "probe-result.json").write_text(
                    json.dumps(
                        {"id": payload["id"], "result": {"enabled": value is not None, "command": value}}
                    ),
                    encoding="utf-8",
                )
                original_poll(native)
                return
            elif payload["action"] == "zoom":
                native.form.webview.ZoomFactor = payload["factor"]
            elif payload["action"] == "resize":
                drawing: Any = importlib.import_module("System.Drawing")
                scale = float(native.form.DeviceDpi) / 96
                native.form.WindowState = native.forms.FormWindowState.Normal
                native.form.Size = drawing.Size(int(payload["width"] * scale), int(payload["height"] * scale))
            elif payload["action"] == "minimize":
                native.form.WindowState = native.forms.FormWindowState.Minimized
            elif payload["action"] == "quit":
                native.host_action("htu.quit")
            elif payload["action"] == "script":
                pending = native.form.webview.CoreWebView2.ExecuteScriptAsync(
                    "(() => { return eval(" + json.dumps(payload["script"]) + "); })()"
                )
            elif payload["action"] == "remote-credential" and args.remote_test:
                # This input exists already in the user's test file; never copy it to probe command files.
                source = (Path(__file__).resolve().parents[2] / "docs/测试.txt").read_text(
                    encoding="utf-8-sig"
                )
                secrets = re.findall(r"测试密钥[\uff1a:]\s*(\S+)", source)
                credential_index = {"moyuu": 0, "heilovehei": 1}[payload.get("site", "moyuu")]
                assert len(secrets) > credential_index
                code = (
                    "const e=document.querySelector('input[aria-label=\"令牌\"]');"
                    f"e.value={json.dumps(secrets[credential_index])};"
                    "e.dispatchEvent(new Event('input',{bubbles:true}));return true;"
                )
                pending = native.form.webview.CoreWebView2.ExecuteScriptAsync("(() => {" + code + "})()")
            elif payload["action"] == "key":
                native.focus()
                native.forms.SendKeys.SendWait(payload["keys"])
            elif payload["action"] == "capture":
                io: Any = importlib.import_module("System.IO")
                core: Any = importlib.import_module("Microsoft.Web.WebView2.Core")
                capture_stream = io.FileStream(str(Path(payload["path"]).resolve()), io.FileMode.Create)
                pending = native.form.webview.CoreWebView2.CapturePreviewAsync(
                    core.CoreWebView2CapturePreviewImageFormat.Png, capture_stream
                )
            pending_id = payload["id"]
            if pending is None:
                (directory / "probe-result.json").write_text(
                    json.dumps(
                        {
                            "id": pending_id,
                            "index": index,
                            "zoom": float(native.form.webview.ZoomFactor),
                            "deviceDpi": int(native.form.DeviceDpi),
                        }
                    ),
                    encoding="utf-8",
                )
        original_poll(native)

    instance_name = f"HTU.UsageProbe.{uuid4().hex}"

    def instance(root: Path) -> SingleInstance:
        return SingleInstance(root, name=instance_name, pipe_name=instance_name)

    patched: Any = entry
    patched.SingleInstance = instance
    if os.environ.get("HTU_PROBE_SYNTHETIC_REMOTE") == "1":
        credentials = MemoryCredentialStore()

        class SyntheticProvider:
            async def fetch(self, account: RemoteAccount, credential: str) -> BalanceObservation:
                return BalanceObservation(
                    account.id,
                    Instant(datetime.now(UTC)),
                    BalanceKind.ACCOUNT_REMAINING,
                    "synthetic",
                    currency="USD",
                    amount_pico=1234000000000,
                )

            async def aclose(self) -> None:
                pass

        def application(*args: Any, **kwargs: Any) -> Application:
            return Application(*args, **kwargs, credentials=credentials, provider_factory=SyntheticProvider)

        patched.Application = application
    patched_native: Any = NativeWindow
    patched_native.poll = poll
    try:
        return entry.main([])
    finally:
        if startup_key:
            import winreg

            with suppress(FileNotFoundError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, startup_key)


def process_states(native: NativeWindow) -> list[dict[str, Any]]:
    import ctypes
    from ctypes import wintypes

    import psutil
    from htu.adapters.desktop._win32 import close_handle, native_function
    from htu.adapters.desktop.process_power import _Throttling

    opened = native_function(
        "kernel32", "OpenProcess", (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE
    )
    get = native_function(
        "kernel32",
        "GetProcessInformation",
        (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD),
        wintypes.BOOL,
    )
    result: list[dict[str, Any]] = []
    for process in native.form.webview.CoreWebView2.Environment.GetProcessInfos():
        pid = int(process.ProcessId)
        handle = opened(0x1000, False, pid)
        if not handle:
            continue
        try:
            power, priority = _Throttling(1, 0, 0), wintypes.ULONG(5)
            get(handle, 4, ctypes.byref(power), ctypes.sizeof(power))
            get(handle, 0, ctypes.byref(priority), ctypes.sizeof(priority))
            result.append(
                {
                    "pid": pid,
                    "kind": str(process.Kind),
                    "eco": bool(power.StateMask & 1),
                    "memoryPriority": priority.value,
                    "rss": psutil.Process(pid).memory_info().rss,
                }
            )
        except psutil.Error:
            pass
        finally:
            close_handle(handle)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
