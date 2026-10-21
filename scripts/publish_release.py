"""Build, check, push and publish the current version with GitHub CLI authentication."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from scripts.set_version import check_version

ROOT = Path(__file__).resolve().parents[1]
PENDING_SUFFIX = "\uff08待发布\uff09"


class ReleaseError(RuntimeError):
    pass


def repository(origin: str) -> str:
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?", origin)
    if not match:
        raise ReleaseError("origin must be a github.com HTTPS or SSH repository")
    return match[1]


def find_gh(root: Path, explicit: str | None) -> str:
    configured = explicit or os.environ.get("HTD_GH")
    if configured:
        path = Path(configured)
        if not path.is_file():
            raise ReleaseError("The configured GitHub CLI executable does not exist")
        return str(path.resolve())
    found = shutil.which("gh")
    if found:
        return found
    for path in (
        root / ".verify/tools/gh/bin/gh.exe",
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "GitHub CLI/gh.exe",
    ):
        if path.is_file():
            return str(path.resolve())
    raise ReleaseError("Install GitHub CLI, add gh to PATH, or set HTD_GH; run gh auth login once")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def network_command(args: tuple[str, ...], gh: str) -> tuple[str, ...]:
    if args[:2] not in (("git", "push"), ("git", "fetch")):
        return args
    helper = "!" + shlex.quote(str(Path(gh).resolve()).replace("\\", "/")) + " auth git-credential"
    return (
        "git",
        "-c",
        "credential.helper=",
        "-c",
        "credential.https://github.com.helper=",
        "-c",
        "credential.https://github.com.helper=" + helper,
        *args[1:],
    )


def validate_build(path: Path, version: str) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if (
        report.get("version") != version
        or report.get("checks") not in ("reused targeted checks", "frontend functionality passed")
        or report.get("smoke", {}).get("exit_code") != 0
        or report.get("smoke", {}).get("integrity") != "ok"
        or report.get("installer") != f"Harness Token Deck_{version}_x64-setup.exe"
    ):
        raise ReleaseError("The build manifest does not describe a checked, successful release build")
    installer = path.parent / report["installer"]
    if not installer.is_file() or installer.stat().st_size != report.get("installer_bytes"):
        raise ReleaseError("The installer is missing or does not match the build manifest")
    return report


def current_notes(root: Path, version: str) -> str:
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(rf"(?m)^## {re.escape(version)}{re.escape(PENDING_SUFFIX)}\s*$", text)
    if heading is None:
        raise ReleaseError(f"CHANGELOG.md must contain '## {version}{PENDING_SUFFIX}' before a new release")
    body = text[heading.end() :].strip()
    body = re.sub(r"\n历史版本说明见.*?。\s*$", "", body).strip()
    if not body:
        raise ReleaseError("Release notes are empty")
    return body + "\n"


def archive_notes(root: Path, version: str, release: dict[str, Any], notes: str) -> list[str]:
    pending = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## {version}{PENDING_SUFFIX}" in pending and current_notes(root, version) != notes:
        raise ReleaseError("Release notes changed during publishing; refusing to overwrite them")
    day = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
    date = day.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    folder = root / ".github/release-notes/archive"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{version}.md"
    content = f"# {version}\n\n发布日期:{date}\n\n[GitHub Release]({release['html_url']})\n\n{notes}"
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise ReleaseError(
            "Archived release notes already exist with different content; refusing to overwrite"
        )
    path.write_text(content, encoding="utf-8")
    index = folder / "README.md"
    existing = index.read_text(encoding="utf-8") if index.exists() else ""
    versions = set(re.findall(r"(?m)^\|\s*(\d+\.\d+\.\d+)\s*\|", existing)) | {version}
    ordered = sorted(versions, key=lambda value: tuple(map(int, value.split("."))), reverse=True)
    index.write_text(
        "# 历史版本说明\n\n| 版本 | 更新说明 |\n| --- | --- |\n"
        + "".join(f"| {value} | [{value}]({value}.md) |\n" for value in ordered),
        encoding="utf-8",
    )
    (root / "CHANGELOG.md").write_text(
        "# 当前更新说明\n\n## 未发布\n\n暂无待发布更新。\n\n"
        "历史版本说明见[归档目录](.github/release-notes/archive/README.md)。\n",
        encoding="utf-8",
    )
    return ["CHANGELOG.md", path.relative_to(root).as_posix(), index.relative_to(root).as_posix()]


class Publisher:
    def __init__(self, root: Path, gh: str) -> None:
        self.root, self.gh = root, gh
        self.repo = repository(self.capture("git", "remote", "get-url", "origin").strip())
        self.version = check_version(root)
        self.tag = "v" + self.version
        self.branch = ""

    def capture(self, *args: str) -> str:
        return subprocess.run(
            args,
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        ).stdout

    def live(self, *args: str) -> None:
        print("Running:", " ".join(args[:4]), flush=True)
        subprocess.run(network_command(args, self.gh), cwd=self.root, check=True, timeout=1800)

    def api(self, endpoint: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
        try:
            return json.loads(self.capture(self.gh, "api", endpoint))
        except subprocess.CalledProcessError as error:
            if missing_ok and "HTTP 404" in (error.stderr or ""):
                return None
            raise

    def changed(self) -> set[str]:
        values = self.capture("git", "diff", "--name-only", "-z", "HEAD")
        values += self.capture("git", "ls-files", "--others", "--exclude-standard", "-z")
        return set(values.split("\0")) - {""}

    def preflight(self) -> dict[str, Any] | None:
        self.capture(self.gh, "api", "user", "--jq", ".login")
        self.branch = self.capture(
            self.gh,
            "repo",
            "view",
            self.repo,
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ).strip()
        if self.capture("git", "branch", "--show-current").strip() != self.branch:
            raise ReleaseError("Run the publisher from the repository's default branch")
        if self.capture("git", "diff", "--name-only", "--diff-filter=U").strip():
            raise ReleaseError("Resolve Git conflicts before publishing")
        self.live("git", "fetch", "origin", self.branch, "--tags")
        try:
            self.capture("git", "merge-base", "--is-ancestor", "origin/" + self.branch, "HEAD")
        except subprocess.CalledProcessError as error:
            raise ReleaseError(
                "Integrate the updated origin branch before publishing; no force push is used"
            ) from error
        self.live(sys.executable, "scripts/check_publication.py", "--history")
        return self.api(f"repos/{self.repo}/releases/tags/{self.tag}", missing_ok=True)

    def wait_checks(self, commit: str) -> None:
        if not (self.root / ".github/workflows/ci.yml").is_file():
            return
        print("Waiting for the pushed commit's GitHub Checks workflow", flush=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            runs = json.loads(
                self.capture(
                    self.gh,
                    "run",
                    "list",
                    "--repo",
                    self.repo,
                    "--workflow",
                    "ci.yml",
                    "--commit",
                    commit,
                    "--event",
                    "push",
                    "--limit",
                    "1",
                    "--json",
                    "databaseId,status,conclusion",
                )
            )
            if runs:
                run = runs[0]
                if run["status"] == "completed":
                    if run["conclusion"] != "success":
                        raise ReleaseError(
                            "GitHub Checks failed; fix the commit before creating the release tag"
                        )
                    return
                self.live(
                    self.gh,
                    "run",
                    "watch",
                    str(run["databaseId"]),
                    "--repo",
                    self.repo,
                    "--interval",
                    "10",
                    "--exit-status",
                )
                return
            time.sleep(3)
        raise ReleaseError("GitHub Checks has not appeared yet; rerun the publisher to resume")

    def verify_assets(self, release: dict[str, Any], paths: list[Path]) -> None:
        assets = {asset["name"]: asset for asset in release["assets"]}
        if set(assets) != {path.name for path in paths}:
            raise ReleaseError("Release attachments do not match the planned four files")
        for path in paths:
            asset = assets[path.name]
            expected = sha256(path)
            if asset["size"] != path.stat().st_size or asset.get("state") != "uploaded":
                raise ReleaseError(f"Incomplete upload: {path.name}")
            if asset.get("digest"):
                if asset["digest"] != "sha256:" + expected:
                    raise ReleaseError(f"Uploaded checksum mismatch: {path.name}")
            else:
                with tempfile.TemporaryDirectory(prefix="htd-release-verify-") as temporary:
                    self.live(
                        self.gh,
                        "release",
                        "download",
                        self.tag,
                        "--repo",
                        self.repo,
                        "--pattern",
                        path.name,
                        "--dir",
                        temporary,
                    )
                    if sha256(Path(temporary) / path.name) != expected:
                        raise ReleaseError(f"Downloaded checksum mismatch: {path.name}")

    def finish(self, release: dict[str, Any], notes: str) -> str:
        files = archive_notes(self.root, self.version, release, notes)
        if self.changed() - set(files):
            raise ReleaseError(
                "Release is published; unrelated changes prevent the notes-only follow-up commit"
            )
        if self.changed():
            self.live("git", "add", "--", *files)
            self.live("git", "commit", "-m", f"docs: archive {self.version} release notes")
            self.live("git", "push", "origin", f"HEAD:refs/heads/{self.branch}")
        return str(release["html_url"])

    def publish(self, *, reuse_checks: bool = False, dry_run: bool = False) -> str:
        release = self.preflight()
        archive = self.root / f".github/release-notes/archive/{self.version}.md"
        if release and not release["draft"]:
            allowed = {
                "CHANGELOG.md",
                archive.relative_to(self.root).as_posix(),
                ".github/release-notes/archive/README.md",
            }
            difference = set(self.capture("git", "diff", "--name-only", self.tag, "HEAD").splitlines())
            if (self.changed() | difference) - allowed:
                raise ReleaseError(
                    "This version is already published; bump VERSION before publishing more changes"
                )
            if archive.exists() and not self.changed():
                return str(release["html_url"])
            notes = (
                archive.read_text(encoding="utf-8").split("\n\n", 3)[-1]
                if archive.exists()
                else current_notes(self.root, self.version)
            )
            return str(release["html_url"]) if dry_run else self.finish(release, notes)
        notes = current_notes(self.root, self.version)
        tag_commit = self.capture("git", "tag", "--list", self.tag).strip()
        if tag_commit and (
            self.changed()
            or self.capture("git", "rev-parse", self.tag + "^{}") != self.capture("git", "rev-parse", "HEAD")
        ):
            raise ReleaseError("The version tag already identifies another snapshot; it will not be moved")
        print(
            f"Release plan: {self.repo} / {self.tag}; installer, source ZIP, manifest and SHA256SUMS.txt",
            flush=True,
        )
        if dry_run:
            return f"https://github.com/{self.repo}/releases/tag/{self.tag}"
        build = [sys.executable, "scripts/build_release.py"]
        if reuse_checks:
            build.append("--reuse-checks")
        self.live(*build)
        folder = self.root / "release"
        manifest = folder / f"release-{self.version}.json"
        report = validate_build(manifest, self.version)
        self.live(sys.executable, "scripts/check_publication.py", "--history", "--export")
        if self.changed():
            self.live("git", "add", "--all")
            self.live("git", "commit", "-m", f"release: Harness Token Deck {self.version}")
        commit = self.capture("git", "rev-parse", "HEAD").strip()
        if self.capture("git", "rev-parse", "origin/" + self.branch).strip() != commit:
            self.live("git", "push", "origin", f"HEAD:refs/heads/{self.branch}")
        self.wait_checks(commit)
        if not tag_commit:
            self.live("git", "tag", "-a", self.tag, "-m", f"Release {self.version}", commit)
        self.live("git", "push", "origin", "refs/tags/" + self.tag)
        installer = folder / report["installer"]
        source = folder / f"HarnessTokenDeck-{self.version}-source.zip"
        report.update(
            git_commit=commit,
            git_tag=self.tag,
            installer_sha256=sha256(installer),
            source_sha256=sha256(source),
        )
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        files = [installer, source, manifest]
        checksum = folder / "SHA256SUMS.txt"
        checksum.write_text("".join(f"{sha256(path)} *{path.name}\n" for path in files), encoding="utf-8")
        files.append(checksum)
        notes_file = folder / f"notes-{self.version}.md"
        notes_file.write_text(notes, encoding="utf-8")
        if release is None:
            self.live(
                self.gh,
                "release",
                "create",
                self.tag,
                "--repo",
                self.repo,
                "--verify-tag",
                "--draft",
                "--title",
                f"Harness Token Deck {self.tag}",
                "--notes-file",
                str(notes_file),
            )
        else:
            self.live(
                self.gh, "release", "edit", self.tag, "--repo", self.repo, "--notes-file", str(notes_file)
            )
        self.live(self.gh, "release", "upload", self.tag, "--repo", self.repo, "--clobber", *map(str, files))
        uploaded = self.api(f"repos/{self.repo}/releases/tags/{self.tag}")
        assert uploaded is not None
        self.verify_assets(uploaded, files)
        self.live(self.gh, "release", "edit", self.tag, "--repo", self.repo, "--draft=false", "--latest")
        published = self.api(f"repos/{self.repo}/releases/tags/{self.tag}")
        if published is None or published["draft"] or not published["published_at"]:
            raise ReleaseError("GitHub has not confirmed the public release")
        description = self.root / ".github/description.txt"
        if description.exists():
            self.live(
                self.gh,
                "repo",
                "edit",
                self.repo,
                "--description",
                description.read_text(encoding="utf-8").strip(),
            )
        return self.finish(published, notes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gh", help="Path to an existing GitHub CLI executable")
    parser.add_argument("--reuse-checks", action="store_true", help="Reuse already completed targeted checks")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preflight only; no build, commit, push or release"
    )
    args = parser.parse_args()
    os.environ.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", GIT_TERMINAL_PROMPT="0")
    try:
        publisher = Publisher(ROOT, find_gh(ROOT, args.gh))
        print(publisher.publish(reuse_checks=args.reuse_checks, dry_run=args.dry_run), flush=True)
    except (ReleaseError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"Publish stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
