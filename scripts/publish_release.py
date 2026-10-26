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
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from scripts.set_version import check_version
from scripts.source_snapshot import source_files, source_fingerprint

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


def asset_name(path: Path) -> str:
    # GitHub canonicalizes spaces in upload names.
    return path.name.replace(" ", ".")


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


def configure_proxy(value: str | None) -> None:
    if not value:
        return
    if value == "direct":
        for key in tuple(os.environ):
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
                del os.environ[key]
        return
    proxy = f"http://127.0.0.1:{value}" if value.isdecimal() else value
    if not re.fullmatch(r"(?:https?|socks5h?)://[^\s]+", proxy):
        raise ReleaseError("--proxy requires a proxy URL, a local port, or 'direct'")
    for key in tuple(os.environ):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            del os.environ[key]
    os.environ.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy)


def validate_build(path: Path, version: str, fingerprint: str | None = None) -> dict[str, Any]:
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
    if fingerprint is not None and report.get("source_fingerprint") != fingerprint:
        raise ReleaseError("The build manifest does not match the current source snapshot")
    if sha256(installer) != report.get("installer_sha256"):
        raise ReleaseError("The installer checksum does not match the build manifest; rebuild locally")
    return report


def release_only_path(name: str) -> bool:
    """Changes to these files cannot alter the frozen application or installer."""
    return (
        name in {"README.md", "CONTRIBUTING.md", "CHANGELOG.md"}
        or name.startswith((".github/", "backend/tests/", "frontend/tests/"))
        or name
        in {
            "scripts/publish_release.py",
            "scripts/publish_release.ps1",
            "scripts/publish_release.cmd",
            "scripts/publish_launcher.ps1",
        }
    )


