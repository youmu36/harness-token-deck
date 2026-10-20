"""P8.3/P8.4 production WebView2 flow with isolated sources and real modal pickers."""

import ctypes
import json
import os
import sqlite3
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    directory = ROOT / ".verify" / "p8-model-settings-runs" / uuid4().hex
    directory.mkdir(parents=True)
    report: dict[str, Any] = {"directory": str(directory), "checks": [], "screens": []}
    with (directory / "stdout.log").open("w") as output, (directory / "stderr.log").open("w") as errors:
        process = subprocess.Popen(
            [sys.executable, "-m", "scripts.probes.probe_usage_pages", "--directory", str(directory)],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "backend/src") + os.pathsep + str(ROOT)},
            stdout=output,
            stderr=errors,
            text=True,
        )

        def command(action: str, **values: object) -> Any:
            identifier = uuid4().hex
            (directory / "probe-command.json").write_text(
                json.dumps({"id": identifier, "action": action, **values}), encoding="utf-8"
            )
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline and process.poll() is None:
                result = directory / "probe-result.json"
                if result.exists():
                    try:
                        reply = json.loads(result.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if reply.get("id") == identifier:
                        return reply.get("result", reply)
                time.sleep(0.05)
            raise AssertionError(f"Native command timed out: {action}")

        def script(code: str) -> Any:
            return command("script", script=code)

        def wait(code: str) -> Any:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                value = script(code)
                if value:
                    return value
                time.sleep(0.05)
            raise AssertionError(f"DOM condition timed out: {code}")

        def click(label: str, scope: str = "document") -> None:
            assert script(
                f"(() => {{ const b=[...{scope}.querySelectorAll('button')]"
                f".find(b=>b.textContent.trim()==={json.dumps(label)}); "
                "if (!b || b.disabled) return false; b.click(); return true; })()"
            ), label

        def input(selector: str, value: str, event: str = "input") -> None:
            script(
                f"(() => {{const e=document.querySelector({json.dumps(selector)});"
                f"e.value={json.dumps(value)};"
                f"e.dispatchEvent(new Event({json.dumps(event)},{{bubbles:true}}));}})()"
            )

        def capture(name: str) -> None:
            command("capture", path=str(ROOT / "docs/design/screens" / name))
            report["screens"].append(name)

        def api(name: str, payload: dict[str, object]) -> Any:
            script(
                f"window.__p8result=null;pywebview.api.call({json.dumps(name)},{json.dumps(payload)})"
                ".then(r=>window.__p8result=r)"
            )
            return wait("window.__p8result")

        user: Any = ctypes.WinDLL("user32", use_last_error=True)
        user.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        user.FindWindowW.restype = wintypes.HWND
        user.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user.IsWindowVisible.argtypes = (wintypes.HWND,)
        enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def picker(kind: str, initial: Path, *, accept: bool) -> Any:
            script(
                "window.__pick=null;pywebview.api.call('app.pickPath',"
                f"{{kind:{json.dumps(kind)},initial:{json.dumps(str(initial))}}})"
                ".then(r=>window.__pick=r)"
            )
            deadline = time.monotonic() + 10
            handle = None
            identifiers = {process.pid, *(child.pid for child in psutil.Process(process.pid).children())}
            candidates: list[tuple[int, str]] = []

            def inspect(window: int, parameter: int) -> bool:
                pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(window, ctypes.byref(pid))
                name = ctypes.create_unicode_buffer(256)
                user.GetClassNameW(window, name, len(name))
                if pid.value in identifiers and name.value == "#32770" and user.IsWindowVisible(window):
                    user.GetWindowTextW(window, name, len(name))
                    candidates.append((window, name.value))
                return True

            while time.monotonic() < deadline and not handle:
                candidates.clear()
                user.EnumWindows(enum_callback(inspect), 0)
                if candidates:
                    for window, title in candidates:
                        if title == "位置不可用":
                            # Isolated USERPROFILE has no shell known-folder history.
                            user.PostMessageW(window, 0x0111, 1, 0)
                            continue
                        if kind == "file" and title != "选择文件":
                            continue
                        handle = window
                        report.setdefault("pickerTitles", []).append(title)
                        break
                time.sleep(0.05)
            assert handle, f"Native {kind} picker did not appear: {script('window.__pick')}"
            assert user.PostMessageW(handle, 0x0111 if accept else 0x0010, 1 if accept else 0, 0)
            result = wait("window.__pick")
            assert result["ok"], result
            return result["data"]["path"]

        try:
            wait("!!window.pywebview?.api && !!document.querySelector('.metrics-grid')")
            assert api("collection.scanNow", {"operationId": str(uuid4())})["ok"]
            script("location.hash='/models'")
            wait("!!document.querySelector('.models-tab tbody tr')")
            name = script("document.querySelector('.models-tab tbody th').textContent.trim()")
            input(f'input[aria-label="{name} 输入价格"]', "2.5")
            wait("document.querySelectorAll('tr.dirty').length===1")
            click("保存全部修改")
            wait("document.querySelectorAll('tr.dirty').length===0")
            click("移出")
            wait("!!document.querySelector('dialog[open]')")
            click("移出", "document.querySelector('dialog[open]')")
            wait("document.querySelector('.excluded summary').textContent.includes('1')")
            script("document.querySelector('.excluded').open=true")
            click("恢复")
            wait("document.querySelector('.excluded summary').textContent.includes('0')")
            click("从来源扫描近 14 天")
            wait("!!document.querySelector('dialog[open] .results')")
            assert script(
                "document.querySelector('dialog[open] .results').textContent.includes('gpt-6-astra')"
            )
            capture("p8-models-native.png")
            click("关闭", "document.querySelector('dialog[open]')")
            report["checks"].append("pool base rates / removal / restoration / real three-source scan")

            script("location.hash='/settings'")
            wait("document.querySelectorAll('.source-card').length===3")
            input('select[aria-label="统计时区"]', "system", "change")
            wait("!document.querySelector('select[aria-label=\"统计时区\"]').disabled")
            click("暂停采集")
            wait("document.querySelector('section[aria-label=\"后台\"]').textContent.includes('采集已暂停')")
            click("恢复采集")
            wait("document.querySelector('section[aria-label=\"后台\"]').textContent.includes('采集运行中')")
            input("#path-codex", str(directory / "missing"))
            wait("document.querySelector('.source-card').textContent.includes('路径不可用')")
            click("保存", "document.querySelector('.source-card')")
            wait("document.querySelector('.source-card h3').textContent.includes('来源不可用')")
            report["checks"].append(
                "display persistence / pause-resume / unavailable source save and validation"
            )

            assert picker("file", directory / "zcode.sqlite", accept=False) is None
            selected = picker("file", directory / "zcode.sqlite", accept=True)
            assert Path(selected).resolve() == (directory / "zcode.sqlite").resolve(), selected
            selected = picker("dir", directory / "dsh", accept=True)
            assert Path(selected).resolve() == (directory / "dsh").resolve(), selected
            report["checks"].append("native file cancel / file selection / directory selection on GUI thread")

            click("备份数据库")
            wait("document.querySelector('section[aria-label=\"数据\"]').textContent.includes('备份已完成')")
            with sqlite3.connect(directory / "htu.sqlite3") as connection:
                assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert list((directory / "backups").glob("*.sqlite3"))
            click("回收空闲空间")
            wait(
                "document.querySelector('section[aria-label=\"数据\"]').textContent.includes('空闲空间已回收')"
            )
            input('select[aria-label="保留期限"]', "180", "change")
            wait("!document.querySelector('select[aria-label=\"保留期限\"]').disabled")
            assert api("maintenance.status", {})["data"]["retention"] == 180
            capture("p8-settings-native.png")
            report["checks"].append("online backup / vacuum / retention persistence / SQLite integrity")
            command("quit")
            assert process.wait(timeout=15) == 0
            report["ok"] = True
        except Exception as error:
            report["ok"] = False
            report["error"] = str(error)
            raise
        finally:
            if process.poll() is None:
                try:
                    command("quit")
                    process.wait(timeout=10)
                except Exception:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False, capture_output=True
                    )
            (ROOT / ".verify/p8-model-settings.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
