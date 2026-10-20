"""Exercise categorized settings, vendor sites and notes in isolated production WebView2."""

import json
from typing import Any
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    directory = ROOT / ".verify/feedback-20261016-native-runs" / uuid4().hex
    checks: list[str] = []
    report: dict[str, Any] = {"directory": str(directory), "checks": checks}
    try:
        with UiProbe(directory, synthetic_remote=True) as probe:
            probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")

            def api(name: str, payload: dict[str, object] | None = None) -> Any:
                probe.script(
                    f"window.__feedbackResult=null;pywebview.api.call({json.dumps(name)},"
                    f"{json.dumps(payload or {})}).then(r=>window.__feedbackResult=r)"
                )
                result = probe.wait("window.__feedbackResult")
                assert result["ok"], result
                return result["data"]

            def preference(**patch: object) -> None:
                value = api("preferences.get")
                api(
                    "preferences.update",
                    {"operationId": str(uuid4()), "expectedRevision": value["revision"], "patch": patch},
                )

            api("collection.scanNow", {"operationId": str(uuid4())})
            probe.navigate("设置", "设置")
            probe.wait("!!document.querySelector('.harness-visibility')")
            assert not probe.script(
                "document.querySelector('.settings-card').textContent.includes('自动匹配供应商')"
            )
            probe.script("document.querySelector('input[aria-label=\"显示 Claude Code 页面\"]').click()")
            probe.wait("!document.querySelector('nav a[aria-label=\"Claude Code\"]')")
            probe.script("location.hash='/harness/claude'")
            probe.wait("document.querySelector('h1')?.textContent==='数据统计与分析'")
            assert api("preferences.get")["preferences"]["lastRoute"] == "/overview"
            checks.append("hidden Harness navigation falls back to overview and persists")

            preference(hiddenHarnesses=[])
            for theme in ("light", "dark"):
                preference(theme=theme)
                probe.wait(f"document.documentElement.dataset.theme==={json.dumps(theme)}")
                probe.navigate("设置", "设置")
                probe.click("显示", "document.querySelector('nav[aria-label=\"设置分类\"]')")
                probe.wait("!!document.querySelector('.harness-visibility')")
                assert probe.script("document.querySelectorAll('.settings-card').length") == 1
                for factor in (1, 1.25):
                    probe.command("zoom", factor=factor)
                    probe.wait("document.documentElement.scrollWidth<=innerWidth")
                probe.command("capture", path=str(ROOT / f".verify/feedback-settings-{theme}.png"))
                probe.command("zoom", factor=1)
                probe.click("指南与关于")
                probe.wait(
                    "[...document.querySelectorAll('button')].some(b=>b.textContent==='查看简要使用指南')"
                )
                probe.click("查看简要使用指南")
                probe.wait(
                    "document.querySelector('dialog[open]')?.textContent.includes('不代表准确的真实消耗')"
                )
                probe.script("document.querySelector('dialog[open] .dialog__footer button').click()")
                checks.append(f"{theme}: categorized settings, guide and compact layout")

            probe.navigate("供应商与钱包", "供应商与钱包")
            probe.click("添加供应商")
            probe.input('input[aria-label="名称"]', "DeepSeek 验收")
            probe.input('input[aria-label="供应商网址"]', "https://api.deepseek.com/v1/")
            probe.click("确认", "document.querySelector('dialog[open] .dialog__footer')")
            probe.wait("!document.querySelector('dialog[open]')")
            vendor = next(v for v in api("wallet.list")["vendors"] if v["name"] == "DeepSeek 验收")
            assert vendor["url"] == "https://api.deepseek.com"
            probe.click("远程账户")
            probe.click("编辑远程账户")
            probe.wait("!!document.querySelector('input[aria-label=\"站点地址\"]')")
            assert (
                probe.script("document.querySelector('input[aria-label=\"站点地址\"]').value")
                == vendor["url"]
            )
            probe.input('select[aria-label="接口类型"]', "deepseek", "change")
            probe.input('input[aria-label="令牌"]', "synthetic-deepseek-key")
            probe.click("测试连接")
            probe.wait("document.querySelector('.test-result')?.textContent.includes('连接成功')")
            assert not probe.script("!!document.querySelector('input[aria-label=\"用户 ID\"]')")
            for theme in ("light", "dark"):
                preference(theme=theme)
                probe.wait(f"document.documentElement.dataset.theme==={json.dumps(theme)}")
                probe.command("capture", path=str(ROOT / f".verify/feedback-deepseek-{theme}.png"))
            probe.click("取消", "document.querySelector('dialog[open] .dialog__footer')")
            checks.append(
                "vendor website is normalized and prefilled; DeepSeek UI uses API key (synthetic provider)"
            )

            daily = api(
                "wallet.createVendor",
                {"operationId": str(uuid4()), "name": "每日备注验收", "initialBalance": "100"},
            )
            for target in ("65.123456", "65.123456"):
                daily = api(
                    "wallet.calibrate",
                    {
                        "operationId": str(uuid4()),
                        "vendorId": daily["vendorId"],
                        "target": target,
                        "expectedWalletRevision": daily["revision"],
                    },
                )
            probe.script(f"location.hash={json.dumps('/wallet/' + daily['vendorId'])}")
            probe.wait(
                "document.querySelector('.entries')?.textContent.includes('含差额校准 -34.876544 USD')"
            )
            probe.command("capture", path=str(ROOT / ".verify/feedback-notes-dark.png"))
            probe.navigate("DSH", "Deepseek Harness 统计")
            probe.wait("document.querySelector('.cache-legend')?.textContent.includes('命中率低于 10%')")
            probe.command("capture", path=str(ROOT / ".verify/feedback-cache-dark.png"))
            probe.wait("location.hash.startsWith('#/harness/dsh')")
            assert api("preferences.get")["preferences"]["lastRoute"] == "/harness/dsh"
            probe.script("location.replace(location.href.split('#')[0])")
            probe.wait("document.querySelector('h1')?.textContent==='Deepseek Harness 统计'")
            checks.append(
                "signed daily calibration note and cache legend; reload without a hash restores saved page"
            )
        report["ok"] = True
    except Exception as error:
        report.update(ok=False, error=str(error))
        raise
    finally:
        (ROOT / ".verify/feedback-20261016-native.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