def validate_archived_build(
    root: Path, manifest: Path, version: str, fingerprint: str, source_archive: Path
) -> dict[str, Any]:
    """Reuse an exact verified build when only publication files have changed."""
    report = validate_build(manifest, version)
    with zipfile.ZipFile(source_archive) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ReleaseError("The build source archive contains duplicate entries")
        original = {name: archive.read(name) for name in names}
    digest = hashlib.sha256(b"htd-source-v1\0")
    for name, content in sorted(original.items()):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(hashlib.sha256(content).digest())
    if digest.hexdigest() != report.get("source_fingerprint"):
        raise ReleaseError("The archived source does not match the installer's original build fingerprint")
    current = {name: path.read_bytes() for name, path in source_files(root)}
    changed = sorted(
        name for name in original.keys() | current.keys() if original.get(name) != current.get(name)
    )
    incompatible = [name for name in changed if not release_only_path(name)]
    if incompatible:
        raise ReleaseError(
            "Application/build inputs differ from the verified archive: " + ", ".join(incompatible)
        )
    if source_fingerprint(root) != fingerprint:
        raise ReleaseError("Source changed while comparing the original build archive")
    report.update(
        publication_source_fingerprint=fingerprint,
        build_source_archive_sha256=sha256(source_archive),
        publication_only_changes=changed,
    )
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
            if "HTTP 404" in (error.stderr or ""):
                # Drafts are visible to the owner in the list, but not the tag endpoint.
                if endpoint == f"repos/{self.repo}/releases/tags/{self.tag}":
                    releases: list[dict[str, Any]] = json.loads(
                        self.capture(self.gh, "api", f"repos/{self.repo}/releases?per_page=100")
                    )
                    found = next((item for item in releases if item["tag_name"] == self.tag), None)
                    if found is not None:
                        return found
                if missing_ok:
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

    def prepare_build(
        self, fingerprint: str, *, reuse_checks: bool, build_source_archive: Path | None = None
    ) -> dict[str, Any]:
        manifest = self.root / f"release/release-{self.version}.json"
        if build_source_archive is not None:
            report = validate_archived_build(
                self.root, manifest, self.version, fingerprint, build_source_archive
            )
            print(
                "REUSE original checked installer; application/build inputs match the verified archive",
                flush=True,
            )
            return report
        try:
            report = validate_build(manifest, self.version, fingerprint)
        except (OSError, ValueError, ReleaseError) as error:
            print(f"Local build required: {error}", flush=True)
        else:
            print(f"REUSE checked installer for source {fingerprint[:12]}", flush=True)
            return report
        if not reuse_checks:
            self.live(sys.executable, "-m", "scripts.local_ci")
        if source_fingerprint(self.root) != fingerprint:
            raise ReleaseError("Source changed during local verification; retry before publishing")
        # Local CI passed, or the caller explicitly reused completed targeted checks.
        self.live(sys.executable, "scripts/build_release.py", "--reuse-checks")
        return validate_build(manifest, self.version, fingerprint)

    def verify_assets(self, release: dict[str, Any], paths: list[Path]) -> None:
        assets = {asset["name"]: asset for asset in release["assets"]}
        if set(assets) != {asset_name(path) for path in paths}:
            raise ReleaseError("Release attachments do not match the planned four files")
        for path in paths:
            asset = assets[asset_name(path)]
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
                        asset_name(path),
                        "--dir",
                        temporary,
                    )
                    if sha256(Path(temporary) / asset_name(path)) != expected:
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
        if self.capture("git", "rev-parse", "origin/" + self.branch) != self.capture(
            "git", "rev-parse", "HEAD"
        ):
            self.live("git", "push", "origin", f"HEAD:refs/heads/{self.branch}")
        return str(release["html_url"])

    def publish(
        self, *, reuse_checks: bool = False, dry_run: bool = False, build_source_archive: Path | None = None
    ) -> str:
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
            if build_source_archive is not None:
                validate_archived_build(
                    self.root,
                    self.root / f"release/release-{self.version}.json",
                    self.version,
                    source_fingerprint(self.root),
                    build_source_archive,
                )
                print(
                    "Original installer verified; application/build inputs match the source archive",
                    flush=True,
                )
            return f"https://github.com/{self.repo}/releases/tag/{self.tag}"
        fingerprint = source_fingerprint(self.root)
        report = self.prepare_build(
            fingerprint, reuse_checks=reuse_checks, build_source_archive=build_source_archive
        )
        folder = self.root / "release"
        manifest = folder / f"release-{self.version}.json"
        self.live(sys.executable, "scripts/check_publication.py", "--history", "--export")
        if source_fingerprint(self.root) != fingerprint:
            raise ReleaseError("Source changed after the build; retry before committing or pushing")
        if self.changed():
            self.live("git", "add", "--all")
            self.live("git", "commit", "-m", f"release: Harness Token Deck {self.version}")
        if self.changed() or source_fingerprint(self.root) != fingerprint:
            raise ReleaseError("Source changed during the release commit; retry before pushing")
        commit = self.capture("git", "rev-parse", "HEAD").strip()
        if self.capture("git", "rev-parse", "origin/" + self.branch).strip() != commit:
            self.live("git", "push", "origin", f"HEAD:refs/heads/{self.branch}")
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
            installer_asset=asset_name(installer),
        )
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        files = [installer, source, manifest]
        checksum = folder / "SHA256SUMS.txt"
        checksum.write_text(
            "".join(f"{sha256(path)} *{asset_name(path)}\n" for path in files), encoding="utf-8"
        )
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
        "--build-source-archive",
        type=Path,
        help="Reuse the original installer only if its source ZIP matches all application/build inputs",
    )
    parser.add_argument(
        "--proxy", help="Proxy URL, local port (e.g. 7890), or 'direct'; applies only to this process"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preflight only; no build, commit, push or release"
    )
    args = parser.parse_args()
    os.environ.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", GIT_TERMINAL_PROMPT="0")
    try:
        configure_proxy(args.proxy)
        publisher = Publisher(ROOT, find_gh(ROOT, args.gh))
        print(
            publisher.publish(
                reuse_checks=args.reuse_checks,
                dry_run=args.dry_run,
                build_source_archive=args.build_source_archive,
            ),
            flush=True,
        )
    except (ReleaseError, ValueError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print(f"Publish stopped: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr.strip(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
