"""Check updated wallet and cost cards in isolated production WebView2."""

import json
from typing import Any
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    directory = ROOT / ".verify/performance-native-runs" / uuid4().hex
    checks: list[str] = []
    report: dict[str, Any] = {"directory": str(directory), "checks": checks}
    try:
        with UiProbe(directory, synthetic_remote=True) as probe:
            probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")

            def api(name: str, payload: dict[str, object]) -> Any:
                probe.script(
                    f"window.__performanceResult=null;pywebview.api.call({json.dumps(name)},"
                    f"{json.dumps(payload)}).then(r=>window.__performanceResult=r)"
                )
                result = probe.wait("window.__performanceResult")
                assert result["ok"], result
                return result["data"]

            daily = api(
                "wallet.createVendor",
                {"name": "每日消费验收", "initialBalance": "100", "operationId": str(uuid4())},
            )
            revision = daily["revision"]
            for target in ("70", "65", "65"):
                change = api(
                    "wallet.calibrate",
                    {
                        "vendorId": daily["vendorId"],
                        "target": target,
                        "expectedWalletRevision": revision,
                        "operationId": str(uuid4()),
                    },
                )
                revision = change["revision"]

            remote = api(
                "wallet.createVendor",
                {"name": "自动充值验收", "initialBalance": "0", "operationId": str(uuid4())},
            )
            remote_id = remote["vendorId"]
            api(
                "remote.save",
                {
                    "vendorId": remote_id,
                    "baseUrl": "https://example.test",
                    "siteType": "sub2api",
                    "authMode": "api_key",
                    "credential": "synthetic-probe-abcd",
                    "enabled": False,
                    "operationId": str(uuid4()),
                },
            )
            for _ in range(2):
                api("remote.refresh", {"vendorId": remote_id, "operationId": str(uuid4())})
            rows = api("wallet.entries", {"vendorId": remote_id, "kind": "top_up"})["entries"]
            assert len(rows) == 1 and rows[0]["amountPico"] == "1234000000000"
            checks.append("native bridge: repeated remote refresh produces one exact top-up")

            for theme in ("light", "dark"):
                preference = api("preferences.get", {})
                api(
                    "preferences.update",
                    {
                        "patch": {"theme": theme},
                        "expectedRevision": preference["revision"],
                        "operationId": str(uuid4()),
                    },
                )
                probe.wait(f"document.documentElement.dataset.theme === {json.dumps(theme)}")
                for label in ("数据统计与分析", "Codex", "Claude Code", "DSH", "Zcode"):
                    probe.script(
                        f'document.querySelector(\'nav[aria-label="主导航"] '
                        f'a[aria-label="{label}"]\').click()'
                    )
                    title = label if label == "数据统计与分析" else f"{label} 统计"
                    if label == "DSH":
                        title = "Deepseek Harness 统计"
                    probe.wait(f"document.querySelector('h1')?.textContent === {json.dumps(title)}")
                    probe.wait(
                        "[...document.querySelectorAll('.metrics-grid .metric')].some(card=>"
                        "card.querySelector('h3')?.textContent.includes('估算成本') && "
                        "card.querySelector('.metric__description')?.textContent==='估算仅供参考')"
                    )
                    assert not probe.script("document.documentElement.scrollWidth>innerWidth")
                checks.append(f"{theme}: all five statistics cost cards show reference note")
                probe.command("capture", path=str(ROOT / f".verify/performance-cost-{theme}.png"))

                for identifier, expected in ((daily["vendorId"], "$35.0000"), (remote_id, "自动补记充值")):
                    probe.script(f"location.hash={json.dumps('/wallet/' + identifier)}")
                    probe.wait("!!document.querySelector('.entries tbody tr')")
                    probe.wait(
                        f"document.querySelector('.entries').textContent.includes({json.dumps(expected)})"
                    )
                    text = probe.script("document.querySelector('.entries').textContent")
                    assert "校准调整" not in text and "消费日汇总" not in text
                    assert probe.script(
                        "[...document.querySelectorAll('.entries option')].map(o=>o.textContent)"
                    ) == ["全部类型", "每日消费", "充值", "其他调整"]
                    if identifier == daily["vendorId"]:
                        assert probe.script("document.querySelectorAll('.entries tbody tr').length") == 1
                        assert "含差额校准" in text
                        probe.command("capture", path=str(ROOT / f".verify/performance-daily-{theme}.png"))
                    else:
                        probe.command("capture", path=str(ROOT / f".verify/performance-topup-{theme}.png"))
                checks.append(f"{theme}: wallet daily amount and automatic recharge are visible")
                probe.navigate("供应商与钱包", "供应商与钱包")
                probe.click("自动充值验收", "document.querySelector('nav[aria-label=\"供应商列表\"]')")
                probe.wait("document.querySelector('.vendor-heading')?.textContent.includes('自动充值验收')")
                probe.click("远程账户")
                probe.wait(
                    "document.querySelector('.remote-panel')?.textContent.includes('余额增加时自动补记充值')"
                )
                checks.append(f"{theme}: remote panel explains automatic recharge")
        report["ok"] = True
    except Exception as error:
        report.update(ok=False, error=str(error))
        raise
    finally:
        (ROOT / ".verify/performance-native.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
