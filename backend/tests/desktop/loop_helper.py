"""Source desktop entry with test-owned observations and deterministic state barriers."""

# pyright: reportPrivateUsage=false

import argparse
import importlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import htd.__main__ as entry
from htd.adapters.desktop.controller import DesktopController
from htd.adapters.desktop.native import NativeWindow
from htd.adapters.desktop.single_instance import SingleInstance
from htd.adapters.desktop.tray import Tray
from htd.adapters.sqlite.collection_store import SqliteCollectionStore

from scripts.lifecycle_support import Windows, publish_status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--secondary", action="store_true")
    parser.add_argument("--quit", action="store_true")
    args = parser.parse_args()
    directory = Path(os.environ["HTD_DATA_DIR"])
    output = directory / "observations.jsonl"
    lock = threading.Lock()

    def record(kind: str, **values: object) -> None:
        with lock, output.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "pid": os.getpid(), **values}) + "\n")

    def barrier(name: str) -> None:
        record(name + "-held")
        deadline = time.monotonic() + 45
        while not (directory / (name + ".release")).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Test barrier {name} was not released")
            time.sleep(0.05)
        record(name + "-released")

    def instance(root: Path) -> SingleInstance:
        return SingleInstance(root, name=args.name, pipe_name=args.name)

    patched_entry: Any = entry
    patched_entry.SingleInstance = instance

    # Errors must fail automation instead of leaving a modal dialog unattended.
    def show_error(message: str) -> None:
        record("error", message=message)

    patched_entry.show_error = show_error
    if args.secondary or args.quit:

        def forbidden_bootstrap(*_: Any, **__: Any) -> None:
            raise AssertionError("Secondary attempted to bootstrap an application")

        patched_entry.Application = forbidden_bootstrap
        return entry.main(["--quit"] if args.quit else [])

    original_runtime = entry.webview2_version

    def runtime() -> str | None:
        if (directory / "startup.hold").exists():
            barrier("startup")
        return original_runtime()

    patched_entry.webview2_version = runtime
    windows = Windows()
    original_tick = DesktopController.tick
    original_handle = DesktopController.handle
    original_commit = SqliteCollectionStore.commit_batch
    original_tray_init, original_tray_close = Tray.__init__, Tray.close
    original_focus = NativeWindow.focus
    created = disposed = 0
    tray_handle = tray_identifier = 0
    focus_generation = focus_foreground = 0
    activation_generation = 0
    focus_contains = False

    def focus(native: NativeWindow) -> None:
        nonlocal focus_generation, focus_foreground, focus_contains
        original_focus(native)
        focus_generation += 1
        focus_foreground = windows.api.GetForegroundWindow()
        focus_contains = bool(native.form.ContainsFocus)

    def tray_init(tray: Tray, **kwargs: Any) -> None:
        nonlocal created, tray_handle, tray_identifier
        original_tray_init(tray, **kwargs)
        # Cache stable native identity so polling does not allocate CLR reflection proxies.
        reflection: Any = importlib.import_module("System.Reflection")
        flags = reflection.BindingFlags.Instance | reflection.BindingFlags.NonPublic
        icon = tray._tray
        tray_identifier = int(icon.GetType().GetField("id", flags).GetValue(icon))
        owner = icon.GetType().GetField("window", flags).GetValue(icon)
        tray_handle = int(owner.Handle.ToInt64())
        created += 1
        record("tray-created")

    def tray_close(tray: Tray) -> None:
        nonlocal disposed
        was_disposed = tray._disposed
        original_tray_close(tray)
        if not was_disposed:
            disposed += 1
            record("tray-disposed", visible=bool(tray._tray.Visible))

    def commit(store: SqliteCollectionStore, *arguments: Any, **kwargs: Any) -> Any:
        result = original_commit(store, *arguments, **kwargs)
        hold = directory / "scan.hold"
        if hold.exists():
            hold.unlink()
            # Rows and checkpoint have been written inside the actual writer transaction.
            barrier("scan")
        return result

    def handle(controller: DesktopController, event: Any) -> None:
        nonlocal activation_generation
        original_handle(controller, event)
        if event == "activate":
            activation_generation += 1
        if event == "quit" and controller.lifecycle.state == "closing":
            record("closing")
        if event == "started":
            record("started")

    last_sample = 0.0

    def observe(controller: DesktopController) -> None:
        nonlocal last_sample
        now = time.monotonic()
        if now - last_sample < 0.1 or controller.native is None or controller.tray is None:
            return
        last_sample = now
        native, tray = controller.native, controller.tray
        icon = tray._tray
        threads = [{"name": t.name, "id": t.native_id} for t in threading.enumerate()]
        status = {
            "pid": os.getpid(),
            "handle": int(native.form.Handle.ToInt64()),
            "state": controller.lifecycle.state,
            "ready": controller._attached,
            "dpi": int(native.form.DeviceDpi),
            "focus_generation": focus_generation,
            "activation_generation": activation_generation,
            "focus_foreground": focus_foreground,
            "focus_contains": focus_contains,
            "tray_count": created - disposed,
            "tray_created": created,
            "tray_visible": bool(icon.Visible),
            "tray_handle": tray_handle,
            "tray_id": tray_identifier,
            "threads": threads,
        }
        publish_status(directory / "status.json", status)

    def tick(controller: DesktopController) -> None:
        original_tick(controller)
        if controller.lifecycle.state == "stopped":
            return
        try:
            observe(controller)
        except Exception as error:
            record("observation-error", message=str(error))
            original_handle(controller, "quit")

    patched_controller: Any = DesktopController
    patched_controller.tick = tick
    patched_controller.handle = handle

    def error_dialog(*_: Any) -> bool:
        record("webview-error")
        return False

    def shutdown_dialog(*_: Any) -> None:
        record("shutdown-error")

    patched_native: Any = NativeWindow
    patched_native.focus = focus
    patched_native.error_dialog = error_dialog
    patched_native.shutdown_dialog = shutdown_dialog
    patched_store: Any = SqliteCollectionStore
    patched_store.commit_batch = commit
    patched_tray: Any = Tray
    patched_tray.__init__, patched_tray.close = tray_init, tray_close
    return entry.main([])


if __name__ == "__main__":
    raise SystemExit(main())
