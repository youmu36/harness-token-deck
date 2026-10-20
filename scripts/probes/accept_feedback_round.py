"""Verify the seven feedback changes in the production WebView2 UI with isolated data."""

import json
from typing import Any
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    directory = ROOT / ".verify/oct14-native-runs" / uuid4().hex
    checks: list[str] = []
    report: dict[str, Any] = {"directory": str(directory), "checks": checks}
    try:
        with UiProbe(directory, synthetic_remote=True) as probe:
            probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
            report["navigation"] = probe.script(
                '[...document.querySelectorAll(\'nav[aria-label="主导航"] section:first'
                "-child a')].map(a=>a.getAttribute('aria-label'))"
            )
            assert report["navigation"] == ["Claude Code", "Codex", "DSH", "Zcode"], report["navigation"]
            checks.append("Harness navigation A-Z")
            for theme in ("light", "dark"):
                probe.navigate("设置", "设置")
                probe.input('select[aria-label="显示主题"]', theme, "change")
                probe.wait(f"document.documentElement.dataset.theme === '{theme}'")
                for name in ("Claude Code", "Codex"):
                    probe.navigate(name, name + " 统计")
                    probe.wait("document.querySelector('.balance-main')?.textContent.includes('余额\\uFF1A')")
                    probe.wait("!!document.querySelector('.bar .trend-fill')")
                    layout = probe.script(
                        "(() => { const bg=document.querySelector('.plot-background');const"
                        " fill=document.querySelector('.trend-fill');const balance=document"
                        ".querySelector('.balance-card');return {plotHeight:Number(bg.getAt"
                        "tribute('height')),titleSize:getComputedStyle(document.querySelect"
                        "or('h1')).fontSize,balanceSize:getComputedStyle(balance).fontSize,"
                        "barFill:getComputedStyle(fill).backgroundImage,barShadow:"
                        "getComputedStyle(fill).boxShadow,headerHeight:docu"
                        "ment.querySelector('.usage-header').getBoundingClientRect().height"
                        ",overflow:document.documentElement.scrollWidth>innerWidth};})()"
                    )
                    assert (
                        layout["plotHeight"] == 180
                        and layout["titleSize"] == "28px"
                        and layout["balanceSize"] == "13px"
                        and not layout["overflow"]
                    ), layout
                    assert "linear-gradient" in layout["barFill"] and layout["barShadow"] != "none", layout
                    checks.append(
                        f"{theme}/{name}: 180px plot, layered gradient bars, compact heading and balance"
                    )
                    probe.command(
                        "capture", path=str(ROOT / f".verify/oct14-{theme}-{name.replace(' ', '-')}.png")
                    )
            probe.navigate("Codex", "Codex 统计")
            probe.click("自定义")
            probe.wait("!!document.querySelector('input[placeholder=\"YYYY-MM-DD HH:mm\"]')")
            probe.script("document.querySelector('button[aria-label=\"打开日历\"]').click()")
            probe.wait("!!document.querySelector('button[aria-label=\"分钟\"]')")
            assert probe.script(
                "/^\\d{2}$/.test(document.querySelector('button[aria-label=\"分钟\"]').textContent)"
            )
            probe.script("document.querySelector('button[aria-label=\"分钟\"]').click()")
            probe.wait("!!document.querySelector('.time-list .options')")
            assert probe.script(
                "(() => {const list=document.querySelector('.time-list .options');c"
                "onst last=list.querySelector('[data-index=\"119\"]');const next=last"
                ".nextElementSibling;return last.textContent==='59'&&next.textConte"
                "nt==='00';})()"
            )
            probe.script(
                "(() => {const list=document.querySelector('.time-list .options');l"
                "ist.scrollTop=4100;list.dispatchEvent(new Event('scroll'));})()"
            )
            assert probe.script("document.querySelector('.time-list .options').scrollTop<4100")
            checks.append("digit-only time buttons and continuous 59 → 00 scroll")
            probe.script("document.querySelector('.time-list [data-index=\"60\"]').click()")
            probe.script("document.querySelector('button[aria-label=\"打开日历\"]').click()")
            probe.navigate("供应商与钱包", "供应商与钱包")
            probe.click("扫描供应商")
            probe.wait("document.querySelectorAll('.provider-result').length>0")
            assert probe.script(
                "[...document.querySelectorAll('dialog[open] button')].find(b=>b.te"
                "xtContent.startsWith('创建选中供应商')).disabled"
            )
            probe.script(
                "(() => {let i=0;for(const e of document.querySelectorAll('.provide"
                "r-result input:not([type=checkbox])')){if(!e.disabled){e.value='Na"
                "tive provider '+(++i);e.dispatchEvent(new Event('input',{bubbles:t"
                "rue}));}}})()"
            )
            probe.click("创建选中供应商", "document.querySelector('dialog[open]')")
            probe.wait("!document.querySelector('dialog[open]')")
            checks.append("recent supplier scan requires names and creates wallets")
            probe.click("远程账户")
            for mode in ("api_key", "access_token"):
                probe.click("编辑远程账户")
                probe.wait("!!document.querySelector('input[aria-label=\"令牌\"]')")
                probe.input('input[aria-label="站点地址"]', "https://example.test")
                probe.input(
                    'select[aria-label="接口类型"]', "sub2api" if mode == "api_key" else "newapi", "change"
                )
                if mode == "access_token":
                    probe.wait("!!document.querySelector('input[aria-label=\"用户 ID\"]')")
                    probe.input('input[aria-label="用户 ID"]', "1")
                probe.input('input[aria-label="令牌"]', "  Bearer sk-synthetic-abcd  ")
                probe.click("测试连接", "document.querySelector('dialog[open]')")
                probe.wait("document.querySelector('dialog[open]')?.textContent.includes('连接成功')")
                probe.click("保存", "document.querySelector('dialog[open]')")
                probe.wait("!document.querySelector('dialog[open]')")
                checks.append(f"{mode}: pasted token tested and saved through native IPC")
            assert probe.script("!document.querySelector('.shell-error [role=alert]')")
            checks.append("no unexplained settings conflict banner")
        report["ok"] = True
    except Exception as error:
        report.update(ok=False, error=str(error))
        raise
    finally:
        (ROOT / ".verify/oct14-native.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
