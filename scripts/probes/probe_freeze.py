"""Build and smoke-test the webview probe, without installing the product."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    target = ROOT / ".verify/freeze"
    target.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--console",
        "--name",
        "htu-probe",
        "--distpath",
        str(target / "dist"),
        "--workpath",
        str(target / "build"),
        "--specpath",
        str(target),
        "--add-data",
        f"{ROOT / 'backend/src/htu/web'};web",
        "--hidden-import",
        "webview",
        "--hidden-import",
        "webview.platforms.winforms",
        "--hidden-import",
        "webview.platforms.edgechromium",
        "--hidden-import",
        "clr",
        str(ROOT / "scripts/probes/probe_webview.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True, timeout=300)
    output = target / "smoke.json"
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="htu-frozen-") as temporary:
        environment = {**os.environ, "HTU_DATA_DIR": temporary}
        smoke = subprocess.run(
            [str(target / "dist/htu-probe/htu-probe.exe"), "--output", str(output)],
            cwd=ROOT,
            env=environment,
            check=False,
            timeout=60,
        )
    report: dict[str, Any] = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    report["smoke_exit_code"] = smoke.returncode
    report["wall_seconds"] = round(time.monotonic() - started, 3)
    report["artifact_bytes"] = sum(p.stat().st_size for p in (target / "dist").rglob("*") if p.is_file())
    report["passed"] = smoke.returncode == 0 and report.get("passed", False)
    (ROOT / ".verify/probe-freeze.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
