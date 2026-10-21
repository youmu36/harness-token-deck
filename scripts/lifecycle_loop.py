"""Run isolated source desktop cycles and state-specific repeated launches on Windows."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
from htd.adapters.desktop.single_instance import SingleInstance

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lifecycle_support import (  # noqa: E402
    Windows,
    isolated_environment,
    process_tree,
    resource_sample,
    trend,
)

HELPER = ROOT / "backend/tests/desktop/loop_helper.py"


class LifecycleRun:
    def __init__(
        self, directory: Path, *, cycles: int, repeats: int, warmup: int, settle_seconds: float
    ) -> None:
        self.directory, self.cycles, self.repeats, self.warmup = directory, cycles, repeats, warmup
        self.environment = isolated_environment(directory)
        self.environment["PYTHONPATH"] = os.pathsep.join((str(ROOT / "backend/src"), str(ROOT)))
        self.data = Path(self.environment["HTD_DATA_DIR"])
        self.name = f"HTD.Loop.{uuid4().hex}"
        self.windows = Windows()
        self.process: subprocess.Popen[str] | None = None
        self.status: dict[str, Any] = {}
        self.focus_before = -1
        self.settle_seconds = settle_seconds
        self.samples: list[dict[str, Any]] = []
        self.special: list[dict[str, Any]] = []
        self.warmup_samples: list[dict[str, Any]] = []
        self.known_processes: dict[int, psutil.Process] = {}
        self.report: dict[str, Any] = {
            "status": "running",
            "cycles_requested": cycles,
            "cycles_completed": 0,
            "special_repeats": repeats,
            "warmup_cycles": warmup,
            "settle_seconds": settle_seconds,
            "warmup_samples": self.warmup_samples,
            "data_directory": str(self.data),
            "instance_name": self.name,
            "samples": self.samples,
            "special_states": self.special,
        }

    def rows(self) -> list[dict[str, Any]]:
        path = self.data / "observations.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        return [json.loads(line) for line in lines if line.endswith("\n")]

    def observed(self, kind: str, *, count: int = 1) -> bool:
        return sum(row["kind"] == kind for row in self.rows()) >= count

    def refresh(self) -> None:
        with suppress(FileNotFoundError, PermissionError):
            self.status = json.loads((self.data / "status.json").read_text(encoding="utf-8"))

    def wait(self, condition: Callable[[], bool], message: str, *, timeout: float = 20) -> None:
        assert self.process is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.refresh()
            errors = [
                row
                for row in self.rows()
                if row["kind"] in {"error", "observation-error", "webview-error", "shutdown-error"}
            ]
            if errors:
                raise AssertionError(f"Desktop helper reported {errors}")
            if condition():
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError(f"{message}; exit={self.process.poll()}, status={self.status}")

    def launch_secondary(self, state: str, index: int) -> None:
        self.refresh()
        activation_before = self.status.get("activation_generation", 0)
        self.windows.grant_foreground()
        completed = subprocess.run(
            [sys.executable, str(HELPER), self.name, "--secondary"],
            cwd=ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=8,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(f"Secondary {state}/{index}: {completed.returncode}: {completed.stderr}")
        if state not in {"starting", "closing"}:
            # ACK confirms queuing, not GUI completion; a previous visible frame may still be current.
            self.wait(
                lambda: self.status.get("activation_generation", 0) > activation_before,
                "Secondary activation was not processed",
            )

    def forward(self, command: str) -> None:
        self.windows.grant_foreground()
        with SingleInstance(self.data, name=self.name, pipe_name=self.name) as instance:
            assert not instance.primary, "Primary lost its mutex"
            assert instance.forward(command), f"No ACK for {command}"

    def visible(self) -> None:
        self.wait(
            lambda: (
                self.status.get("state") == "visible"
                and self.windows.visible(self.status["handle"])
                and self.status["focus_generation"] > self.focus_before
                and self.status["focus_foreground"] == self.status["handle"]
                and self.status["focus_contains"]
            ),
            "Window did not restore and focus",
        )

    def hide(self) -> None:
        self.focus_before = self.status["focus_generation"]
        self.windows.close_window(self.status["handle"])
        self.wait(
            lambda: (
                self.status.get("state") == "hidden"
                and not self.windows.api.IsWindowVisible(self.status["handle"])
            ),
            "WM_CLOSE did not hide window",
        )

    def sample(self, phase: str, index: int) -> dict[str, Any]:
        assert self.process is not None
        self.refresh()
        row = resource_sample(self.process.pid, self.status["pid"])
        self.known_processes.update((member.pid, member) for member in process_tree(self.process.pid))
        collection = [thread["id"] for thread in self.status["threads"] if thread["name"] == "htd-collection"]
        assert row["application_processes"] == 1, row
        assert collection == self.report["collection_thread_ids"], f"Collection thread changed: {collection}"
        assert self.status["tray_count"] == self.status["tray_created"] == 1, self.status
        assert self.status["tray_visible"], self.status
        assert self.windows.icon_present(self.status["tray_handle"], self.status["tray_id"]), "No shell icon"
        return {
            "phase": phase,
            "index": index,
            "state": self.status["state"],
            "window_visible": bool(self.windows.api.IsWindowVisible(self.status["handle"])),
            "window_minimized": bool(self.windows.api.IsIconic(self.status["handle"])),
            "tray_count": self.status["tray_count"],
            "shell_icon_present": True,
            "focus_generation": self.status["focus_generation"],
            "focus_foreground": self.status["focus_foreground"],
            "collection_thread_ids": collection,
            **row,
        }

    def cycle(self) -> None:
        self.launch_secondary("hidden", len(self.samples) + 1)
        self.visible()
        self.hide()
        self.forward("test.trayOpen")
        self.visible()
        self.hide()

    def hold_scan(self, index: int) -> None:
        (self.data / "scan.release").unlink(missing_ok=True)
        (self.data / "scan.hold").touch()
        source = Path(self.environment["HTD_SOURCE_ROOT_DSH"]) / f"scan-{index}.jsonl"
        fixture = (ROOT / "fixtures/dsh/session.jsonl").read_text(encoding="utf-8")
        source.write_text(fixture.replace("synthetic-dsh", f"loop-dsh-{index}"), encoding="utf-8")
        self.wait(lambda: self.observed("scan-held", count=index), "Scan never entered writer transaction")

    def run(self) -> None:
        if self.repeats:
            (self.data / "startup.hold").touch()
        with (
            (self.directory / "stdout.log").open("w", encoding="utf-8") as stdout,
            (self.directory / "stderr.log").open("w", encoding="utf-8") as stderr,
        ):
            self.process = subprocess.Popen(
                [sys.executable, str(HELPER), self.name],
                cwd=ROOT,
                env=self.environment,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
            try:
                self.exercise()
            finally:
                self.cleanup()

    def exercise(self) -> None:
        assert self.process is not None
        if self.repeats:
            self.wait(lambda: self.observed("startup-held"), "Startup barrier not reached")
            for index in range(1, self.repeats + 1):
                self.launch_secondary("starting", index)
                assert not (self.data / "htd.sqlite3").exists(), "Secondary opened database during startup"
                owner_pid = next(row["pid"] for row in self.rows() if row["kind"] == "startup-held")
                resources = resource_sample(self.process.pid, owner_pid)
                assert resources["application_processes"] == 1, resources
                self.special.append({"phase": "starting", "index": index, **resources})
            (self.data / "startup.release").touch()
        self.wait(lambda: self.status.get("ready") is True, "Desktop receiver not ready")
        self.report["dpi"] = self.status["dpi"]
        self.report["collection_thread_ids"] = [
            thread["id"] for thread in self.status["threads"] if thread["name"] == "htd-collection"
        ]
        assert len(self.report["collection_thread_ids"]) == 1
        if self.repeats:
            assert sum(row["kind"] == "started" for row in self.rows()) == 1
        self.hide()
        for index in range(1, self.warmup + 1):
            self.cycle()
            self.warmup_samples.append(self.sample("warmup", index))
        if self.settle_seconds:
            print(f"Settling WebView2 initialization for {self.settle_seconds:g}s", flush=True)
            due = time.monotonic() + self.settle_seconds
            while time.monotonic() < due:
                self.warmup_samples.append(self.sample("settling", len(self.warmup_samples) + 1))
                self.wait(lambda: self.status.get("ready") is True, "Receiver failed during settling")
                time.sleep(min(1, max(0, due - time.monotonic())))
        for index in range(1, self.cycles + 1):
            self.cycle()
            self.samples.append(self.sample("cycle", index))
            self.report["cycles_completed"] = index
            if index % 10 == 0 or index == self.cycles:
                print(f"Lifecycle {index}/{self.cycles}", flush=True)
        if self.repeats:
            self.special_states()
        metrics = {"owner_handles": 32, "tree_handles": 64, "owner_threads": 4, "tree_threads": 8}
        self.report["resource_trends"] = {
            key: trend([row[key] for row in self.samples], limit) for key, limit in metrics.items()
        }
        assert all(item["passed"] for item in self.report["resource_trends"].values()), self.report[
            "resource_trends"
        ]
        if not self.repeats:
            self.forward("quit")
        self.process.wait(timeout=20)
        assert self.process.returncode == 0, "Primary exited unsuccessfully"
        self.verify_exit()
        self.report["status"] = "passed"

    def special_states(self) -> None:
        for index in range(1, self.repeats + 1):
            self.launch_secondary("visible", index)
            self.visible()
            self.special.append(self.sample("visible", index))
            self.focus_before = self.status["focus_generation"]
            self.windows.api.ShowWindow(self.status["handle"], 6)
            self.wait(lambda: bool(self.windows.api.IsIconic(self.status["handle"])), "Window not minimized")
            self.launch_secondary("minimized", index)
            self.visible()
            self.special.append(self.sample("minimized", index))
        self.hold_scan(1)
        for index in range(1, self.repeats + 1):
            self.hide()
            self.launch_secondary("scanning", index)
            self.visible()
            assert not self.observed("scan-released"), "Scan barrier ended early"
            self.special.append(self.sample("scanning", index))
        (self.data / "scan.release").touch()
        self.wait(lambda: self.observed("scan-released"), "Scan did not finish")
        self.hold_scan(2)
        self.forward("quit")
        self.wait(lambda: self.status.get("state") == "closing", "Closing state not reached")
        for index in range(1, self.repeats + 1):
            self.launch_secondary("closing", index)
            self.refresh()
            assert self.status["state"] == "closing", self.status
            self.special.append(self.sample("closing", index))
        (self.data / "scan.release").touch()
        print(f"Special states: starting/visible/minimized/scanning/closing x {self.repeats}", flush=True)

    def verify_exit(self) -> None:
        self.wait_dead_processes()
        assert not self.windows.api.IsWindow(self.status["handle"]), "Window survived shutdown"
        assert not self.windows.icon_present(self.status["tray_handle"], self.status["tray_id"])
        assert sum(row["kind"] == "tray-disposed" for row in self.rows()) == 1
        assert not any(row["kind"].endswith("error") for row in self.rows())
        assert not list(self.data.glob("instance-*.key")) and not list(self.data.glob(".instance-*.json"))
        with SingleInstance(self.data, name=self.name, pipe_name=self.name) as next_instance:
            assert next_instance.primary, "Mutex survived shutdown"
            api: Any = ctypes_kernel()
            assert not api.WaitNamedPipeW(next_instance.address, 0), "Pipe survived shutdown"
        with sqlite3.connect(self.data / "htd.sqlite3") as database:
            assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        self.report["exit_cleanup"] = {
            "processes": True,
            "window": True,
            "tray": True,
            "mutex": True,
            "pipe": True,
            "credentials": True,
            "database_integrity": True,
        }

    def wait_dead_processes(self) -> None:
        _, alive = psutil.wait_procs(list(self.known_processes.values()), timeout=8)
        assert not alive, f"Residual processes: {[process.pid for process in alive]}"

    def cleanup(self) -> None:
        for name in ("startup", "scan"):
            (self.data / (name + ".release")).touch()
        if self.process is not None and self.process.poll() is None:
            with suppress(AssertionError, OSError):
                self.forward("quit")
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=10)
        remaining = list(self.known_processes.values())
        if self.process is not None:
            remaining.extend(process_tree(self.process.pid))
        for member in reversed(remaining):
            with suppress(psutil.NoSuchProcess):
                member.kill()


def ctypes_kernel() -> Any:
    import ctypes
    from ctypes import wintypes

    api: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    api.WaitNamedPipeW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
    api.WaitNamedPipeW.restype = wintypes.BOOL
    return api


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--special-repeats", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--settle-seconds", type=float, default=60)
    parser.add_argument("--report", type=Path, default=ROOT / ".verify/lifecycle.json")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Lifecycle acceptance requires an interactive Windows desktop")
    if args.cycles < 1 or args.special_repeats < 0 or args.warmup < 0 or args.settle_seconds < 0:
        parser.error(
            "cycles must be positive; special-repeats, warmup and settle-seconds must be nonnegative"
        )
    directory = ROOT / ".verify/lifecycle-runs" / uuid4().hex
    directory.mkdir(parents=True)
    run = LifecycleRun(
        directory,
        cycles=args.cycles,
        repeats=args.special_repeats,
        warmup=args.warmup,
        settle_seconds=args.settle_seconds,
    )
    started = time.monotonic()
    try:
        run.run()
    except Exception as error:
        run.report.update(status="failed", error=f"{type(error).__name__}: {error}")
        print(run.report["error"], file=sys.stderr, flush=True)
    run.report.update(seconds=round(time.monotonic() - started, 3), finished_at=datetime.now(UTC).isoformat())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(run.report, indent=2) + "\n", encoding="utf-8")
    print(f"Report: {args.report.resolve()}; logs: {directory}", flush=True)
    return int(run.report["status"] != "passed")


if __name__ == "__main__":
    raise SystemExit(main())
