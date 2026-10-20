"""Verify the refactored production UI once in WebView2 with synthetic source data."""

import json
from typing import Any
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    directory = ROOT / ".verify/internal-refactor-native-runs" / uuid4().hex
    checks: list[str] = []
    report: dict[str, Any] = {"directory": str(directory), "checks": checks}
    try:
        with UiProbe(directory) as probe:
            probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
            for theme in ("light", "dark"):
                probe.navigate("设置", "设置")
                probe.input('select[aria-label="显示主题"]', theme, "change")
                probe.wait(f"document.documentElement.dataset.theme === '{theme}'")
                checks.append(f"{theme}: settings theme persisted and applied")
                probe.navigate("Codex", "Codex 统计")
                probe.wait("document.querySelectorAll('.metric').length === 6")
                assert probe.script(
                    "!document.querySelector('.toolbar') && !document.querySelector('.metrics-grid a')"
                    " && document.querySelectorAll('.cache-legend p').length === 2"
                )
                checks.append(f"{theme}: heading, six metrics, no pricing link, cache legends")
                probe.navigate("价格与模型", "价格与模型")
                probe.wait("!!document.querySelector('.price-editor input')")
                probe.input('input[aria-label="输入价格"]', "2.75")
                probe.click("倍率版本管理")
                probe.wait("!!document.querySelector('dialog[open]')")
                probe.click("取消", "document.querySelector('dialog[open]')")
                assert probe.script("document.querySelector('input[aria-label=\"输入价格\"]').value==='2.75'")
                probe.click("倍率版本管理")
                probe.wait("!!document.querySelector('dialog[open]')")
                probe.click("确认", "document.querySelector('dialog[open]')")
                probe.wait("!!document.querySelector('.version-list')")
                checks.append(f"{theme}: cancelled draft retained, confirmed version navigation")
                probe.click("价格组设置")
                probe.wait("!!document.querySelector('.group-detail')")
                checks.append(f"{theme}: group settings separated from prices")
                probe.navigate("供应商与钱包", "供应商与钱包")
                probe.wait("!!document.querySelector('.vendor-card')")
                assert probe.script("document.querySelectorAll('.vendor-card').length===1")
                probe.click("资金流水")
                probe.wait("!!document.querySelector('.entries')")
                probe.click("远程账户")
                probe.wait("!!document.querySelector('.remote-panel')")
                probe.click("编辑远程账户")
                probe.wait("!!document.querySelector('input[aria-label=\"令牌\"]')")
                probe.click("取消", "document.querySelector('dialog[open]')")
                probe.click("供应商设置")
                probe.wait("!!document.querySelector('.vendor-settings')")
                checks.append(f"{theme}: wallet / entries / remote dialog / vendor settings")
                if theme == "light":
                    probe.click("添加供应商")
                    probe.wait("!!document.querySelector('dialog[open]')")
                    probe.input('input[aria-label="名称"]', "Refactor new vendor")
                    probe.click("确认", "document.querySelector('dialog[open]')")
                    probe.wait(
                        "document.querySelector('.vendor-heading h2')?.textContent==='Refactor new vendor'"
                    )
                    checks.append("created vendor is immediately selected in the production wallet")
                assert probe.script("document.documentElement.scrollWidth<=window.innerWidth+1")
                checks.append(f"{theme}: no horizontal overflow in WebView2")
            probe.navigate("Codex", "Codex 统计")
            count_code = "document.querySelector('.metrics-grid').textContent"
            before = probe.script(count_code)
            probe.command("append")
            probe.click("立即更新")
            probe.wait(f"{count_code}!=={json.dumps(before)}")
            checks.append("new source request reaches the production page")
            probe.command("capture", path=str(ROOT / "docs/design/screens/internal-refactor-native.png"))
        report["ok"] = True
    except Exception as error:
        report.update(ok=False, error=str(error))
        raise
    finally:
        (ROOT / ".verify/internal-refactor-native.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
