"""Short native acceptance of recovery editing, wallet metadata and restored focus."""

import json
from typing import Any
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def call(probe: UiProbe, command: str, parameters: dict[str, object]) -> Any:
    probe.script(
        "window.__feedbackResult=null;window.pywebview.api.call("
        + json.dumps(command)
        + ","
        + json.dumps(parameters)
        + ").then(r=>window.__feedbackResult=r);true"
    )
    result = probe.wait("window.__feedbackResult")
    assert result["ok"], result
    return result["data"]


def main() -> None:
    report: dict[str, object] = {}
    with UiProbe(ROOT / ".verify/current-feedback" / uuid4().hex) as probe:
        probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")
        probe.command("resize", width=1024, height=680)
        catalog = call(probe, "pricing.catalog", {})
        version = catalog["groups"][0]["versions"][0]
        call(
            probe,
            "pricing.saveVersionPrices",
            {
                "operationId": str(uuid4()),
                "expectedRevision": catalog["revision"],
                "versionId": version["id"],
                "models": [],
            },
        )
        pool = call(probe, "models.pool", {})
        model = "gpt-6-astra"
        call(
            probe,
            "models.saveBaseRates",
            {
                "operationId": str(uuid4()),
                "expectedRevision": pool["revision"],
                "models": [
                    {
                        "modelId": model,
                        "rates": {"input": "1", "cache": "0.1", "cacheWrite": "0.5", "output": "3"},
                    }
                ],
            },
        )
        probe.command("append")
        call(probe, "collection.scanNow", {"operationId": str(uuid4())})
        probe.wait("document.querySelector('h1').textContent.includes('统计')")
        probe.script("location.hash='#/unpriced';true")
        probe.wait("!!document.querySelector('tbody button')")
        probe.click("编辑价格", "document.querySelector('tbody tr')")
        probe.wait("!!document.querySelector('dialog[open] [aria-label=倍率]')")
        probe.input('dialog[open] [aria-label="价格组"]', "htu-history", "change")
        probe.input('dialog[open] [aria-label="倍率"]', "2")
        probe.click("按倍率填充", "document.querySelector('dialog[open]')")
        probe.wait("document.querySelector('dialog[open] [aria-label=输入价格]').value==='2'")
        screenshot = ROOT / ".verify/current-feedback-price.png"
        probe.command("capture", path=str(screenshot))
        geometry = probe.script(
            "(() => {const d=document.querySelector('dialog[open]');const r=d.getBoundingClientRect();"
            "return {width:r.width,height:r.height,viewport:[innerWidth,innerHeight],"
            "overflow:d.scrollWidth>d.clientWidth};})()"
        )
        assert not geometry["overflow"], geometry
        probe.click("保存", "document.querySelector('dialog[open]')")
        probe.wait("!document.querySelector('dialog[open]')")
        probe.wait("document.querySelector('.unpriced-tab').textContent.includes('价格已补齐')")
        probe.click("一键重算已补价请求")
        probe.wait("document.querySelector('.unpriced-tab').textContent.includes('已重算')")
        report["priceDialog"] = geometry
        probe.navigate("供应商与钱包", "供应商与钱包")
        probe.click("供应商设置")
        probe.click("修改过往消费")
        probe.wait("!!document.querySelector('dialog[open] [aria-label=过往消费金额]')")
        probe.input('dialog[open] [aria-label="过往消费金额"]', "12.345678")
        probe.click("确认", "document.querySelector('dialog[open]')")
        probe.wait("!document.querySelector('dialog[open]')")
        probe.wait(
            "document.querySelector('.vendor-settings').textContent.includes('12.3457')"
            if probe.script("!!document.querySelector('.vendor-settings')")
            else "document.querySelector('main').textContent.includes('12.3457')"
        )
        probe.script("document.querySelector('.skip-link').focus();true")
        assert probe.script("document.querySelector('.skip-link').getBoundingClientRect().bottom<0")
        probe.command("minimize")
        probe.command("restore")
        probe.wait("!document.querySelector('.skip-link').classList.contains('keyboard-focus')")
        assert probe.script("document.querySelector('.skip-link').getBoundingClientRect().bottom<0")
        probe.command("hide")
        probe.command("restore")
        probe.wait("!document.querySelector('.skip-link').classList.contains('keyboard-focus')")
        assert probe.script("document.querySelector('.skip-link').getBoundingClientRect().bottom<0")
        probe.script("document.querySelector('nav[aria-label=\"主导航\"] a').focus();true")
        probe.command("key", keys="+{TAB}")
        probe.wait("document.querySelector('.skip-link').classList.contains('keyboard-focus')")
        assert probe.script("document.querySelector('.skip-link').getBoundingClientRect().top>=0")
        route = probe.script("location.hash")
        probe.command("key", keys="{ENTER}")
        assert probe.script("document.activeElement===document.querySelector('main')")
        assert probe.script("location.hash") == route
        report["checks"] = [
            "native price dialog and multiplier save",
            "history-only recalculation",
            "editable historical spending",
            "tray restore hides programmatic skip-link focus",
            "native minimize/restore does not reveal skip link",
            "real Shift+Tab/Enter skip link moves focus without routing",
        ]
        report["screenshot"] = str(screenshot)
    (ROOT / ".verify/current-feedback-native.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
