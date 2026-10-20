"""Exercise current feedback in an isolated production WebView2, using synthetic data."""
import json
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    checks = []
    report = {"checks": checks}
    try:
        with UiProbe(ROOT / ".verify/feedback-native" / uuid4().hex, synthetic_remote=True) as probe:
            probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
            def api(name, payload=None):
                probe.script(f"window.__result=null;pywebview.api.call({json.dumps(name)},{json.dumps(payload or {})}).then(r=>window.__result=r)")
                result = probe.wait("window.__result")
                assert result["ok"], result
                return result["data"]
            def preference(patch):
                snapshot = api("preferences.get")
                api("preferences.update", {"operationId": str(uuid4()), "expectedRevision": snapshot["revision"], "patch": patch})
            api("collection.scanNow", {"operationId": str(uuid4())})
            preference({"filters": {"codex": {"range": "30days"}}})
            probe.navigate("价格与模型", "价格与模型")
            probe.wait("!!document.querySelector('.version-list')")
            assert probe.script("[...document.querySelectorAll('.section-nav button')].map(x=>x.textContent).join('|').includes('倍率版本管理')")
            probe.click("价格编辑")
            probe.wait("!!document.querySelector('.rate-row')")
            assert not probe.script("!!document.querySelector('input[aria-label=\"展示名称\"]')")
            for theme in ("light", "dark"):
                preference({"theme": theme})
                probe.wait(f"document.documentElement.dataset.theme==={json.dumps(theme)}")
                probe.command("zoom", factor=1.25)
                assert probe.script("[...document.querySelectorAll('.rate-row')].every(x=>x.scrollWidth<=x.clientWidth+1)")
                probe.command("capture", path=str(ROOT / f".verify/feedback-pricing-{theme}.png"))
                probe.command("zoom", factor=1)
            checks.append("version-first navigation; model names removed; price fields fit at 125% in both themes")
            preference({"theme": "light"})
            probe.navigate("Codex", "Codex 统计")
            probe.wait("!!document.querySelector('.history-link')")
            probe.wait("!!document.querySelector('.monitor-panel .point')")
            probe.script("document.querySelector('.monitor-panel').scrollIntoView({block:'center'});document.querySelector('.monitor-panel .point').focus({preventScroll:true})")
            probe.wait("document.querySelector('[role=tooltip]')?.textContent.includes('缓存命中率')")
            probe.command("capture", path=str(ROOT / ".verify/feedback-cache-light.png"))
            assert not probe.script("!!document.querySelector('[aria-label=\"小时明细表\"]')")
            probe.click("在数据统计与分析中查看小时明细")
            probe.wait("document.querySelector('h1')?.textContent==='数据统计与分析'")
            probe.wait("document.querySelector('.grain-note')?.textContent.includes('天')")
            probe.click("查看小时明细")
            probe.wait("!!document.querySelector('.detail-controls select')")
            assert probe.script("!document.querySelector('[aria-label=\"趋势粒度\"] [data-value=\"hour\"]')")
            probe.command("capture", path=str(ROOT / ".verify/feedback-analysis.png"))
            probe.click("返回 Harness 统计")
            probe.wait("document.querySelector('h1')?.textContent==='Codex 统计'")
            checks.append("30-day Harness link carries context; daily trend and on-demand daily hours; return navigation")
            probe.navigate("供应商与钱包", "供应商与钱包")
            probe.click("资金流水")
            probe.wait("!!document.querySelector('.entries thead [aria-label=\"金额含义\"]')")
            assert not probe.script("!!document.querySelector('.entries tbody [aria-label=\"金额含义\"]')")
            probe.command("capture", path=str(ROOT / ".verify/feedback-wallet.png"))
            probe.click("远程账户")
            probe.click("编辑远程账户")
            probe.wait("!!document.querySelector('select[aria-label=\"接口类型\"]')")
            probe.input('select[aria-label="接口类型"]', "deepseek", "change")
            assert probe.script("document.querySelector('dialog[open]').textContent.includes('1:1')")
            probe.click("取消", "document.querySelector('dialog[open] .dialog__footer')")
            checks.append("wallet header help and explicit DeepSeek 1:1 calculation notice")
            probe.click("资金流水")
            vendor_id = api("wallet.list", {"includeArchived": False})["vendors"][0]["id"]
            probe.script(f"window.__filled=false;(async()=>{{for(let i=0;i<60;i++){{const r=await pywebview.api.call('wallet.topUp',{{vendorId:{json.dumps(vendor_id)},amount:'2',operationId:crypto.randomUUID()}});if(!r.ok)throw Error(JSON.stringify(r));}}window.__filled=true;}})()")
            probe.wait("window.__filled")
            probe.click("加载更多", "document.querySelector('.entries')")
            probe.wait("document.querySelectorAll('.entries tbody tr').length>50")
            probe.script("window.__table=document.querySelector('.entries .table-wrap');window.__table.scrollTop=300;window.__saved=window.__table.scrollTop;window.__outer=document.querySelector('.workspace');window.__outer.scrollTop=80;window.__outerSaved=window.__outer.scrollTop")
            api("wallet.topUp", {"vendorId": vendor_id, "amount": "2", "operationId": str(uuid4())})
            probe.wait("document.querySelectorAll('.entries tbody tr').length>60")
            position = probe.script("[window.__table.scrollTop,window.__saved,window.__outer.scrollTop,window.__outerSaved,document.querySelectorAll('.entries tbody tr').length,window.__table.isConnected,window.__outer.isConnected,document.querySelector('.entries .table-wrap')===window.__table,document.activeElement?.outerHTML.slice(0,300)]")
            assert position[0] == position[1] and abs(position[2] - position[3]) <= 1, position
            checks.append("background wallet refresh retains loaded pages and both scroll positions")
        report["ok"] = True
    except BaseException as error:
        report.update(ok=False, error=repr(error))
    (ROOT / ".verify/feedback-native.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
