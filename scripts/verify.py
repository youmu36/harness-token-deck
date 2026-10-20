"""One verification entry point; deferred checks are explicit, never silent passes."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEVELS = ("quick", "full", "desktop", "release")


@dataclass(frozen=True)
class Step:
    name: str
    command: tuple[str, ...] = ()
    skip: str | None = None


@dataclass(frozen=True)
class Result:
    name: str
    status: str
    seconds: float
    command: tuple[str, ...]
    reason: str | None = None


def steps(level: str) -> list[Step]:
    python = sys.executable
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
    node = shutil.which("node") or "node"
    if level == "release":
        # Routine delivery checks functionality, then builds once. Deep diagnostics are opt-in.
        return [
            Step("vue-tsc", (npm, "run", "typecheck")),
            Step("vitest", (npm, "test")),
            Step("Production frontend", (npm, "run", "build")),
            Step(
                "Release build, frozen smoke and NSIS",
                (python, "scripts/build_release.py", "--from-verifier"),
            ),
        ]
    comprehensive = LEVELS.index(level) >= LEVELS.index("full")
    result = [
        Step("ruff", (python, "-m", "ruff", "check", "backend", "scripts")),
        Step("format", (python, "-m", "ruff", "format", "--check", "backend", "scripts")),
        Step("pyright", (python, "-m", "pyright")),
        Step(
            "pytest full" if comprehensive else "pytest quick",
            (python, "-m", "pytest", "-m", "not desktop" if comprehensive else "not slow and not desktop"),
        ),
        Step("vue-tsc", (npm, "run", "typecheck")),
        Step("vitest", (npm, "test")),
        Step("DateTime DST", (npm, "run", "test:dst")),
        Step("Python architecture", (python, "scripts/check_architecture.py")),
        Step("Frontend architecture", (node, "scripts/check_frontend_imports.mjs")),
        Step("Frontend checker tests", (node, "--test", "scripts/check_frontend_imports.test.mjs")),
        Step("Theme contrast", (node, "scripts/check_contrast.mjs")),
        Step("Theme contrast checker tests", (node, "--test", "scripts/check_contrast.test.mjs")),
        Step("Documentation links", (python, "scripts/check_docs.py")),
        Step("Router command registration", (python, "scripts/gen_contracts.py", "--check-registry")),
    ]
    if comprehensive:
        result.extend(
            [
                Step("Contract generation diff", (python, "scripts/gen_contracts.py", "--check")),
                Step("Cross-process contract roundtrip", (node, "scripts/contract_roundtrip.mjs", python)),
                Step("Production frontend", (npm, "run", "build")),
            ]
        )
    if LEVELS.index(level) >= LEVELS.index("desktop"):
        result.extend(
            [
                Step("Desktop native tests", (python, "-m", "pytest", "-m", "desktop")),
            ]
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("level", choices=LEVELS)
    parser.add_argument("--keep-going", action="store_true")
    args = parser.parse_args()
    level = str(args.level)
    results: list[Result] = []
    failed = False
    for step in steps(level):
        if step.skip:
            print(f"SKIP {step.name}: {step.skip}", flush=True)
            results.append(Result(step.name, "skipped", 0, step.command, step.skip))
            continue
        if failed and (not args.keep_going or step.name.startswith(("Release", "Installer"))):
            results.append(Result(step.name, "not_run", 0, step.command, "Previous step failed"))
            continue
        print(f"RUN  {step.name}", flush=True)
        started = time.monotonic()
        reason: str | None = None
        try:
            environment = {**os.environ, "HTU_RELEASE_VERIFIED": "1"} if level == "release" else None
            completed = subprocess.run(step.command, cwd=ROOT, check=False, env=environment)
            passed = completed.returncode == 0
            if not passed:
                reason = f"Exit code {completed.returncode}"
        except OSError as error:
            passed, reason = False, str(error)
        elapsed = round(time.monotonic() - started, 3)
        status = "passed" if passed else "failed"
        results.append(Result(step.name, status, elapsed, step.command, reason))
        print(f"{status.upper()} {step.name} ({elapsed:.3f}s)", flush=True)
        if not passed:
            failed = True
            print(f"Failed command: {subprocess.list2cmdline(step.command)}; {reason}", flush=True)
    destination = ROOT / ".verify/last.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                "level": level,
                "finished_at": datetime.now(UTC).isoformat(),
                "status": "failed" if failed else "passed",
                "has_skips": any(item.status == "skipped" for item in results),
                "steps": [asdict(item) for item in results],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Report: {destination}", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
