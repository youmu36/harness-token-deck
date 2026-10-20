"""P10 native wallet acceptance; real keys never enter probe files or reports."""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import uuid4

from htu.adapters.credentials.winvault import WinVaultCredentialStore

from backend.tests.desktop.test_single_instance import kill_tree

ROOT = Path(__file__).resolve().parents[2]


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        value = (
            {"success": True, "data": {"quota_per_unit": 500000, "display_in_currency": True}}
            if self.path == "/api/status"
            else {"code": True, "data": {"total_available": 987655, "unlimited_quota": False}}
        )
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--site", choices=("moyuu", "heilovehei"), default="moyuu")
    parser.add_argument("--auto", action="store_true", help="Keep automatic interface selection")
    args = parser.parse_args()
    mode = "real-" + args.site if args.real else "synthetic"
    directory = ROOT / ".verify" / f"p10-remote-{mode}-{uuid4().hex[:8]}"
    directory.mkdir(parents=True)
    report: dict[str, Any] = {"mode": mode, "directory": str(directory), "checks": [], "screens": []}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler) if not args.real else None
    if server:
        Thread(target=server.serve_forever, daemon=True).start()
    output = (directory / "stdout.log").open("w")
    errors = (directory / "stderr.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scripts.probes.probe_usage_pages",
            "--directory",
            str(directory),
            *(["--remote-test"] if args.real else []),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend/src") + os.pathsep + str(ROOT)},
        stdout=output,
        stderr=errors,
        text=True,
    )

    def command(action: str, **values: object) -> Any:
        identifier = uuid4().hex
        (directory / "probe-command.json").write_text(
            json.dumps({"id": identifier, "action": action, **values}), encoding="utf-8"
        )
        deadline = time.monotonic() + 25
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
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            value = script(code)
            if value:
                return value
            time.sleep(0.05)
        raise AssertionError(f"DOM wait timed out: {code}")

    def click(label: str, scope: str = "document") -> None:
        assert script(
            f"(() => {{const e=[...{scope}.querySelectorAll('button')]"
            f".find(b=>b.textContent.trim()==={json.dumps(label)});"
            " if(!e || e.disabled) return false; e.click(); return true;})()"
        ), label

    def input(label: str, value: str, event: str = "input") -> None:
        script(
            f"(() => {{const e=document.querySelector({json.dumps('[aria-label="' + label + '"]')});"
            f" e.value={json.dumps(value)};"
            f" e.dispatchEvent(new Event({json.dumps(event)},{{bubbles:true}}));}})()"
        )

    def api(name: str, payload: dict[str, object]) -> Any:
        script(
            f"window.__remoteAcceptance=null;pywebview.api.call({json.dumps(name)},{json.dumps(payload)}).then(r=>window.__remoteAcceptance=r)"
        )
        value = wait("window.__remoteAcceptance")
        assert value["ok"], value
        return value["data"]

    def capture(name: str) -> None:
        target = ROOT / "docs/design/screens" / name
        command("capture", path=str(target))
        report["screens"].append(str(target))

    try:
        wait("!!document.querySelector('.metrics-grid') && !!window.pywebview?.api")
        script("location.hash='/wallet'")
        wait("!!document.querySelector('.vendor-card .remote-balance')")
        vendors = api("wallet.list", {})["vendors"]
        supplier = vendors[0]
        vendor_id = supplier["id"]
        before = {k: supplier[k] for k in ("R", "C", "A", "B")}
        click("远程账户")
        wait("!!document.querySelector('.remote-panel')")
        click("配置远程账户")
        wait("!!document.querySelector('input[aria-label=\"站点地址\"]')")
        if args.real:
            source = (ROOT / "docs/测试.txt").read_text(encoding="utf-8-sig")
            if args.site == "moyuu":
                base = "https://moyuu.cc"
                input(
                    "查询方式" if args.auto else "接口类型",
                    "access_token" if args.auto else "newapi",
                    "change",
                )
                user = re.search(r"用户\s*id\s*[=\uff1a:\s]*([0-9]{1,20})", source, re.I)
                assert user is not None
                input("用户 ID", user.group(1))
            else:
                match = re.search(r"https://[^\s]+", source)
                assert match is not None
                base = match.group(0).rstrip("/").removesuffix("/v1")
                input(
                    "查询方式" if args.auto else "接口类型", "billing" if args.auto else "sub2api", "change"
                )
            input("站点地址", base)
            command("remote-credential", site=args.site)
        else:
            assert server is not None
            input("站点地址", f"http://127.0.0.1:{server.server_port}")
            input("令牌", "synthetic-key-abcd")
        script("document.querySelector('input[type=checkbox]').click()")
        click("测试连接")
        wait(
            "!!document.querySelector('.test-result') && "
            "!document.querySelector('.test-result').textContent.includes('正在')"
        )
        report["apiTestText"] = script("document.querySelector('.test-result').textContent.trim()")
        click("保存")
        wait("!document.querySelector('dialog[open]')")
        click("刷新远程余额")
        wait("!document.querySelector('.remote-balance button').disabled")
        result = api("remote.get", {"vendorId": vendor_id})
        report["apiResult"] = {
            "status": result["status"],
            "kind": result["observation"]["kind"] if result["observation"] else None,
            "error": result["error"]["code"] if result["error"] else None,
            "authMode": result["account"]["authMode"],
            "siteType": result["account"]["siteType"],
            "amountPico": result["observation"]["amountPico"] if result["observation"] else None,
            "currency": result["observation"]["currency"] if result["observation"] else None,
        }
        capture(f"p10-remote-{mode}-wallet.png")
        if args.real:
            assert result["error"] is None and result["observation"]
            expected_type = "auto" if args.auto else "newapi" if args.site == "moyuu" else "sub2api"
            assert result["account"]["siteType"] == expected_type
            observation = result["observation"]
            assert observation["kind"] == "account_remaining" and observation["currency"] == "USD"
            assert observation["canCalibrate"] and not observation["currencyUncertain"]
            amount = int(observation["amountPico"])
            scaled = abs(amount) // 1_000_000
            target = f"{'-' if amount < 0 and scaled else ''}{scaled // 1_000_000}.{scaled % 1_000_000:06d}"
            click("按此余额校准")
            wait("!!document.querySelector('input[aria-label=\"金额\"]')")
            assert script(
                "document.querySelector('input[aria-label=\"金额\"]').value===" + json.dumps(target)
            )
            capture(f"p10-remote-{mode}-calibrate.png")
            click("取消", "document.querySelector('dialog[open]')")
            wait("!document.querySelector('dialog[open]')")
            report["checks"].append("real account USD balance / calibration prefill / cancellation verified")
        else:
            assert result["observation"] and result["observation"]["canCalibrate"]
            click("按此余额校准")
            wait("!!document.querySelector('input[aria-label=\"金额\"]')")
            assert script("document.querySelector('input[aria-label=\"金额\"]').value==='1.975310'")
            capture("p10-remote-synthetic-calibrate.png")
            click("确认", "document.querySelector('dialog[open]')")
            wait("!document.querySelector('dialog[open]')")
            after = api("wallet.list", {})["vendors"][0]
            assert after["B"] == result["observation"]["amountPico"]
            assert after["R"] == before["R"] and after["C"] == before["C"]
            report["checks"].append("synthetic remote calibration prefill / exact target / unchanged R and C")
        if args.real:
            after = api("wallet.list", {})["vendors"][0]
            assert {k: after[k] for k in ("R", "C", "A", "B")} == before
            report["checks"].append(
                "real configuration, test and refresh leave local wallet totals unchanged"
            )
        api(
            "preferences.update",
            {
                "operationId": str(uuid4()),
                "expectedRevision": api("preferences.get", {})["revision"],
                "patch": {"theme": "dark"},
            },
        )
        click("编辑远程账户")
        wait("!!document.querySelector('input[aria-label=\"令牌\"]')")
        assert script(
            "document.querySelector('input[aria-label=\"令牌\"]').value==='' && "
            "document.querySelector('dialog[open]').textContent.includes('已保存 ••••')"
        )
        assert script(
            "document.querySelector('select[aria-label=\"接口类型\"]').value==="
            + json.dumps(result["account"]["siteType"])
        )
        capture(f"p10-remote-{mode}-account-dark.png")
        report["checks"].append("saved key is masked, password blank, production light/dark views rendered")
        click("移除账户")
        click("确认移除")
        wait("!document.querySelector('dialog[open]')")
        assert api("remote.get", {"vendorId": vendor_id})["account"] is None
        report["checks"].append("remove account cancels settings and deletes vault credential")
        report["passed"] = True
    except BaseException as error:
        report["passed"] = False
        report["failure"] = str(error)
        with suppress(Exception):
            report["dialogText"] = script("document.querySelector('dialog[open]')?.textContent")
            capture(f"p10-remote-{mode}-failure.png")
        raise
    finally:
        try:
            command("quit")
            process.wait(20)
        except Exception:
            kill_tree(process)
        output.close()
        errors.close()
        if server:
            server.shutdown()
            server.server_close()
        # Clean even when the UI flow failed before account removal.
        path = directory / "htu.sqlite3"
        if path.exists():
            with sqlite3.connect(path) as db:
                references = [r[0] for r in db.execute("SELECT credential_ref FROM remote_account")]
            vault = WinVaultCredentialStore()
            for reference in references:
                vault.delete(reference)
        (directory / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
