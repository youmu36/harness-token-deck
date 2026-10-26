"""Isolated, equal-input page/collection memory samples; no forced garbage collection."""

import argparse
import json
import os
import platform
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.probes.data_refactor_runtime import measure
from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--requests", type=int, default=80)
    args = parser.parse_args()
    if args.seconds < 1 or not 1 <= args.requests <= 1000:
        parser.error("seconds >= 1 and requests in 1..1000 required")
    directory = ROOT / ".verify/pages-memory" / (args.label + "-" + uuid4().hex)
    os.environ["HTD_PROBE_REFACTOR_HISTORY"] = "1"
    report: dict[str, Any] = {
        "label": args.label,
        "machine": platform.platform(),
        "build": "source host + production Vite + WebView2",
        "input": {"historicalRequests": 5760, "newRequests": args.requests * 4 + 4},
        "secondsPerPage": args.seconds,
        "scenarios": {},
    }
    with UiProbe(directory) as probe:
        probe.command("state")
        probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
        probe.script("""window.__memoryCalls={}; window.__memoryErrors=[];
            const call=pywebview.api.call;
            pywebview.api.call=(name,payload)=>{
              window.__memoryCalls[name]=(window.__memoryCalls[name]||0)+1;
              return call(name,payload).then(result=>{
                if (!result.ok) window.__memoryErrors.push({name,error:result.error});
                return result;
              });
            };""")

        def sample(name: str) -> None:
            probe.script("window.__memoryCalls={}")
            result = measure(probe.process.pid, lambda: None, args.seconds)
            result["page"] = probe.script("""({route:location.hash,
              elements:document.querySelectorAll('*').length,
              points:document.querySelectorAll('[data-point]').length,
              foreignObjects:document.querySelectorAll('foreignObject').length,
              heap:performance.memory ? performance.memory.usedJSHeapSize : null,
              calls:window.__memoryCalls, errors:window.__memoryErrors})""")
            result["host"] = probe.command("host-memory")
            if name in ("harness-populated", "analysis-cache"):
                probe.command("capture", path=str(directory / (name + ".png")))
            report["scenarios"][name] = result
            print(name, json.dumps(result["processMetrics"]), flush=True)

        sample("harness-cold")
        for _ in range(args.requests):
            probe.command("append")
        probe.script(
            "pywebview.api.call('collection.scanNow',{operationId:crypto.randomUUID()})"
            ".then(r=>window.__memoryScan=r)"
        )
        assert probe.wait("window.__memoryScan")["ok"]
        probe.wait("!!document.querySelector('[aria-label=\"近期缓存监控\"]')")
        sample("harness-populated")

        def route(path: str, selector: str) -> None:
            probe.script("location.hash=" + json.dumps(path))
            probe.wait("!!document.querySelector(" + json.dumps(selector) + ")")

        route("/overview", ".overview-page")
        sample("statistics")
        probe.click("数据分析")
        sample("analysis-tps")
        probe.click("缓存")
        sample("analysis-cache")
        route("/wallet", ".wallet-page")
        probe.wait("!!document.querySelector('[aria-label=\"供应商列表\"] button')")
        probe.click("资金流水")
        sample("wallet-flows")
        for index in range(3):
            route("/harness/codex", ".harness-page")
            sample(f"harness-return-{index + 1}")
            route("/overview", ".overview-page")
            sample(f"analysis-return-{index + 1}")
        route("/harness/codex", ".harness-page")
        probe.click("查看小时明细")
        sample("hours-open")
        probe.click("收起小时明细")
        sample("hours-closed")
        probe.command("hide")
        report["scenarios"]["tray"] = measure(probe.process.pid, lambda: None, args.seconds)
        probe.command("restore")
        sample("restored")
    with sqlite3.connect(directory / "htd.sqlite3") as database:
        count = database.execute("SELECT COUNT(*) FROM identity").fetchone()[0]
        report["acceptedRequests"] = count
        assert count == 5760 + args.requests * 4 + 4, count
        assert database.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
    report["ok"] = True
    destination: Path = ROOT / f".verify/pages-memory-{args.label}.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
