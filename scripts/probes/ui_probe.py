"""Small isolated WebView2 test driver; exposes DOM actions without real user data."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


class UiProbe:
    def __init__(
        self, directory: Path, *, synthetic_remote: bool = False, isolated_startup: bool = False
    ) -> None:
        self.directory = directory
        directory.mkdir(parents=True)
        self.output = (directory / "stdout.log").open("w", encoding="utf-8")
        self.errors = (directory / "stderr.log").open("w", encoding="utf-8")
        self.startup_key = (
            rf"Software\HarnessTokenUsage\ProbeStartup\{uuid4().hex}" if isolated_startup else ""
        )
        self.process = subprocess.Popen(
            [sys.executable, "-m", "scripts.probes.probe_usage_pages", "--directory", str(directory)],
            cwd=ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "backend/src") + os.pathsep + str(ROOT),
                "HTU_PROBE_SYNTHETIC_REMOTE": "1" if synthetic_remote else "0",
                "HTU_PROBE_STARTUP_KEY": self.startup_key,
            },
            stdout=self.output,
            stderr=self.errors,
            text=True,
        )

    def command(self, action: str, **values: object) -> Any:
        identifier = uuid4().hex
        temporary = self.directory / "probe-command.tmp"
        temporary.write_text(json.dumps({"id": identifier, "action": action, **values}), encoding="utf-8")
        temporary.replace(self.directory / "probe-command.json")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and self.process.poll() is None:
            result = self.directory / "probe-result.json"
            if result.exists():
                try:
                    reply = json.loads(result.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    continue
                if reply.get("id") == identifier:
                    return reply.get("result", reply)
            time.sleep(0.05)
        raise AssertionError(f"WebView2 command timed out: {action}")

    def script(self, code: str) -> Any:
        return self.command("script", script=code)

    def wait(self, code: str) -> Any:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.script(code)
            if result:
                return result
            time.sleep(0.05)
        raise AssertionError(f"DOM condition timed out: {code}")

    def click(self, label: str, scope: str = "document") -> None:
        assert self.script(
            f"(() => {{ const button=[...{scope}.querySelectorAll('button')]"
            f".find(button=>button.textContent.trim().startsWith({json.dumps(label)}));"
            "if (!button || button.disabled) return false; button.click(); return true; })()"
        ), label

    def navigate(self, name: str, title: str) -> None:
        selector = f'nav[aria-label="主导航"] a[aria-label="{name}"]'
        self.script(f"document.querySelector({json.dumps(selector)}).click()")
        self.wait(f"document.querySelector('h1')?.textContent === {json.dumps(title)}")

    def input(self, selector: str, value: str, event: str = "input") -> None:
        self.script(
            f"(() => {{ const input=document.querySelector({json.dumps(selector)});"
            f"input.value={json.dumps(value)};"
            f"input.dispatchEvent(new Event({json.dumps(event)},{{bubbles:true}})); }})()"
        )

    def __enter__(self) -> "UiProbe":
        return self

    def __exit__(
        self, kind: type[BaseException] | None, value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            if self.process.poll() is None:
                self.command("quit")
                self.process.wait(timeout=15)
            if kind is None and self.process.returncode != 0:
                raise AssertionError(f"WebView2 exit code: {self.process.returncode}")
        finally:
            if self.process.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
            self.output.close()
            self.errors.close()
