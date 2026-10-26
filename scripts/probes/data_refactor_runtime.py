"""Repeatable P00/P15 source-host + owned WebView2 CPU/private/RSS measurements."""

import argparse
import json
import os
import platform
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psutil
from htd.domain.common.time import Instant

from backend.tests.integration.scan_helpers import headers, request
from scripts.probes.ui_probe import ROOT, UiProbe


def measure(pid: int, action: Callable[[], None], seconds: float) -> dict[str, Any]:
    stopped = threading.Event()
    samples: list[dict[str, Any]] = []
    cpu_first: dict[int, float] = {}
    cpu_last: dict[int, float] = {}
    roles: dict[int, str] = {}

    def sample() -> None:
        while not stopped.is_set():
            private = rss = 0
            parts = {name: {"privateBytes": 0, "workingSetBytes": 0} for name in ("host", "webview2")}
            names: list[str] = []
            host = psutil.Process(pid)
            for process in [host, *host.children(recursive=True)]:
                try:
                    name = process.name()
                    if process.pid != pid and name.lower() not in (
                        "msedgewebview2.exe",
                        "python.exe",
                        "pythonw.exe",
                        "harnesstokendeck.exe",
                    ):
                        continue
                    memory = process.memory_info()
                    cpu = process.cpu_times()
                    value = cpu.user + cpu.system
                    cpu_first.setdefault(process.pid, value)
                    cpu_last[process.pid] = value
                    # Windows venv launchers can start the actual Python host as a child.
                    role = "webview2" if name.lower() == "msedgewebview2.exe" else "host"
                    roles[process.pid] = role
                    parts[role]["privateBytes"] += memory.private
                    parts[role]["workingSetBytes"] += memory.rss
                    private += memory.private
                    rss += memory.rss
                    names.append(name)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            samples.append(
                {
                    "privateBytes": private,
                    "workingSetBytes": rss,
                    "names": names,
                    "parts": parts,
                    "elapsed": time.monotonic() - started,
                    "cpu": {
                        role: sum(
                            cpu_last[key] - value for key, value in cpu_first.items() if roles[key] == role
                        )
                        for role in ("host", "webview2")
                    },
                }
            )
            stopped.wait(0.1)

    monitor = threading.Thread(target=sample, daemon=True)
    started = time.monotonic()
    monitor.start()
    try:
        action()
        remaining = seconds - (time.monotonic() - started)
        if remaining > 0:
            stopped.wait(remaining)
    finally:
        stopped.set()
        monitor.join()
    elapsed = time.monotonic() - started
    cpu = sum(cpu_last[key] - value for key, value in cpu_first.items())

    def peak(role: str) -> float:
        # A one-second window avoids treating Windows' CPU clock quantum as a spike.
        values: list[float] = []
        left = 0
        for right in range(1, len(samples)):
            while left + 1 < right and samples[right]["elapsed"] - samples[left + 1]["elapsed"] >= 1:
                left += 1
            duration = samples[right]["elapsed"] - samples[left]["elapsed"]
            if duration >= 1:
                values.append((samples[right]["cpu"][role] - samples[left]["cpu"][role]) / duration * 100)
        return max(values, default=0.0)

    return {
        "seconds": elapsed,
        "cpuSeconds": cpu,
        "cpuPercentOneCore": cpu / elapsed * 100,
        "privateSteadyBytes": samples[-1]["privateBytes"],
        "privatePeakBytes": max(row["privateBytes"] for row in samples),
        "workingSetSteadyBytes": samples[-1]["workingSetBytes"],
        "workingSetPeakBytes": max(row["workingSetBytes"] for row in samples),
        "processNames": sorted({name for row in samples for name in row["names"]}),
        "sampleCount": len(samples),
        "processMetrics": {
            role: {
                "processIds": [key for key, value in roles.items() if value == role],
                "cpuSeconds": sum(
                    cpu_last[key] - value for key, value in cpu_first.items() if roles[key] == role
                ),
                "cpuPeakOneCorePercent1s": peak(role),
                "cpuPeakMachinePercent1s": peak(role) / (psutil.cpu_count() or 1),
                "privateSteadyBytes": samples[-1]["parts"][role]["privateBytes"],
                "privatePeakBytes": max(row["parts"][role]["privateBytes"] for row in samples),
                "workingSetSteadyBytes": samples[-1]["parts"][role]["workingSetBytes"],
                "workingSetPeakBytes": max(row["parts"][role]["workingSetBytes"] for row in samples),
            }
            for role in ("host", "webview2")
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--seconds", type=float, default=3)
    parser.add_argument("--requests", type=int, default=20)
    args = parser.parse_args()
    if args.seconds < 1 or not 1 <= args.requests <= 1000:
        parser.error("seconds >= 1 and requests in 1..1000 required")
    directory = ROOT / ".verify/data-refactor-runtime" / (args.label + "-" + uuid4().hex)
    report: dict[str, Any] = {
        "label": args.label,
        "machine": platform.platform(),
        "python": platform.python_version(),
        "logicalCpuCount": psutil.cpu_count(),
        "build": "source Python + production Vite + WebView2",
        "requestsPerCollectionScenario": args.requests * 4,
        "secondsPerScenario": args.seconds,
        "history": "180 days x 32 synthetic requests; same-day four-source input",
        "scenarios": {},
    }
    os.environ["HTD_PROBE_REFACTOR_HISTORY"] = "1"
    with UiProbe(directory) as probe:
        # A cold source snapshot can spend the normal DOM deadline compiling/importing.
        probe.command("state")
        probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")

        def scenario(name: str, action: Callable[[], None]) -> None:
            report["scenarios"][name] = measure(probe.process.pid, action, args.seconds)

        def ingest() -> None:
            for _ in range(args.requests):
                probe.command("append")
            probe.script("pywebview.api.call('collection.scanNow',{operationId:crypto.randomUUID()})")

        scenario("visible-idle", lambda: None)
        probe.command("hide")
        scenario("tray-idle", lambda: None)
        probe.command("restore")
        scenario("collection-four-sources", ingest)

        def codex_sessions(count: int) -> None:
            for index in range(args.requests * 4):
                session = f"perf-{count}-{index % count}"
                path = directory / "codex" / (session + ".jsonl")
                if not path.exists():
                    records = [json.loads(line) for line in headers().splitlines()]
                    records[0]["payload"]["id"] = session
                    path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
                row = json.loads(request(index, Instant(datetime.now(UTC))))
                row["payload"]["thread_id"] = session
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(row) + "\n")
            probe.script("pywebview.api.call('collection.scanNow',{operationId:crypto.randomUUID()})")

        scenario("single-session-equal-input", lambda: codex_sessions(1))
        scenario("multi-session-equal-input", lambda: codex_sessions(8))

        def navigate() -> None:
            for label in ("Codex", "数据统计与分析", "供应商与钱包", "设置"):
                probe.script(
                    "document.querySelector("
                    + json.dumps(f'nav[aria-label="主导航"] a[aria-label="{label}"]')
                    + ")?.click()"
                )

        scenario("navigation-cold", navigate)
        scenario("navigation-warm", navigate)
        long_query = (
            "pywebview.api.call('stats.analytics', {harness:'all',period:'all',limit:1})"
            ".then(r=>window.__runtimeLongTerm=r)"
        )
        scenario("long-history-cold", lambda: probe.script(long_query))
        assert probe.wait("window.__runtimeLongTerm")["ok"]
        scenario("long-history-warm", lambda: probe.script(long_query))
        scenario("visible-after-collection", lambda: None)
        probe.command("hide")
        scenario("tray-after-collection", ingest)
        report["ok"] = True
    with sqlite3.connect(directory / "htd.sqlite3") as database:
        count = database.execute("SELECT COUNT(*) FROM identity").fetchone()[0]
        report["acceptedRequests"] = count
        report["expectedRequests"] = 5760 + args.requests * 16 + 4
        assert count == report["expectedRequests"], (count, report["expectedRequests"])
        assert database.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
    destination = ROOT / f".verify/data-refactor-{args.label}.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(destination), **report}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
