"""Set and validate the product version, including both dependency lock roots."""

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


def validate_version(value: str) -> str:
    if not re.fullmatch(VERSION_PATTERN, value) or any(int(part) > 65535 for part in value.split(".")):
        raise ValueError("Expected x.y.z with each component in 0..65535")
    return value


def check_version(root: Path = ROOT) -> str:
    version = validate_version((root / "VERSION").read_text(encoding="utf-8").strip())
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    npm_lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    uv_lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    values = {
        "pyproject.toml": project["project"]["version"],
        "package.json": package["version"],
        "package-lock.json": npm_lock["version"],
        "package-lock.json root": npm_lock["packages"][""]["version"],
        "uv.lock": next(p["version"] for p in uv_lock["package"] if p["name"] == package["name"]),
    }
    if any(value != version for value in values.values()):
        raise ValueError(f"Version mismatch: VERSION={version}, {values}")
    return version


def set_version(value: str, root: Path = ROOT) -> None:
    value = validate_version(value)
    path = root / "pyproject.toml"
    source = path.read_text(encoding="utf-8")
    project = tomllib.loads(source)["project"]
    before = f'version = "{project["version"]}"'
    section = re.search(r"(?ms)^\[project\]\s*\n(.*?)(?=^\[|\Z)", source)
    if section is None or section[1].count(before) != 1:
        raise ValueError("Cannot locate the unique project version")
    replacement = section[0].replace(before, f'version = "{value}"', 1)
    updated = source[: section.start()] + replacement + source[section.end() :]
    assert tomllib.loads(updated)["project"]["version"] == value
    path.write_text(updated, encoding="utf-8")
    for filename in ("package.json", "package-lock.json"):
        path = root / filename
        data = json.loads(path.read_text(encoding="utf-8"))
        data["version"] = value
        if filename == "package-lock.json":
            data["packages"][""]["version"] = value
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    (root / "VERSION").write_text(value + "\n", encoding="utf-8")
    subprocess.run(["uv", "lock"], cwd=root, check=True)
    check_version(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        print(check_version())
    elif args.version:
        set_version(args.version)
        print(check_version())
    else:
        parser.error("Provide a version or --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
