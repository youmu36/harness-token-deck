"""Exercise the production WebView2 and synthetic collection/pricing chain for P7."""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.tests.desktop.test_single_instance import kill_tree

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--management", action="store_true", help="Also accept P8.1/P8.2 workflows")
    args = parser.parse_args()
    directory = ROOT / ".verify" / f"p7-acceptance-{uuid4().hex[:8]}"
    directory.mkdir(parents=True)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("probe_usage_pages.py")),
            "--directory",
            str(directory),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend/src") + os.pathsep + str(ROOT)},
        stdout=(directory / "stdout.log").open("w"),
        stderr=(directory / "stderr.log").open("w"),
        text=True,
    )
    report: dict[str, Any] = {"directory": str(directory), "checks": [], "screens": []}

    def command(action: str, **values: object) -> Any:
        identifier = uuid4().hex
        (directory / "probe-command.json").write_text(
            json.dumps({"id": identifier, "action": action, **values}), encoding="utf-8"
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            result_file = directory / "probe-result.json"
            if result_file.exists():
                try:
                    reply = json.loads(result_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                if reply.get("id") == identifier:
                    return reply.get("result", reply)
            time.sleep(0.05)
        raise AssertionError(f"Native command timed out: {action}")

    def script(code: str) -> Any:
        return command("script", script=code)

    def wait(code: str) -> Any:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = script(code)
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"DOM condition timed out: {code}")

    def nav(name: str, title: str) -> None:
        script(f"document.querySelector('nav a[aria-label={json.dumps(name)}]').click()")
        wait(
            f"document.querySelector('h1')?.textContent === {json.dumps(title)} && "
            "document.querySelectorAll('.metric').length === 6"
        )
        wait("document.querySelectorAll('.metric[aria-busy=true]').length === 0")

    def capture(name: str) -> None:
        path = ROOT / "docs/design/screens" / name
        command("capture", path=str(path))
        report["screens"].append(name)

    try:
        wait("!!window.pywebview?.api && !!document.querySelector('.metrics-grid')")
        script(
            "window.__acceptance = null; pywebview.api.call('collection.scanNow',"
            "{operationId:crypto.randomUUID()}).then(r => window.__acceptance=r)"
        )
        assert wait("window.__acceptance")["ok"]
        with sqlite3.connect(directory / "htu.sqlite3") as connection:
            initial = connection.execute("SELECT harness,pico FROM ledger ORDER BY harness").fetchall()
        assert [row[0] for row in initial] == ["codex", "dsh", "zcode"], initial
        for name, title in (
            ("Codex", "Codex 统计"),
            ("Zcode", "Zcode 统计"),
            ("DSH", "Deepseek Harness 统计"),
            ("总览", "总览"),
        ):
            nav(name, title)
            rows = script(
                "Array.from(document.querySelectorAll('.model-table tbody tr')).map(r=>r.innerText)"
            )
            assert rows and all("暂无" not in row for row in rows), rows
        assert (
            script(
                "document.querySelector('[aria-label=\"按来源汇总\"] table')?.getAttribute('aria-rowcount')"
            )
            == "4"
        )
        report["checks"].append("three sources and overview reconcile with the real ledger")
        for factor in (1, 1.25, 1.5):
            zoom = command("zoom", factor=factor)
            report.setdefault("zoom", []).append(zoom)
            for name, title, stem in (("Codex", "Codex 统计", "harness"), ("总览", "总览", "overview")):
                nav(name, title)
                assert script("document.documentElement.scrollWidth <= window.innerWidth"), (factor, name)
                capture(f"p7-native-{stem}-{int(factor * 100)}.png")
        command("zoom", factor=1)
        nav("Codex", "Codex 统计")
        for theme in ("light", "dark"):
            script(
                f"const s=document.querySelector('[aria-label=主题]'); s.value='{theme}'; "
                "s.dispatchEvent(new Event('change',{bubbles:true}))"
            )
            wait(f"document.documentElement.dataset.theme === '{theme}'")
            assert script(
                "getComputedStyle(document.querySelector('.balance-card .money')).color === "
                "getComputedStyle(document.querySelector('.metric__value.money')).color"
            )
            capture(f"p7-native-balance-{theme}.png")
        script("document.querySelector('.pricing-button').click()")
        wait("document.querySelector('dialog')?.open")
        script(
            "const s=document.querySelector('[aria-label=版本]'); "
            "s.value=Array.from(s.options).find(o=>o.textContent.includes('v2')).value; "
            "s.dispatchEvent(new Event('change',{bubbles:true}))"
        )
        wait("!document.querySelector('dialog .button--primary')?.disabled")
        assert "Zcode" in script("document.querySelector('.switch-impact').textContent")
        script("document.querySelector('dialog .button--primary').click()")
        wait("!document.querySelector('dialog')?.open")
        command("append")
        script(
            "window.__acceptance=null; pywebview.api.call('collection.scanNow',"
            "{operationId:crypto.randomUUID()}).then(r=>window.__acceptance=r)"
        )
        assert wait("window.__acceptance")["ok"]
        with sqlite3.connect(directory / "htu.sqlite3") as connection:
            rows = connection.execute(
                "SELECT harness,pico FROM ledger ORDER BY harness,occurred_at"
            ).fetchall()
        for name, cost in initial:
            assert [int(pico) for harness, pico in rows if harness == name] == [int(cost), int(cost) * 3], (
                rows
            )
        report["checks"].append("UI switch keeps old costs and bills appended requests at triple rates")
        script(
            "Array.from(document.querySelectorAll('[role=radio]')).find(b=>b.textContent==='自定义').click()"
        )
        wait("document.querySelectorAll('.datetime-field').length === 2")
        script("document.querySelector('[aria-label=\"打开日历\"]').focus()")
        command("key", keys="{ENTER}")
        wait("document.querySelector('[data-day]:focus')?.dataset.day")
        for key in (
            "{RIGHT}",
            "{DOWN}",
            "{HOME}",
            "{END}",
            "{PGUP}",
            "{PGDN}",
            "+{PGUP}",
            "+{PGDN}",
            "{ENTER}",
            "{ESC}",
        ):
            command("key", keys=key)
        assert script("document.activeElement?.getAttribute('aria-label')") == "打开日历"
        report["checks"].append("native keyboard calendar navigation, selection and Escape focus return")
        if args.management:
            accept_management(command, script, wait, capture, directory, report)
        report["status"] = "passed"
        command("quit")
        process.wait(timeout=20)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            kill_tree(process)
        (ROOT / f".verify/{'p8-manage' if args.management else 'p7-pages'}.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, indent=2, ensure_ascii=True))


def accept_management(
    command: Any, script: Any, wait: Any, capture: Any, directory: Path, report: dict[str, Any]
) -> None:
    def click(text: str, root: str = "document") -> None:
        script(
            f"Array.from({root}.querySelectorAll('button')).find(b => "
            f"b.textContent.trim() === {json.dumps(text)}).click()"
        )

    def fill(label: str, value: str) -> None:
        script(
            f"const el=document.querySelector('dialog[open] input[aria-label={json.dumps(label)}]');"
            f"el.value={json.dumps(value)}; el.dispatchEvent(new Event('input',{{bubbles:true}}))"
        )

    def submit(form: str) -> None:
        script(
            f"document.querySelector({json.dumps(form)}).dispatchEvent("
            "new Event('submit',{bubbles:true,cancelable:true}))"
        )
        wait("!document.querySelector('dialog[open]')")

    def api(name: str, payload: dict[str, object]) -> Any:
        script(
            f"window.__manage=null; pywebview.api.call({json.dumps(name)},{json.dumps(payload)})"
            ".then(r=>window.__manage=r)"
        )
        reply = wait("window.__manage")
        assert reply["ok"], reply
        return reply["data"]

    script("document.querySelector('nav a[aria-label=\"供应商与钱包\"]').click()")
    wait("!!document.querySelector('.wallet-page') && !!document.querySelector('.vendor-card')")
    click("添加供应商")
    wait("!!document.querySelector('dialog[open]')")
    fill("名称", "Native acceptance")
    fill("金额", "-0.5")
    submit("#wallet-command")
    supplier = next(v for v in api("wallet.list", {})["vendors"] if v["name"] == "Native acceptance")
    identifier = supplier["id"]
    card = "document.querySelector('.vendor-card')"
    click("充值", card)
    wait("!!document.querySelector('dialog[open]')")
    fill("金额", "100")
    submit("#wallet-command")
    click("校准余额", card)
    wait("!!document.querySelector('dialog[open]')")
    fill("金额", "65")
    wait("document.querySelector('dialog[open]').textContent.includes('-34.500000')")
    submit("#wallet-command")
    supplier = next(v for v in api("wallet.list", {})["vendors"] if v["id"] == identifier)
    assert (supplier["R"], supplier["C"], supplier["A"], supplier["B"]) == (
        "100000000000000",
        "0",
        "-35000000000000",
        "65000000000000",
    ), supplier
    click("供应商设置")
    click("关联价格组", "document.querySelector('.vendor-settings')")
    wait("!!document.querySelector('dialog[open]')")
    script("document.querySelector('dialog[open] input[type=checkbox]').click()")
    submit("#wallet-command")
    supplier = next(v for v in api("wallet.list", {})["vendors"] if v["id"] == identifier)
    assert supplier["linkedGroups"], supplier
    click("供应商设置")
    click("改名", "document.querySelector('.vendor-settings')")
    wait("!!document.querySelector('dialog[open]')")
    fill("名称", "Native renamed")
    submit("#wallet-command")
    capture("p8-native-wallet.png")
    script(f"window.location.hash='#/wallet/{identifier}'")
    wait("!!document.querySelector('.entries tbody tr')")
    assert script("document.querySelectorAll('.entries tbody tr').length") == 3
    report["checks"].append(
        "P8 native create, top-up, live calibration, group reassignment, rename and entries"
    )
    script("document.querySelector('nav a[aria-label=\"价格与模型\"]').click()")
    wait("!!document.querySelector('.price-editor .rate-row')")
    script(
        "const row=Array.from(document.querySelectorAll('.rate-row'))"
        ".find(r=>r.querySelector('[aria-label=计价模型]').value==='gpt-6-astra');"
        "for (const label of ['输入价格','缓存命中价格','输出价格']) { "
        "const el=row.querySelector('[aria-label='+label+']'); el.value='5'; "
        "el.dispatchEvent(new Event('input',{bubbles:true})); }"
    )
    click("保存\uff08追加修订\uff09")
    wait("document.querySelector('.price-editor footer').textContent.includes('未保存修改 0 项')")
    command("append")
    api("collection.scanNow", {"operationId": str(uuid4())})
    with sqlite3.connect(directory / "htu.sqlite3") as connection:
        costs = [
            int(row[0])
            for row in connection.execute(
                "SELECT pico FROM ledger WHERE harness='codex' ORDER BY occurred_at"
            )
        ]
    assert costs == [110_000_000, 330_000_000, 550_000_000], costs
    capture("p8-native-pricing.png")
    report["checks"].append(
        "P8 UI save appends revision; future request uses new prices and old costs remain"
    )
    before = api("pricing.catalog", {})["current"]
    click("新建价格组")
    wait("!!document.querySelector('dialog[open]')")
    fill("名称", "Native unused")
    submit("#pricing-command")
    script("document.querySelector('[aria-label=\"价格组二级导航\"] button:nth-child(2)').click()")
    click("新增版本")
    wait("!!document.querySelector('dialog[open]')")
    submit("#pricing-command")
    click("设为生效")
    wait("!!document.querySelector('dialog[open]')")
    assert "此组尚未使用" in script("document.querySelector('dialog[open]').textContent")
    submit("#pricing-command")
    after = api("pricing.catalog", {})
    assert after["current"] == before
    unused = next(g for g in after["groups"] if g["name"] == "Native unused")
    assert next(v for v in unused["versions"] if v["current"])["number"] == 2
    click("组设置")
    click("停用")
    wait("!!document.querySelector('dialog[open]')")
    submit("#pricing-command")
    click("价格编辑")
    assert script("document.querySelector('.price-editor').textContent.includes('此分组已停用')")
    click("组设置")
    click("删除", "document.querySelector('.group-detail')")
    wait("!!document.querySelector('dialog[open]')")
    submit("#pricing-command")
    assert not any(g["name"] == "Native unused" for g in api("pricing.catalog", {})["groups"])
    script(f"window.location.hash='#/wallet/{identifier}'")
    wait("!!document.querySelector('.entries')")
    click("供应商设置")
    click("删除 / 归档")
    wait("document.querySelector('dialog[open]').textContent.includes('该供应商有资金记录，将归档')")
    submit("#wallet-command")
    archived = next(
        v for v in api("wallet.list", {"includeArchived": True})["vendors"] if v["id"] == identifier
    )
    assert archived["status"] == "archived" and not archived["linkedGroups"]
    click("资金流水")
    wait("!!document.querySelector('.entries')")
    assert script("document.querySelectorAll('.entries tbody tr').length") >= 3
    click("供应商设置")
    click("恢复供应商")
    wait("!!document.querySelector('dialog[open]')")
    submit("#wallet-command")
    restored = next(v for v in api("wallet.list", {})["vendors"] if v["id"] == identifier)
    assert restored["status"] == "active" and not restored["linkedGroups"]
    report["checks"].append(
        "P8 unused-group activation, disable/delete, funded archive/read-only entries and restore"
    )


if __name__ == "__main__":
    main()
