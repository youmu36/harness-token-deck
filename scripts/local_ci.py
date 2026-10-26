"""Run the Windows CI checks on an isolated snapshot, without pushing to GitHub."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from scripts.release_support import isolated_environment, write_report
from scripts.source_snapshot import source_files, source_fingerprint

ROOT = Path(__file__).resolve().parents[1]


def ci_environment(directory: Path | None = None) -> dict[str, str]:
    environment = isolated_environment(directory) if directory is not None else dict(os.environ)
    for name in (
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "HTD_SMOKE_ISOLATED",
    ):
        environment.pop(name, None)
    return environment


def run(command: tuple[str, ...], root: Path, environment: dict[str, str]) -> None:
    print("RUN ", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, cwd=root, env=environment, check=True)


def snapshot(root: Path, workspace: Path, environment: dict[str, str]) -> None:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()
    run(
        ("git", "clone", "--quiet", "--no-hardlinks", "--no-checkout", "--", str(root), str(workspace)),
        root,
        environment,
    )
    # Populate only the index; the worktree below receives the exact local bytes.
    run(("git", "reset", "--quiet", "--mixed", "--no-refresh", commit), workspace, environment)
    files = dict(source_files(root))
    # Copy working-tree bytes rather than just HEAD, including additions and deletions.
    for name, path in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def verify(root: Path = ROOT, *, force: bool = False) -> dict[str, Any]:
    if os.name != "nt" or sys.maxsize <= 2**32:
        raise ValueError("Local CI requires Windows x64, like the GitHub workflow")
    npm = shutil.which("npm.cmd") or "npm.cmd"
    uv = shutil.which("uv") or "uv"
    node = subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if not node.startswith("v24."):
        raise ValueError("Local CI requires Node.js 24, like the GitHub workflow")
    fingerprint = source_fingerprint(root)
    identity = {"platform": sys.platform, "python": platform.python_version(), "node": node}
    storage = root / ".verify/local-ci"
    storage.mkdir(parents=True, exist_ok=True)
    destination = storage / "last.json"
    if destination.is_file() and not force:
        try:
            loaded = json.loads(destination.read_text(encoding="utf-8"))
            cached = cast(dict[str, Any], loaded) if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            cached = {}
        if (
            cached.get("status") == "passed"
            and cached.get("source_fingerprint") == fingerprint
            and cached.get("environment") == identity
        ):
            print(f"REUSE local CI for source {fingerprint[:12]}", flush=True)
            return cached
    report: dict[str, Any] = {
        "status": "running",
        "source_fingerprint": fingerprint,
        "environment": identity,
    }
    write_report(destination, report)
    environment = ci_environment()
    try:
        with tempfile.TemporaryDirectory(prefix="run-", dir=storage) as temporary:
            workspace = Path(temporary).resolve() / "source"
            snapshot(root, workspace, environment)
            if source_fingerprint(workspace) != fingerprint:
                raise ValueError("Source changed while creating the local CI snapshot; retry")
            run((uv, "sync", "--locked", "--python", "3.12"), workspace, environment)
            run((npm, "ci"), workspace, environment)
            isolated = ci_environment(workspace.parent / "data")
            try:
                run((uv, "run", "--no-sync", "python", "-m", "scripts.verify", "ci"), workspace, isolated)
            finally:
                checks = workspace / ".verify/last.json"
                if checks.is_file():
                    report["verification"] = json.loads(checks.read_text(encoding="utf-8"))
            if source_fingerprint(workspace) != fingerprint or source_fingerprint(root) != fingerprint:
                raise ValueError("Source changed during local CI; the result cannot be reused; retry")
            report["status"] = "passed"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_report(destination, report)
        print(f"Local CI report: {destination}", flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Rerun even if this exact snapshot passed")
    args = parser.parse_args()
    try:
        verify(force=args.force)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Local CI stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
