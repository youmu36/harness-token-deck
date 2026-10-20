"""Accept P8.5 against production WebView2, isolated sources and a real SQLite ledger."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from htu.domain.common.money import format_pico

from backend.tests.desktop.test_single_instance import kill_tree

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    directory = ROOT / ".verify" / f"p8-history-{uuid4().hex[:8]}"
    directory.mkdir(parents=True)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("probe_usage_pages.py")),
            "--directory",
            str(directory),
            "--history",
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
            path = directory / "probe-result.json"
            if path.exists():
                try:
                    reply = json.loads(path.read_text(encoding="utf-8"))
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
            result = script(code)
            if result:
                return result
            time.sleep(0.05)
        raise AssertionError(f"DOM condition timed out: {code}")

    def click(text: str, root: str = "document") -> None:
        script(
            f"Array.from({root}.querySelectorAll('button')).find(b => "
            f"b.textContent.trim() === {json.dumps(text)}).click()"
        )

    def api(name: str, payload: dict[str, object]) -> Any:
        script(
            f"window.__accept=null; pywebview.api.call({json.dumps(name)}, {json.dumps(payload)})"
            ".then(r=>window.__accept=r)"
        )
        reply = wait("window.__accept")
        assert reply["ok"], reply
        return reply["data"]

    def capture(name: str) -> None:
        command("capture", path=str(ROOT / "docs/design/screens" / name))
        report["screens"].append(name)

    def ledger() -> list[tuple[str, str, str]]:
        with sqlite3.connect(directory / "htu.sqlite3") as db:
            return db.execute(
                "SELECT harness,pico,occurred_at FROM ledger ORDER BY harness,occurred_at"
            ).fetchall()

    try:
        wait("!!document.querySelector('dialog[open]') && !!window.pywebview?.api")
        assert "扫描历史数据" in script("document.querySelector('dialog[open]').textContent")
        assert api("preferences.get", {})["preferences"]["historyPrompted"]
        click("关闭", "document.querySelector('dialog[open]')")
        wait("!document.querySelector('dialog[open]')")
        assert "尚未导入历史" in script("document.querySelector('.shell').textContent")
        api("collection.scanNow", {"operationId": str(uuid4())})
        before = ledger()
        assert len(before) == 3, before
        sources = api("sources.list", {})["sources"]
        script("document.querySelector('nav a[aria-label=设置]').click()")
        wait("document.querySelector('h1')?.textContent === '设置'")
        click("扫描历史数据…")
        wait("!!document.querySelector('dialog[open]')")
        click("扫描", "document.querySelector('dialog[open]')")
        wait("document.querySelectorAll('.history-results input[value]').length === 2")
        assert script("document.querySelectorAll('.history-results input[value]:checked').length") == 1
        assert len(ledger()) == 3
        assert api("sources.list", {})["sources"] == sources, "History scan changed live collection state"
        capture("p8-history-native.png")
        click("导入选中日期\uff081\uff09", "document.querySelector('dialog[open]')")
        wait("document.querySelector('dialog[open]').textContent.includes('新增 3 条，跳过重复 0 条')")
        imported = ledger()
        assert len(imported) == 6, imported
        assert len([row for row in imported if int(row[1]) == 0]) == 3
        report["checks"].append(
            "First launch prompts once; all-source history scan is read-only; "
            "3 pre-start records import unpriced"
        )
        click("重新扫描", "document.querySelector('dialog[open]')")
        wait("document.querySelectorAll('.history-results input[value]').length === 2")
        assert script("document.querySelectorAll('.history-results input[value]:checked').length") == 0
        script("document.querySelector('.history-results input[value]').click()")
        click("导入选中日期\uff081\uff09", "document.querySelector('dialog[open]')")
        wait("document.querySelector('dialog[open]').textContent.includes('新增 0 条，跳过重复 3 条')")
        assert ledger() == imported
        click("关闭", "document.querySelector('dialog[open]')")
        wait("!document.querySelector('dialog[open]')")
        report["checks"].append(
            "Rescan defaults to no selected dates; explicit re-import skips duplicates with ledger unchanged"
        )
        script("document.querySelector('nav a[aria-label=Codex]').click()")
        wait("document.querySelector('h1')?.textContent === 'Codex 统计'")
        supplier = next(v for v in api("wallet.list", {})["vendors"] if v["name"] == "Fixture vendor")
        click("重算历史…")
        wait("!!document.querySelector('[aria-label=重算目标版本]')")
        script(
            "const s=document.querySelector('[aria-label=重算目标版本]'); "
            "s.value=Array.from(s.options).find(o=>o.textContent.includes('v2')).value; "
            "s.dispatchEvent(new Event('change',{bubbles:true}))"
        )
        click("预览", "document.querySelector('dialog[open]')")
        wait("!!document.querySelector('.recalc-preview')")
        preview_text = script("document.querySelector('.recalc-preview').textContent")
        assert "2 条记录" in preview_text, preview_text
        assert "Fixture vendor" in script("document.querySelector('.recalc-preview').textContent")
        assert "0.000220 USD" in script("document.querySelector('.recalc-preview').textContent")
        capture("p8-recalc-native.png")
        click("确认重算", "document.querySelector('dialog[open]')")
        wait("document.querySelectorAll('dialog[open]').length === 2")
        assert "不可撤销" in script(
            "Array.from(document.querySelectorAll('dialog[open]')).at(-1).textContent"
        )
        click("取消", "Array.from(document.querySelectorAll('dialog[open]')).at(-1)")
        wait("document.querySelectorAll('dialog[open]').length === 1")
        assert ledger() == imported
        click("确认重算", "document.querySelector('dialog[open]')")
        wait("document.querySelectorAll('dialog[open]').length === 2")
        click("执行重算", "Array.from(document.querySelectorAll('dialog[open]')).at(-1)")
        wait("!document.querySelector('dialog[open]')")
        after = ledger()
        assert [int(row[1]) for row in after if row[0] == "codex"] == [110_000_000, 330_000_000], after
        current = next(v for v in api("wallet.list", {})["vendors"] if v["id"] == supplier["id"])
        assert int(current["C"]) - int(supplier["C"]) == 220_000_000
        assert int(current["B"]) - int(supplier["B"]) == -220_000_000
        assert current["R"] == supplier["R"] and current["A"] == supplier["A"]
        wait("document.querySelector('.metrics-grid').textContent.includes('0.0003')")
        script("document.querySelector('nav a[aria-label=\"供应商与钱包\"]').click()")
        wait("!!document.querySelector('.vendor-card')")
        balance = format_pico(int(current["B"]), "full6")
        wait(f"document.querySelector('.vendor-card').textContent.includes({json.dumps(balance)})")
        with sqlite3.connect(directory / "htu.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM recalc_batch").fetchone()[0] == 1
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        report["checks"].append(
            "Recalc preview/cancel/confirm; codex cost 110000000→440000000 pico, request-time revisions; "
            "original vendor C +220000000, B -220000000; statistics and wallet UI refresh"
        )
        report["wallet"] = {"before": supplier, "after": current}
        report["ledger"] = after
        report["status"] = "passed"
        command("quit")
        process.wait(timeout=20)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            kill_tree(process)
        (ROOT / ".verify/p8-history-recalc.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
