"""Check the final production chart and tray continuity using isolated fixture data."""

# ruff: noqa: RUF001 -- Exact UI labels are used as DOM selectors.

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> None:
    report: dict[str, Any] = {"ranges": []}
    with UiProbe(ROOT / ".verify/tray-trend" / uuid4().hex) as probe:
        probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
        probe.navigate("数据统计与分析", "数据统计与分析")
        probe.wait("!!document.querySelector('.grain-note')")
        probe.click("自定义")
        end = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1
        )
        for span, note, count in (
            (timedelta(hours=72, minutes=-1), "小时", 72),
            (timedelta(hours=72), "2小时", 36),
            (timedelta(days=7), "2小时", 84),
            (timedelta(days=7, minutes=1), "天", 8),
        ):
            start = (end - span).strftime("%Y-%m-%d %H:%M")
            probe.input('input[aria-label="开始时间"]', start)
            probe.input('input[aria-label="结束时间"]', end.strftime("%Y-%m-%d %H:%M"))
            probe.click("应用", "document.querySelector('.custom-range')")
            probe.wait(f"document.querySelector('.grain-note')?.textContent==='实际粒度：{note}'")
            probe.wait(f"document.querySelectorAll('.trend-panel .point').length==={count}")
            assert probe.script(
                '!!document.querySelector(\'[aria-label="趋势粒度"] [data-value="hour"]\')'
            ) == (note != "天")
            report["ranges"].append({"start": start, "note": note, "bars": count})
            if count == 84:
                probe.script(
                    "document.querySelector('.trend-panel').scrollIntoView();"
                    "document.querySelector('.trend-panel [data-point=\"83\"]').focus()"
                )
                tooltip = probe.wait(
                    "document.querySelector('.trend-panel [role=tooltip] strong')?.textContent"
                )
                expected = f"{end - timedelta(hours=2):%m-%d %H:%M} 至 {end:%m-%d %H:%M}"
                assert tooltip == expected, tooltip
                report["tooltip"] = tooltip
                screenshot = ROOT / ".verify/tray-trend-two-hours.png"
                probe.command("capture", path=str(screenshot))
                report["screenshot"] = str(screenshot)
        probe.script("window.__trendNode=document.querySelector('.trend-panel')")
        probe.command("hide")
        probe.command("restore")
        probe.wait("window.__trendNode===document.querySelector('.trend-panel')")
        state = probe.command("state")
        assert state["visible"] and not state["suspended"] and state["memoryTarget"] == "Normal"
        report["restored"] = state
    (ROOT / ".verify/tray-trend-native.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
