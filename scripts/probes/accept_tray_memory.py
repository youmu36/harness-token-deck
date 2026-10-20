"""Bounded comparison of direct close, minimization and tray restore in an isolated app."""

import argparse
import json
import time
from typing import Any
from uuid import uuid4

import psutil

from scripts.probes.ui_probe import ROOT, UiProbe


def sample(probe: UiProbe) -> dict[str, Any]:
    state = probe.command("state")
    for row in state["processes"]:
        try:
            memory = psutil.Process(row["pid"]).memory_info()
            row["workingSetMiB"] = round(memory.rss / 1024**2, 2)
            row["privateMiB"] = round(memory.private / 1024**2, 2)
        except psutil.Error:
            row["exited"] = True
    return state


def settled(probe: UiProbe) -> dict[str, Any]:
    time.sleep(10)
    return sample(probe)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="tray-memory-native.json")
    args = parser.parse_args()
    report: dict[str, Any] = {}
    with UiProbe(ROOT / ".verify/tray-memory" / uuid4().hex) as probe:
        probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
        probe.navigate("数据统计与分析", "数据统计与分析")
        probe.wait("!!document.querySelector('.metric')")
        probe.script(
            "window.__originalMetric=document.querySelector('.metric');"
            "document.querySelector('.workspace').scrollTop=180"
        )
        probe.script(
            "window.__powerEvents=[]; for(const type of ['blur','focus','visibilitychange']) "
            "window.addEventListener(type,()=>window.__powerEvents.push({type,"
            "visibility:document.visibilityState,focused:document.hasFocus()}),true)"
        )
        scroll = probe.script("document.querySelector('.workspace').scrollTop")
        report["visible"] = settled(probe)
        probe.command("hide")
        report["directTray"] = settled(probe)
        assert not report["directTray"]["visible"]
        assert report["directTray"]["suspended"], report["directTray"]
        probe.command("restore")
        probe.wait("window.__originalMetric===document.querySelector('.metric')")
        report["directEvents"] = probe.script("window.__powerEvents")
        assert probe.script("document.querySelector('.workspace').scrollTop") == scroll
        report["restored"] = settled(probe)
        probe.command("minimize")
        probe.command("background")
        report["minimized"] = settled(probe)
        probe.command("hide")
        report["minimizedTray"] = settled(probe)
        probe.command("restore")
        probe.wait("window.__originalMetric===document.querySelector('.metric')")
        assert probe.script("document.querySelector('.workspace').scrollTop") == scroll
        report["finalRestore"] = sample(probe)
        assert report["finalRestore"]["visible"] and not report["finalRestore"]["suspended"]
        assert report["finalRestore"]["backgroundProcesses"] == 0
    (ROOT / ".verify" / args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
