"""Brief real desktop acceptance; isolated data and startup registry key only."""

import json
import time
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> None:
    report = {"checks": [], "screenshots": []}
    with UiProbe(ROOT / ".verify/feedback-current" / uuid4().hex, isolated_startup=True) as probe:
        probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
        probe.command("resize", width=1280, height=900)
        probe.wait("!!window.__htdFlushPreferences")
        probe.click("打开使用指南")
        probe.wait("!!document.querySelector('dialog[open]')")
        assert probe.script("[...document.querySelectorAll('dialog h3')].map(n=>n.textContent)")[:3] == [
            "扫描并添加模型池",
            "准备倍率版本",
            "编辑价格组并添加模型",
        ]
        probe.click("打开倍率版本管理", "document.querySelector('dialog[open]')")
        probe.wait("location.hash.split('?')[0] === '#/pricing/versions'")
        probe.wait(
            'document.querySelector(\'[aria-label="价格组二级导航"] '
            "[aria-current=page]')?.textContent.includes('倍率版本管理')"
        )
        probe.click("打开使用指南")
        probe.click("稍后设置", "document.querySelector('dialog[open]')")
        probe.click("不再显示")
        probe.wait("![...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='打开使用指南')")
        report["checks"].append("guide reopens, versions link selects its page, dismissal saves")
        probe.navigate("供应商与钱包", "供应商与钱包")
        probe.click("添加供应商")
        probe.input('dialog input[aria-label="名称"]', "Imported supplier")
        probe.input('dialog input[aria-label="过往充值金额"]', "123.456789")
        probe.click("确认", "document.querySelector('dialog[open]')")
        probe.wait("document.querySelector('.vendor-heading')?.textContent.includes('Imported supplier')")
        assert probe.script("document.querySelector('.vendor-card dl').textContent.includes('123.4568')")
        probe.click("供应商设置")
        probe.click("修改过往充值")
        probe.input('dialog input[aria-label="过往充值金额"]', "321.123456")
        probe.click("确认", "document.querySelector('dialog[open]')")
        probe.wait("document.querySelector('.vendor-settings').textContent.includes('321.1235')")
        probe.click("钱包概览")
        probe.wait("!!document.querySelector('.vendor-card')")
        destination = ROOT / ".verify/feedback-current-wallet-light.png"
        probe.command("capture", path=str(destination))
        report["screenshots"].append(str(destination))
        report["checks"].append("past funding creates and edits independently of the wallet balance")
        probe.script("location.hash='#/settings/background'")
        probe.wait(
            "document.querySelector('h1')?.textContent==='设置' && "
            "!!document.querySelector('.checkbox-field input')"
        )
        assert probe.script("[...document.querySelectorAll('.checkbox-field input')].map(e=>e.checked)") == [
            True,
            False,
        ]
        probe.script(
            "[...document.querySelectorAll('label')].find(l=>l.textContent.includes('开机自启')).querySelector('input').click()"
        )
        deadline = time.monotonic() + 5
        while not probe.command("startup-state")["enabled"]:
            assert time.monotonic() < deadline
        assert "python.exe" in probe.command("startup-state")["command"]
        probe.script(
            "[...document.querySelectorAll('label')].find(l=>l.textContent.includes('开机自启')).querySelector('input').click()"
        )
        deadline = time.monotonic() + 5
        while probe.command("startup-state")["enabled"]:
            assert time.monotonic() < deadline
        report["checks"].append(
            "startup defaults off; setting writes/removes the correct command in an isolated registry key"
        )
        probe.click("暂停采集")
        probe.navigate("Codex", "Codex 统计")
        probe.click("采集已暂停")
        probe.script("document.querySelector('.diagnostics a').click()")
        probe.wait(
            "location.hash.split('?')[0]==='#/settings/background' && "
            "document.querySelector('h1')?.textContent==='设置'"
        )
        probe.click("恢复采集")
        report["checks"].append("paused collection navigates to background settings and resumes")
        probe.navigate("Codex", "Codex 统计")
        probe.wait("!!document.querySelector('.metric')")
        probe.click("自定义")
        probe.script(
            "const input=document.querySelector('input[aria-label=\"开始时间\"]');"
            "input.value='2026-10-16 00:00';"
            "input.dispatchEvent(new Event('input',{bubbles:true}));"
            "input.dispatchEvent(new Event('blur',{bubbles:true}))"
        )
        probe.script("document.querySelectorAll('[aria-label=\"打开日历\"]')[1].click()")
        probe.click("随当前时间变动")
        probe.click("应用")
        probe.wait("document.querySelector('.custom-range').textContent.includes('随当前时间变动')")
        destination = ROOT / ".verify/feedback-current-custom-light.png"
        probe.command("capture", path=str(destination))
        report["screenshots"].append(str(destination))
        probe.script(
            "window.__originalMetric=document.querySelector('.metric');document.querySelector('.workspace').scrollTop=160"
        )
        scroll = probe.script("document.querySelector('.workspace').scrollTop")
        before = probe.command("state")
        probe.command("hide")
        deadline = time.monotonic() + 8
        while True:
            hidden = probe.command("state")
            if not hidden["visible"] and hidden["suspended"]:
                break
            assert time.monotonic() < deadline, hidden
        assert hidden["backgroundProcesses"] > 0
        assert any(item["eco"] and item["memoryPriority"] == 1 for item in hidden["processes"])
        probe.command("restore")
        probe.wait("window.__originalMetric===document.querySelector('.metric')")
        assert probe.script("document.querySelector('.workspace').scrollTop") == scroll
        restored = probe.command("state")
        assert restored["visible"] and not restored["suspended"] and restored["backgroundProcesses"] == 0
        assert restored["memoryTarget"] == "Normal"
        probe.click("立即更新")
        probe.wait("!document.querySelector('.metric[aria-busy=true]')")
        report["memorySnapshots"] = {"visible": before, "hidden": hidden, "restored": restored}
        report["checks"].append(
            "hidden WebView suspends with EcoQoS; restore preserves DOM, scroll, "
            "foreground updates and Normal target"
        )
        probe.navigate("设置", "设置")
        probe.click("显示")
        probe.input('select[aria-label="显示主题"]', "dark", "change")
        probe.wait("document.documentElement.dataset.theme==='dark'")
        probe.click("后台运行")
        destination = ROOT / ".verify/feedback-current-background-dark.png"
        probe.command("capture", path=str(destination))
        report["screenshots"].append(str(destination))
        probe.script(
            "[...document.querySelectorAll('label')].find(l=>l.textContent.includes('关闭窗口后进入托盘')).querySelector('input').click()"
        )
        probe.wait(
            "![...document.querySelectorAll('label')].find(l=>l.textContent.includes('关闭窗口后进入托盘')).querySelector('input').checked"
        )
        probe.command("hide")
        assert probe.process.wait(timeout=15) == 0
        report["checks"].append("unchecked close-to-tray setting exits cleanly after saving preferences")
    (ROOT / ".verify/feedback-current-native.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
