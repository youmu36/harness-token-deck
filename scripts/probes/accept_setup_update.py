"""Review this update in isolated production WebView2, with synthetic remote credentials."""

import json
from typing import Any
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    directory = ROOT / ".verify/setup-native-runs" / uuid4().hex
    checks: list[str] = []
    report: dict[str, Any] = {"directory": str(directory), "checks": checks}
    try:
        with UiProbe(directory, synthetic_remote=True) as probe:
            probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")

            def api(name: str, payload: dict[str, object]) -> Any:
                probe.script(
                    f"window.__setupResult=null;pywebview.api.call({json.dumps(name)},{json.dumps(payload)})"
                    ".then(r=>window.__setupResult=r)"
                )
                response = probe.wait("window.__setupResult")
                assert response["ok"], response
                return response["data"]

            for theme in ("light", "dark"):
                probe.navigate("设置", "设置")
                probe.input('select[aria-label="显示主题"]', theme, "change")
                probe.wait(f"document.documentElement.dataset.theme === '{theme}'")
                probe.click("查看简要使用指南")
                probe.wait("!!document.querySelector('.setup-guide')")
                headings = probe.script(
                    "[...document.querySelectorAll('.setup-guide h3')].map(h=>h.textContent)"
                )
                assert headings == [
                    "扫描并添加模型池",
                    "创建价格组并添加模型",
                    "检查倍率版本管理",
                    "添加供应商与钱包",
                ]
                assert not probe.script("document.documentElement.scrollWidth>innerWidth")
                probe.command("capture", path=str(ROOT / f".verify/setup-guide-{theme}.png"))
                probe.script("document.querySelector('dialog[open] button[aria-label=\"关闭\"]').click()")
                checks.append(f"{theme}: reusable guide order and layout")

            probe.navigate("价格与模型", "价格与模型")
            probe.wait("!!document.querySelector('.price-editor')")
            assert probe.script(
                "document.querySelector('.setup-hint').textContent === '先前往模型池添加模型，再编辑价格组'"
            )
            probe.script(
                "[...document.querySelectorAll('.price-editor button')].filter(b=>b.textContent"
                ".includes('添加模型行')).at(-1).click()"
            )
            probe.wait("document.querySelectorAll('.rate-row').length===4")
            assert probe.script(
                "[...Array.from(document.querySelectorAll('.rate-row')).at(-1).querySelectorAll('option')].some(o=>o.disabled&&o.textContent.includes('已添加'))"
            )
            checks.append("bottom add button and duplicate models disabled")
            probe.navigate("供应商与钱包", "价格与模型")
            probe.wait("!!document.querySelector('dialog[open]')")
            probe.script("document.querySelector('dialog[open] .button--danger').click()")
            probe.wait("document.querySelector('h1').textContent==='供应商与钱包'")
            probe.click("远程账户")
            assert not probe.script(
                "[...document.querySelectorAll('button')].some(b=>b.textContent==='配置远程账户')"
            )
            probe.click("编辑远程账户")
            probe.wait("!!document.querySelector('#remote-account')")
            assert not probe.script("!!document.querySelector('option[value=auto]')")
            probe.click("保存", "document.querySelector('dialog[open]')")
            probe.wait(
                "document.querySelector('dialog[open] [role=alert]')?.textContent.includes('有必填项未完成')"
            )
            assert probe.script("document.querySelectorAll('#remote-account [aria-required=true]').length>=3")
            checks.append("single remote entry, mandatory protocol and required-field reminder")
            for protocol in ("newapi", "sub2api"):
                probe.input('input[aria-label="站点地址"]', "https://example.test")
                probe.input('select[aria-label="接口类型"]', protocol, "change")
                if protocol == "newapi":
                    probe.input('input[aria-label="用户 ID"]', "42")
                probe.input('input[aria-label="令牌"]', "  Bearer synthetic-setup-abcd  ")
                probe.click("测试连接", "document.querySelector('dialog[open]')")
                probe.wait("document.querySelector('.test-result')?.textContent.includes('连接成功')")
                probe.wait(
                    "(() => {const b=document.querySelector('dialog[open] .dialog__body');"
                    "return b.scrollHeight-b.clientHeight-b.scrollTop<3})()"
                )
                probe.command("capture", path=str(ROOT / f".verify/setup-remote-{protocol}.png"))
                probe.click("保存", "document.querySelector('dialog[open]')")
                probe.wait("!document.querySelector('dialog[open]')")
                vendor = api("wallet.list", {})["vendors"][0]["id"]
                account = api("remote.get", {"vendorId": vendor})["account"]
                assert account["siteType"] == protocol
                assert account["authMode"] == ("access_token" if protocol == "newapi" else "api_key")
                assert account["maskedCredential"] == "••••abcd"
                checks.append(f"{protocol}: normalized credentials, fixed auth, scroll and native save")
                if protocol == "newapi":
                    probe.click("编辑远程账户")
                    probe.wait("!!document.querySelector('#remote-account')")

            probe.script("location.hash='/models'")
            probe.wait("document.querySelector('h1').textContent==='模型池'")
            probe.wait("!document.querySelector('.shell-error [role=alert]')")
            assert api("preferences.get", {})["preferences"]["lastRoute"] == "/models"
            probe.script("history.replaceState(null,'',location.pathname+location.search);location.reload()")
            probe.wait("document.querySelector('h1')?.textContent==='模型池'")
            checks.append("model-pool path saved and restored without a startup hash")

            preferences = api("preferences.get", {})
            api(
                "preferences.update",
                {
                    "patch": {"guideSeen": False},
                    "expectedRevision": preferences["revision"],
                    "operationId": str(uuid4()),
                },
            )
            probe.script("location.reload()")
            probe.wait("document.querySelector('dialog[open] h2')?.textContent==='欢迎使用 · 简要使用指南'")
            probe.click("开始设置", "document.querySelector('dialog[open]')")
            probe.wait("!document.querySelector('dialog[open]')")
            assert api("preferences.get", {})["preferences"]["guideSeen"]
            probe.script("location.reload()")
            probe.wait(
                "document.querySelector('h1')?.textContent==='模型池'"
                " && !document.querySelector('dialog[open]')"
            )
            checks.append("welcome guide completion persists and next startup stays quiet")
        report["ok"] = True
    except Exception as error:
        report.update(ok=False, error=str(error))
        raise
    finally:
        (ROOT / ".verify/setup-native.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
