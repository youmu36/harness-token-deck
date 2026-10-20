"""Inspect price control geometry at the supported minimum native window size."""

import argparse
import json
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--record-only", action="store_true")
    args = parser.parse_args()
    directory = ROOT / ".verify/price-layout" / uuid4().hex
    report = {"directory": str(directory), "label": args.label, "cases": []}
    output = ROOT / f".verify/price-layout-{args.label}.json"
    try:
        with UiProbe(directory) as probe:
            probe.wait("!!document.querySelector('.metrics-grid')")
            probe.click("监测中")
            probe.wait(
                "document.querySelector('[aria-label=\"采集诊断\"]')?.textContent.includes('完成后会自动补齐')"
            )
            probe.navigate("价格与模型", "价格与模型")
            probe.wait("!!document.querySelector('.version-list')")
            probe.click("价格编辑")
            probe.wait("!!document.querySelector('.rate-row')")
            for theme in ("light", "dark"):
                probe.script(f"document.documentElement.dataset.theme={json.dumps(theme)}")
                for width in (1024, 1280):
                    probe.command("resize", width=width, height=680)
                    for zoom in (1, 1.25, 1.5):
                        probe.command("zoom", factor=zoom)
                        probe.script(
                            "document.querySelector('.rate-row').scrollIntoView("
                            "{block:'center',behavior:'instant'})"
                        )
                        measured = probe.script("""(() => {
                            const row=document.querySelector('.rate-row');
                            const boxes=[...row.querySelectorAll('.row-fields input')].map(input=>{
                                const box=input.getBoundingClientRect();
                                return {name:input.getAttribute('aria-label'),x:box.x,y:box.y,
                                        width:box.width,height:box.height};
                            });
                            const model=row.querySelector('.model-select').getBoundingClientRect();
                            return {viewport:innerWidth,boxes,modelWidth:model.width,
                                    rowWidth:row.clientWidth,rowScrollWidth:row.scrollWidth,
                                    pageOverflow:document.documentElement.scrollWidth>innerWidth,
                                    sameRow:boxes.every(box=>Math.abs(box.y-boxes[0].y)<1)};
                        })()""")
                        measured.update(theme=theme, windowWidth=width, windowHeight=680, zoom=zoom)
                        report["cases"].append(measured)
                        if width == 1024 and zoom in (1, 1.25):
                            probe.command("capture", path=str(directory / f"{theme}-{width}-{zoom}.png"))
                        if not args.record_only:
                            assert len(measured["boxes"]) == 4 and measured["sameRow"], measured
                            assert measured["modelWidth"] <= 241, measured
                            assert not measured["pageOverflow"], measured
                            assert measured["rowScrollWidth"] <= measured["rowWidth"] + 1, measured
            report["ok"] = all(case["sameRow"] for case in report["cases"])
    except Exception as error:
        report.update(ok=False, error=str(error))
        raise
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"ok": report.get("ok"), "directory": str(directory), "cases": len(report["cases"])},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
