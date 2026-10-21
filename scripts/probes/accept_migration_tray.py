"""Short isolated acceptance of custom legacy paths, native pickers and tray continuity."""

import ctypes
import json
import shutil
import time
from ctypes import wintypes
from typing import Any
from uuid import uuid4

import psutil
from htd.adapters.desktop._win32 import native_function

from scripts.probes.ui_probe import ROOT, UiProbe


def finish_picker(probe: UiProbe, *, accept: bool) -> None:
    enum_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    enum = native_function("user32", "EnumWindows", (enum_type, wintypes.LPARAM), wintypes.BOOL)
    owner = native_function(
        "user32", "GetWindowThreadProcessId", (wintypes.HWND, ctypes.POINTER(wintypes.DWORD)), wintypes.DWORD
    )
    title = native_function(
        "user32", "GetWindowTextW", (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int
    )
    class_name = native_function(
        "user32", "GetClassNameW", (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int), ctypes.c_int
    )
    visible = native_function("user32", "IsWindowVisible", (wintypes.HWND,), wintypes.BOOL)
    post = native_function(
        "user32",
        "PostMessageW",
        (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM),
        wintypes.BOOL,
    )
    found: list[int] = []
    candidates: list[str] = []
    identifiers = {
        probe.process.pid,
        *(child.pid for child in psutil.Process(probe.process.pid).children(recursive=True)),
    }

    def visit(window: int, _: int) -> bool:
        pid = wintypes.DWORD()
        owner(window, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(256)
        class_name(window, name, len(name))
        if pid.value not in identifiers or name.value != "#32770" or not visible(window):
            return True
        caption = ctypes.create_unicode_buffer(256)
        title(window, caption, len(caption))
        candidates.append(caption.value)
        if caption.value == "位置不可用":
            post(window, 0x0111, 1, 0)
        elif caption.value == "选择文件":
            found.append(window)
        return True

    deadline = time.monotonic() + 10
    while not found and time.monotonic() < deadline:
        enum(enum_type(visit), 0)
        time.sleep(0.05)
    assert found, f"Native file picker did not appear: {candidates}"
    assert post(found[0], 0x0111 if accept else 0x0010, 1 if accept else 0, 0)
    probe.wait("!document.querySelector('#legacy-source-path').disabled")


def main() -> None:
    report: dict[str, Any] = {"checks": []}
    with UiProbe(ROOT / ".verify/migration-tray" / uuid4().hex) as probe:
        probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
        probe.command("resize", width=1024, height=680)
        folder = probe.directory / "用户备份"
        folder.mkdir()
        source = folder / "旧版账本.sqlite3"
        shutil.copyfile(ROOT / "fixtures/legacy/v030.sqlite3", source)
        probe.navigate("设置", "设置")
        probe.click("数据与历史")
        probe.click("导入旧版数据…")
        probe.wait("!!document.querySelector('#legacy-source-path')")
        probe.input("#legacy-source-path", str(source))
        probe.wait("document.querySelector('dialog[open]').textContent.includes('检测到旧版数据库')")
        for accept in (False, True):
            probe.click("选择文件…")
            finish_picker(probe, accept=accept)
            assert probe.script("document.querySelector('#legacy-source-path').value") == str(source)
        screenshot = ROOT / ".verify/migration-tray-path-picker.png"
        probe.command("capture", path=str(screenshot))
        report["screenshot"] = str(screenshot)
        probe.script("window.__originalInput=document.querySelector('#legacy-source-path')")
        before = probe.command("state")
        probe.command("hide")
        deadline = time.monotonic() + 10
        while True:
            hidden = probe.command("state")
            if not hidden["visible"] and hidden["suspended"]:
                break
            assert time.monotonic() < deadline, hidden
        assert all(row["eco"] and row["memoryPriority"] == 1 for row in hidden["processes"])
        assert {"Gpu", "Browser"} <= {row["kind"] for row in hidden["processes"]}
        started = time.monotonic()
        probe.command("restore")
        probe.wait("window.__originalInput===document.querySelector('#legacy-source-path')")
        report["restoreSeconds"] = round(time.monotonic() - started, 3)
        assert probe.script("document.querySelector('#legacy-source-path').value") == str(source)
        restored = probe.command("state")
        assert restored["visible"] and not restored["suspended"] and restored["backgroundProcesses"] == 0
        assert all(not row["eco"] and row["memoryPriority"] == 5 for row in restored["processes"])
        assert restored["memoryTarget"] == "Normal"
        probe.click("关闭", "document.querySelector('dialog[open]')")
        probe.navigate("数据统计与分析", "数据统计与分析")
        probe.wait("!!document.querySelector('.metric')")
        probe.script(
            "window.__originalMetric=document.querySelector('.metric');document.querySelector('.workspace').scrollTop=180"
        )
        scroll = probe.script("document.querySelector('.workspace').scrollTop")
        probe.command("hide")
        probe.command("restore")
        probe.wait("window.__originalMetric===document.querySelector('.metric')")
        assert probe.script("document.querySelector('.workspace').scrollTop") == scroll
        probe.command("append")
        probe.click("立即更新")
        probe.wait("!document.querySelector('.metric[aria-busy=true]')")
        report["states"] = {"visible": before, "hidden": hidden, "restored": restored}
        report["checks"] = [
            "custom Unicode backup path detected through the native bridge",
            "real native picker accepts and cancels without changing the selected file",
            "hidden GPU and browser processes use EcoQoS and low memory priority",
            "restore retains dialog draft, DOM, scroll and foreground updates",
            "restore removes background settings before interaction and restores the Normal target",
        ]
    (ROOT / ".verify/migration-tray-native.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
