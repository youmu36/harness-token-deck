"""Collect locked runtime dependency notices and their installed license texts."""

import argparse
import importlib.metadata as metadata
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]


def python_runtime(root: Path) -> list[dict[str, Any]]:
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
    names: set[str] = set()

    def visit(name: str) -> None:
        if name in names:
            return
        names.add(name)
        for dependency in packages[name].get("dependencies", []):
            if "marker" not in dependency or Marker(dependency["marker"]).evaluate():
                visit(dependency["name"])

    visit(project)
    result: list[dict[str, Any]] = []
    for name in sorted(names - {project}):
        package = packages[name]
        distribution = metadata.distribution(name)
        if distribution.version != package["version"]:
            raise ValueError(f"Installed {name} does not match uv.lock")
        license_name = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
        if not license_name or len(license_name) > 150:
            classifiers = distribution.metadata.get_all("Classifier") or []
            license_name = ", ".join(c.split(" :: ")[-1] for c in classifiers if c.startswith("License ::"))
        texts: list[tuple[str, str]] = []
        for file in distribution.files or []:
            if any(
                part.lower().startswith(("license", "copying", "notice", "authors")) for part in file.parts
            ):
                path = Path(str(distribution.locate_file(file)))
                if path.is_file() and path.suffix.lower() not in {".py", ".pyc"}:
                    texts.append((str(file), path.read_text(encoding="utf-8", errors="replace")))
        if not texts and name == "proxy-tools":
            path = root / "packaging/licenses/proxy-tools-LICENSE.txt"
            texts.append((path.name, path.read_text(encoding="utf-8")))
            license_name = "BSD-2-Clause (upstream text; package metadata says MIT)"
        if not texts:
            raise ValueError(f"No license text found for {name}")
        result.append(
            {
                "name": name,
                "version": package["version"],
                "license": license_name or "See text",
                "texts": texts,
            }
        )
    return result


def npm_runtime(root: Path) -> list[dict[str, Any]]:
    lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    result: list[dict[str, Any]] = []
    for location, package in sorted(lock["packages"].items()):
        if not location or package.get("dev"):
            continue
        directory = root / location
        installed = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        if installed["version"] != package["version"]:
            raise ValueError(f"Installed {location} does not match package-lock.json")
        texts = [
            (path.name, path.read_text(encoding="utf-8", errors="replace"))
            for path in sorted(directory.iterdir())
            if path.is_file() and path.name.lower().startswith(("license", "copying", "notice"))
        ]
        if not texts:
            raise ValueError(f"No license text found for {location}")
        result.append(
            {
                "name": installed["name"],
                "version": package["version"],
                "license": package.get("license", "See text"),
                "texts": texts,
            }
        )
    return result


def collect(root: Path = ROOT) -> dict[str, list[dict[str, Any]]]:
    dependencies = {"python": python_runtime(root), "npm": npm_runtime(root)}
    lines = [
        "# Third-Party Notices",
        "",
        "Generated from uv.lock and package-lock.json runtime dependencies.",
        "Development and test dependencies are not part of the application.",
        "",
        "proxy-tools 0.1.0 omits its license text from the wheel. Its upstream LICENSE.txt",
        "is included verbatim; upstream uses BSD while wheel metadata claims MIT.",
        "Source: https://github.com/jtushman/proxy_tools/blob/master/LICENSE.txt",
        "",
        "Python is distributed under the PSF license. Its complete license is included below.",
        "",
        "Microsoft Edge WebView2 is a separately installed Evergreen runtime. The bundled SDK DLLs",
        "are subject to the Microsoft WebView2 SDK license included below. The installer downloads",
        "the official Microsoft bootstrapper only when the runtime is missing.",
        "",
        "## Runtime Dependencies",
        "",
        "| Ecosystem | Package | Version | License |",
        "| --- | --- | --- | --- |",
    ]
    for ecosystem, packages in dependencies.items():
        for package in packages:
            license_name = str(package["license"]).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {ecosystem} | {package['name']} | {package['version']} | {license_name} |")
    license_paths = {
        "Python": Path(sys.base_prefix) / "LICENSE.txt",
        "Microsoft WebView2 SDK": root / "packaging/licenses/WebView2-LICENSE.txt",
        "PyInstaller bootloader (GPL with distribution exception)": Path(
            str(
                metadata.distribution("pyinstaller").locate_file(
                    "pyinstaller-" + metadata.version("pyinstaller") + ".dist-info/licenses/COPYING.txt"
                )
            )
        ),
    }
    for name, path in license_paths.items():
        lines.extend(["", f"## {name}", "", "```text", path.read_text(encoding="utf-8"), "```"])
    for ecosystem, packages in dependencies.items():
        for package in packages:
            lines.extend(["", f"## {ecosystem}: {package['name']} {package['version']}"])
            for filename, content in package["texts"]:
                lines.extend(["", f"Source: {filename}", "", "```text", content.rstrip(), "```"])
    (root / "packaging/THIRD_PARTY_NOTICES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dependencies


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    dependencies = collect()
    print(json.dumps({key: len(value) for key, value in dependencies.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
