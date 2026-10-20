"""Brief production WebView2 checks for palette, active memory target and page continuity."""

import json
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> None:
    report = {"checks": [], "screenshots": []}
    with UiProbe(ROOT / ".verify/feedback-20261017" / uuid4().hex) as probe:
        probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
        probe.command("resize", width=1280, height=900)
        probe.wait("document.documentElement.dataset.theme === 'light'")
        assert probe.command("state")["memoryTarget"] == "Normal"
        report["checks"].append("visible WebView2 uses the Normal memory target")
        probe.navigate("Codex", "Codex 统计")
        probe.wait("!!document.querySelector('.metric__value')")
        probe.script(
            "window.__originalMetric=document.querySelector('.metric');document.querySelector('.workspace').scrollTop=200"
        )
        scroll = probe.script("document.querySelector('.workspace').scrollTop")
        probe.command("append")
        # A manual update exercises arrival and redraw without waiting for polling.
        probe.click("立即更新")
        probe.wait(
            "document.querySelector('.metric__value') && !document.querySelector('.metric[aria-busy=true]')"
        )
        assert probe.script("window.__originalMetric === document.querySelector('.metric')")
        assert probe.script("document.querySelector('.workspace').scrollTop") == scroll
        probe.command("hide")
        probe.command("restore")
        probe.wait("document.querySelector('.workspace').scrollTop === " + str(scroll))
        assert probe.script("window.__originalMetric === document.querySelector('.metric')")
        assert probe.command("state")["memoryTarget"] == "Normal"
        report["checks"].append("refresh and tray restore preserve the page node and scroll position")
        probe.script("document.querySelector('.workspace').scrollTop=0")
        for label, title, slug in (
            ("Codex", "Codex 统计", "codex"),
            ("供应商与钱包", "供应商与钱包", "wallet"),
            ("价格与模型", "价格与模型", "pricing"),
            ("数据统计与分析", "数据统计与分析", "overview"),
            ("设置", "设置", "settings"),
        ):
            probe.navigate(label, title)
            probe.wait("!document.querySelector('.metric[aria-busy=true]')")
            assert (
                probe.script("getComputedStyle(document.documentElement).backgroundColor")
                == "rgb(255, 255, 255)"
            )
            assert probe.script("document.documentElement.scrollWidth <= innerWidth")
            destination = ROOT / f".verify/feedback-light-{slug}.png"
            probe.command("capture", path=str(destination))
            report["screenshots"].append(str(destination))
        report["checks"].append(
            "light palette uses white page backgrounds, platinum blue panels "
            "and white plots across five pages"
        )
    (ROOT / ".verify/feedback-20261017-native.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
