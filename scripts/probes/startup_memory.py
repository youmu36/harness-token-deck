"""Compare cold/settled startup and a tray round trip without forced collections."""

import argparse
import json
import os
from typing import Any
from uuid import uuid4

import psutil

from scripts.probes.data_refactor_runtime import measure
from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    os.environ["HTD_PROBE_REFACTOR_HISTORY"] = "1"
    directory = ROOT / ".verify/startup-memory" / (args.label + "-" + uuid4().hex)
    report: dict[str, Any] = {
        "label": args.label,
        "build": "source host + production Vite + WebView2",
        "scenarios": {},
    }
    with UiProbe(directory) as probe:
        probe.command("state")
        probe.wait("!!document.querySelector('.harness-page')")
        probe.script("window.__startupContinuity = crypto.randomUUID()")
        marker = probe.script("window.__startupContinuity")

        def sample(name: str, seconds: float = 6) -> None:
            result = measure(probe.process.pid, lambda: None, seconds)
            processes: list[dict[str, Any]] = []
            result["processes"] = processes
            for process in [
                psutil.Process(probe.process.pid),
                *psutil.Process(probe.process.pid).children(recursive=True),
            ]:
                try:
                    memory = process.memory_info()
                    processes.append(
                        {
                            "pid": process.pid,
                            "name": process.name(),
                            "args": [arg for arg in process.cmdline() if arg.startswith("--type=")],
                            "privateBytes": memory.private,
                            "workingSetBytes": memory.rss,
                        }
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            result["host"] = probe.command("host-memory")
            result["desktop"] = probe.command("state")
            if name == "settled":
                assert result["desktop"]["startupReclaimed"], "Startup reclamation never ran"
            if name != "tray":
                result["page"] = probe.script(
                    "({marker:window.__startupContinuity,heap:performance.memory?.usedJSHeapSize,"
                    "elements:document.querySelectorAll('*').length,route:location.hash})"
                )
                assert result["page"]["marker"] == marker
            report["scenarios"][name] = result
            print(name, json.dumps(result["processMetrics"]), flush=True)

        sample("cold")
        sample("settled", 12)
        probe.command("hide")
        sample("tray")
        probe.command("restore")
        sample("restored")
    destination = ROOT / f".verify/startup-memory-{args.label}.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
