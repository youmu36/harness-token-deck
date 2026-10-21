"""Check frontend functionality, compile and generate the Windows installer."""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from typing import Any

from collect_licenses import collect
from make_icon import make_icon
from release_support import DIST, ROOT, find_makensis, smoke, write_report
from set_version import check_version


def run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def build_frozen(version: str) -> dict[str, Any]:
    dependencies = collect()
    make_icon(ROOT / "packaging/app.png", ROOT / "packaging/app.ico")
    (ROOT / "build").mkdir(exist_ok=True)
    template = Template((ROOT / "packaging/version_info.py.tmpl").read_text(encoding="utf-8"))
    (ROOT / "build/version_info.py").write_text(
        template.substitute(version=version, version_tuple=", ".join([*version.split("."), "0"])),
        encoding="utf-8",
    )
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "packaging/htd.spec")
    return {
        "dependencies": {
            key: [{k: p[k] for k in ("name", "version", "license")} for p in values]
            for key, values in dependencies.items()
        },
        "payload_bytes": sum(path.stat().st_size for path in DIST.rglob("*") if path.is_file()),
    }


def build_installer(version: str) -> Path:
    (ROOT / "release").mkdir(exist_ok=True)
    files = sorted(path for path in DIST.rglob("*") if path.is_file())
    directories = sorted(
        (path for path in DIST.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True
    )
    instructions = [f'Delete "$INSTDIR\\{path.relative_to(DIST)}"' for path in files]
    instructions += [f'RMDir "$INSTDIR\\{path.relative_to(DIST)}"' for path in directories]
    (ROOT / "build/uninstall_files.nsh").write_text("\n".join(instructions) + "\n", encoding="utf-8")
    run(
        str(find_makensis()),
        "/V3",
        "/INPUTCHARSET",
        "UTF8",
        f"/DHTD_VERSION={version}",
        "packaging/installer.nsi",
    )
    return ROOT / f"release/Harness Token Deck_{version}_x64-setup.exe"


def manifest(version: str, installer: Path, report: dict[str, Any]) -> None:
    # Remove the old checksum sidecar so it cannot describe the newly rebuilt installer.
    installer.with_suffix(installer.suffix + ".sha256").unlink(missing_ok=True)
    report.update(
        {
            "version": version,
            "built_at": datetime.now(UTC).isoformat(),
            "architecture": "x64",
            "python": sys.version.split()[0],
            "installer": installer.name,
            "installer_bytes": installer.stat().st_size,
            "signing": "unsigned",
            "project_license": "MIT",
        }
    )
    write_report(ROOT / f"release/release-{version}.json", report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-checks", action="store_true", help="Local debug only; marks output unverified"
    )
    parser.add_argument(
        "--reuse-checks",
        action="store_true",
        help="Reuse this change's completed checks; build and smoke once without rerunning frontend tests",
    )
    parser.add_argument("--from-verifier", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--frozen-only", action="store_true", help="Build and smoke the onedir payload")
    parser.add_argument("--smoke", action="store_true", help="Smoke the existing onedir payload only")
    args = parser.parse_args()
    if args.skip_checks and args.reuse_checks:
        parser.error("--skip-checks and --reuse-checks cannot be combined")
    if os.name != "nt" or sys.maxsize <= 2**32:
        parser.error("Windows x64 Python is required")
    version = check_version()
    if args.smoke:
        print(smoke())
        return 0
    if args.from_verifier and os.environ.get("HTD_RELEASE_VERIFIED") != "1":
        parser.error("--from-verifier is reserved for verify.py release")
    verified = bool(args.from_verifier or not args.skip_checks)
    if not args.from_verifier and not args.skip_checks and not args.reuse_checks:
        run(shutil.which("npm.cmd") or "npm.cmd", "run", "typecheck")
        run(shutil.which("npm.cmd") or "npm.cmd", "test")
    if not args.from_verifier:
        run(shutil.which("npm.cmd") or "npm.cmd", "run", "build")
    report = build_frozen(version)
    report["checks"] = (
        "reused targeted checks"
        if args.reuse_checks
        else "frontend functionality passed"
        if verified
        else "skipped (local debug)"
    )
    report["smoke"] = smoke()
    print(f"Frozen payload: {report['payload_bytes']} bytes ({DIST})", flush=True)
    if args.frozen_only:
        write_report(ROOT / ".verify/release-build.json", report)
        return 0
    installer = build_installer(version)
    manifest(version, installer, report)
    print(f"Installer: {installer}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
