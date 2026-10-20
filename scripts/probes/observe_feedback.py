"""Timed isolated native observation of background stability and tray collection."""

import argparse
import json
import time
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=10)
    args = parser.parse_args()
    directory = ROOT / ".verify/feedback-observation" / uuid4().hex
    report = {"minutes": args.minutes, "directory": str(directory), "samples": []}
    output = ROOT / ".verify/feedback-observation.json"
    try:
        with UiProbe(directory) as probe:
            probe.wait(
                "!!document.querySelector('.metrics-grid') && "
                "!document.querySelector('.metrics-grid .skeleton')"
            )
            probe.script("""
                window.__stability = {skeletons:0,removed:0,scrollChanges:0,calls:0};
                window.__stableNodes = [...document.querySelectorAll('main,h1,.metrics-grid,.monitor-panel')];
                const original = pywebview.api.call;
                pywebview.api.call = function(name,payload){
                    if(name.startsWith('stats.'))window.__stability.calls++;
                    return original(name,payload)};
                new MutationObserver(records=>{for(const record of records){
                    for(const node of record.addedNodes){
                        if(node.nodeType===1 && (node.matches('.skeleton')||node.querySelector('.skeleton')))
                            window.__stability.skeletons++}
                } window.__stability.removed=window.__stableNodes.filter(n=>!n.isConnected).length;})
                    .observe(document.querySelector('main'),{childList:true,subtree:true});
                document.querySelector('main').scrollTop=80;
                window.__stableScroll=document.querySelector('main').scrollTop;
                document.querySelector('main').addEventListener('scroll',()=>{
                    if(Math.abs(document.querySelector('main').scrollTop-window.__stableScroll)>1)window.__stability.scrollChanges++;
                });
            """)
            probe.command("background")
            report["backgroundState"] = probe.command("state")
            assert report["backgroundState"]["visible"] and not report["backgroundState"]["focused"]
            started = time.monotonic()
            next_append = started
            while time.monotonic() - started < args.minutes * 60:
                if time.monotonic() >= next_append:
                    probe.command("append")
                    next_append += 60
                sample = probe.script(
                    "({...window.__stability,connected:window.__stableNodes.every(n=>n.isConnected),alerts:[...document.querySelectorAll('[role=alert]')].map(n=>n.textContent)})"
                )
                sample["elapsedSeconds"] = round(time.monotonic() - started, 1)
                report["samples"].append(sample)
                assert (
                    sample["connected"]
                    and sample["skeletons"] == sample["removed"] == sample["scrollChanges"] == 0
                ), sample
                # Persist partial evidence while the native window stays open.
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                time.sleep(10)
            probe.command("capture", path=str(directory / "background-end.png"))
            probe.command("hide")
            assert not probe.command("state")["visible"]
            probe.command("append")
            time.sleep(35)
            probe.command("restore")
            probe.wait("!!document.querySelector('.metrics-grid')")
            probe.script(
                "window.__sourceStatus=null;pywebview.api.call('collection.status',{}).then(r=>window.__sourceStatus=r)"
            )
            status = probe.wait("window.__sourceStatus")
            assert status["ok"]
            codex = next(source for source in status["data"]["sources"] if source["harness"] == "codex")
            report["codexAfterTray"] = codex
            assert codex["ledgerCount"] >= args.minutes + 2, codex
            report["restoredStability"] = probe.script("window.__stability")
            probe.command("capture", path=str(directory / "tray-restored.png"))
            report["ok"] = True
    except Exception as error:
        report.update(ok=False, error=str(error))
        raise
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
