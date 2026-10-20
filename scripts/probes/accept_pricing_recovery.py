"""Check current pricing feedback and scroll stability in an isolated production WebView2."""

import json
from typing import Any
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    directory = ROOT / ".verify/pricing-recovery-native" / uuid4().hex
    checks: list[str] = []
    report: dict[str, Any] = {"directory": str(directory), "checks": checks}
    try:
        with UiProbe(directory, synthetic_remote=True) as probe:
            probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")

            def api(name: str, payload: dict[str, object] | None = None) -> Any:
                probe.script(
                    f"window.__updateResult=null;pywebview.api.call({json.dumps(name)},"
                    f"{json.dumps(payload or {})}).then(r=>window.__updateResult=r)"
                )
                result = probe.wait("window.__updateResult")
                assert result["ok"], result
                return result["data"]

            def preference(**patch: object) -> None:
                snapshot = api("preferences.get")
                api(
                    "preferences.update",
                    {"operationId": str(uuid4()), "expectedRevision": snapshot["revision"], "patch": patch},
                )

            api("collection.scanNow", {"operationId": str(uuid4())})
            probe.navigate("价格与模型", "价格与模型")
            probe.script("location.hash='/models'")
            probe.wait("!!document.querySelector('.models-tab tbody input')")
            assert probe.script(
                "[...document.querySelectorAll('.models-tab th')].some(x=>x.textContent==='缓存创建价格')"
            )
            probe.input('input[aria-label="gpt-6-astra 缓存创建价格"]', "1.25")
            probe.click("保存全部修改")
            probe.wait("!document.querySelector('.models-tab tr.dirty')")
            pool = api("models.pool")
            assert (
                next(x for x in pool["models"] if x["modelId"] == "gpt-6-astra")["rates"]["cacheWrite"]
                == "1.25"
            )
            for theme in ("light", "dark"):
                preference(theme=theme)
                probe.wait(f"document.documentElement.dataset.theme==={json.dumps(theme)}")
                probe.command("capture", path=str(ROOT / f".verify/update-models-{theme}.png"))
                probe.command("zoom", factor=1.25)
                probe.wait("document.documentElement.scrollWidth<=innerWidth")
                probe.command("capture", path=str(ROOT / f".verify/update-models-{theme}-125.png"))
                probe.command("zoom", factor=1)
            checks.append("four model-pool columns save and reload exact creation prices; light/dark layout")

            catalog = api("pricing.catalog")
            g = catalog["groups"][0]
            v = next(v for v in g["versions"] if v["current"])
            incomplete = [{**m, "input": None} for m in v["models"]]
            api(
                "pricing.saveVersionPrices",
                {
                    "operationId": str(uuid4()),
                    "expectedRevision": catalog["revision"],
                    "versionId": v["id"],
                    "models": incomplete,
                },
            )
            probe.command("append")
            api("collection.scanNow", {"operationId": str(uuid4())})
            probe.script("location.hash='/unpriced'")
            probe.wait("!!document.querySelector('.unpriced-tab tbody tr')")
            pending = api("pricing.unpriced")
            assert pending["recordCount"] > 0 and pending["readyCount"] == 0
            probe.click("前往补价")
            probe.wait("!!document.querySelector('.price-editor .rate-row')")
            assert (
                probe.script("document.querySelector('select[aria-label=\"编辑价格版本\"]').value") == v["id"]
            )
            probe.input('input[aria-label="输入价格"]', "2")
            probe.input('input[aria-label="缓存创建价格"]', "3")
            probe.click("保存（追加修订）")  # noqa: RUF001
            probe.wait(
                "document.querySelector('.price-editor footer').textContent.includes('未保存修改 0 项')"
            )
            probe.script("location.hash='/unpriced'")
            probe.wait("document.querySelector('.unpriced-tab .button--primary')?.disabled===false")
            for theme in ("light", "dark"):
                preference(theme=theme)
                probe.wait(f"document.documentElement.dataset.theme==={json.dumps(theme)}")
                probe.command("capture", path=str(ROOT / f".verify/update-unpriced-{theme}.png"))
                probe.command("zoom", factor=1.25)
                probe.wait("document.documentElement.scrollWidth<=innerWidth")
                probe.command("capture", path=str(ROOT / f".verify/update-unpriced-{theme}-125.png"))
                probe.command("zoom", factor=1)
            probe.click("一键重算已补价请求")
            probe.wait("document.querySelector('.unpriced-tab').textContent.includes('已重算')")
            assert api("pricing.unpriced")["recordCount"] < pending["recordCount"]
            checks.append(
                "unpriced list, original-version editing, and recovery of filled requests only"
            )

            probe.navigate("供应商与钱包", "供应商与钱包")
            probe.click("资金流水")
            probe.wait("!!document.querySelector('.entries button[aria-label=\"备注含义\"]')")
            probe.script("document.querySelector('.entries button[aria-label=\"备注含义\"]').focus()")
            probe.wait("document.querySelector('[role=tooltip]')?.textContent.includes('差额校准')")
            probe.command("capture", path=str(ROOT / ".verify/update-wallet-hint.png"))
            checks.append(
                "amount/note hints explain signed calibration and net consumption with keyboard access"
            )
            probe.click("远程账户")
            probe.click("编辑远程账户")
            probe.wait("!!document.querySelector('select[aria-label=\"接口类型\"]')")
            probe.input('select[aria-label="接口类型"]', "deepseek", "change")
            probe.wait(
                "document.querySelector('input[aria-label=\"站点地址\"]').value==='https://api.deepseek.com'"
            )
            assert not probe.script("!!document.querySelector('input[aria-label=\"用户 ID\"]')")
            assert probe.script("document.querySelector('dialog[open]').textContent.includes('CNY')")
            probe.input('input[aria-label="令牌"]', "synthetic-official-key")
            probe.click("测试连接")
            probe.wait("document.querySelector('dialog[open]').textContent.includes('连接成功')")
            probe.click("取消", "document.querySelector('dialog[open] .dialog__footer')")
            checks.append(
                "DeepSeek form preselects official root, requires bearer key and omits New API fields"
            )

            probe.navigate("Codex", "Codex 统计")
            probe.wait("!!document.querySelector('.harness-page .table-wrap tbody tr')")
            probe.script(
                "window.__outer=document.querySelector('.workspace');"
                "window.__table=document.querySelectorAll('.harness-page .table-wrap')[1];"
                "window.__outer.scrollTop=250;window.__table.scrollTop=160;"
                "window.__table.dispatchEvent(new Event('scroll'));"
                "window.__saved=[window.__outer.scrollTop,window.__table.scrollTop];"
                "window.__oldRows=window.__table.querySelector('tbody').textContent"
            )
            probe.command("append")
            api("collection.scanNow", {"operationId": str(uuid4())})
            probe.wait("window.__table.querySelector('tbody').textContent!==window.__oldRows")
            assert probe.script(
                "Math.abs(window.__outer.scrollTop-window.__saved[0])<=1 && "
                "Math.abs(window.__table.scrollTop-window.__saved[1])<=1"
            )
            checks.append(
                "a real collection refresh preserves both workspace and hourly table scroll positions"
            )
        report["ok"] = True
    except BaseException as error:
        report["ok"] = False
        report["error"] = repr(error)
    path = ROOT / ".verify/pricing-recovery-native.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
