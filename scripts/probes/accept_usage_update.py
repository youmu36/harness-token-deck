"""Source/time selectors and 30-day hours in an isolated production WebView2."""

# ruff: noqa: RUF001 -- Exact user-facing labels are used as DOM selectors.

import json
from typing import Any
from uuid import uuid4

from scripts.probes.ui_probe import ROOT, UiProbe


def main() -> int:
    directory = ROOT / ".verify/usage-update-native-runs" / uuid4().hex
    checks: list[str] = []
    report: dict[str, Any] = {"directory": str(directory), "checks": checks}
    try:
        with UiProbe(directory) as probe:
            probe.wait("!!document.querySelector('nav[aria-label=\"主导航\"]')")

            def api(name: str, payload: dict[str, object]) -> Any:
                probe.script(
                    f"window.__usageResult=null;pywebview.api.call({json.dumps(name)},{json.dumps(payload)}).then(r=>window.__usageResult=r)"
                )
                result = probe.wait("window.__usageResult")
                assert result["ok"], result
                return result["data"]

            probe.navigate("数据统计与分析", "数据统计与分析")
            probe.script(
                "window.__usageCalls=[];const original=pywebview.api.call;"
                "pywebview.api.call=function(name,payload){"
                "if(name==='stats.summary')window.__usageCalls.push(payload);return original(name,payload)}"
            )
            for value in ("claude", "codex", "zcode", "dsh", "all"):
                probe.script("document.querySelector('[aria-label^=\"数据来源：\"]').click()")
                probe.wait("!!document.querySelector('[aria-label=\"数据来源\"]')")
                probe.script(
                    f'document.querySelector(\'[aria-label="数据来源"] [data-value="{value}"]\').click()'
                )
                try:
                    probe.wait(f"window.__usageCalls.at(-1)?.harness === '{value}'")
                except Exception:
                    report["diagnostic"] = probe.script(
                        "({calls:window.__usageCalls,"
                        "selected:document.querySelector('[aria-label^=\"数据来源：\"]')?.outerHTML,"
                        "alerts:[...document.querySelectorAll('[role=alert]')].map(e=>e.textContent),"
                        "text:document.querySelector('.overview-page')?.textContent})"
                    )
                    probe.command("capture", path=str(directory / "failure.png"))
                    raise
                assert api("preferences.get", {})["preferences"]["filters"]["overview"]["harness"] == value
            checks.append("all five source choices reach the real statistics query and persist independently")
            probe.wait("!!document.querySelector('[aria-label=\"按来源汇总\"]')")
            before = probe.script("window.__usageCalls.length")
            probe.script("document.querySelector('[data-value=\"cost\"]').click()")
            probe.script("document.querySelector('[data-value=\"tokens\"]').click()")
            assert probe.script("window.__usageCalls.length") == before
            checks.append("trend display changes reuse returned data without another statistics query")
            for theme in ("light", "dark"):
                snapshot = api("preferences.get", {})
                api(
                    "preferences.update",
                    {
                        "patch": {"theme": theme},
                        "expectedRevision": snapshot["revision"],
                        "operationId": str(uuid4()),
                    },
                )
                probe.wait(f"document.documentElement.dataset.theme === '{theme}'")
                probe.script("document.querySelector('[aria-label^=\"时间范围：\"]').click()")
                assert probe.script(
                    "[...document.querySelectorAll('[aria-label=\"时间范围\"] [role=radio]')]"
                    ".map(e=>e.dataset.value)"
                ) == ["today", "24hours", "7days", "30days"]
                probe.command("capture", path=str(ROOT / f"docs/design/screens/usage-time-{theme}.png"))
                probe.script("document.querySelector('[data-value=\"24hours\"]').click()")
                probe.wait(
                    "window.__usageCalls.length && Date.parse(window.__usageCalls.at(-1).end)"
                    "-Date.parse(window.__usageCalls.at(-1).start)===86400000"
                )
                probe.script("document.querySelector('[aria-label^=\"时间范围：\"]').click()")
                probe.script("document.querySelector('[data-value=\"30days\"]').click()")
                probe.wait(
                    "window.__usageCalls.at(-1)?.grain==='hour' && Date.parse(window.__usageCalls.at(-1).end)"
                    "-Date.parse(window.__usageCalls.at(-1).start)>29*86400000"
                )
                probe.wait("document.querySelector('.grain-note')?.textContent.includes('小时')")
                assert not probe.script("document.querySelector('[data-value=\"hour\"]').disabled")
                positions = probe.script(
                    "(()=>{const a=document.querySelector('[aria-label^=\"时间范围：\"]')"
                    ".getBoundingClientRect();"
                    "const b=document.querySelector('[data-value=\"custom\"]').getBoundingClientRect();"
                    "return {left:a.left,right:a.right,custom:b.left,y:a.y,cy:b.y}})()"
                )
                assert positions["custom"] > positions["right"] and positions["y"] == positions["cy"]
                probe.script("document.querySelector('[data-value=\"custom\"]').click()")
                probe.wait("!!document.querySelector('.custom-range')")
                assert not probe.script("document.documentElement.scrollWidth>innerWidth")
                probe.command("capture", path=str(ROOT / f"docs/design/screens/usage-custom-{theme}.png"))
                probe.script("document.querySelector('[aria-label^=\"时间范围：\"]').click()")
                probe.script("document.querySelector('[data-value=\"today\"]').click()")
                checks.append(
                    f"{theme}: dropdown order, rolling 24 hours, 30-day hours and primary custom controls"
                )
            for label, title in (
                ("Codex", "Codex"),
                ("Claude Code", "Claude Code"),
                ("Zcode", "Zcode"),
                ("DSH", "Deepseek Harness"),
            ):
                probe.navigate(label, title + " 统计")
                probe.script("document.querySelector('[aria-label^=\"时间范围：\"]').click()")
                probe.wait("!!document.querySelector('[data-value=\"24hours\"]')")
                probe.script("document.querySelector('[data-value=\"24hours\"]').click()")
                probe.wait(
                    "document.querySelector('[aria-label^=\"时间范围：\"]')"
                    ".textContent.includes('近 24 小时')"
                )
                assert probe.script("!!document.querySelector('[data-value=\"custom\"]')")
            checks.append("all four Harness pages share the secondary time selector and rolling 24 hours")
            probe.navigate("数据统计与分析", "数据统计与分析")
            probe.script("document.querySelector('[aria-label^=\"数据来源：\"]').click()")
            probe.command("capture", path=str(ROOT / "docs/design/screens/usage-sources-dark.png"))
        report["ok"] = True
    except Exception as error:
        report.update(ok=False, error=str(error))
        raise
    finally:
        (ROOT / ".verify/usage-update-native.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
