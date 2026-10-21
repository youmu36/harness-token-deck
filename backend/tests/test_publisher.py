"""Publishing gates use temporary files and a fake CLI, never a GitHub account."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.publish_release import (
    PENDING_SUFFIX,
    Publisher,
    ReleaseError,
    archive_notes,
    asset_name,
    current_notes,
    network_command,
    repository,
    sha256,
    validate_build,
)

VERSION = "0.4.3"
RELEASE = {
    "draft": False,
    "published_at": "2026-10-21T16:30:00Z",
    "html_url": "https://github.com/example/project/releases/tag/v0.4.3",
}
NOTE = "### 修复\n\n- 缩短会话框。\n"


class FakePublisher(Publisher):
    def __init__(self, root: Path) -> None:
        self.root, self.gh = root, "gh"
        self.repo, self.version, self.tag, self.branch = "example/project", VERSION, "v0.4.3", "main"
        self.calls: list[tuple[str, ...]] = []
        self.source_dirty, self.notes_committed, self.fail_ci = True, False, False
        self.existing: dict[str, Any] | None = None
        self.assets: list[Path] = []
        (root / "CHANGELOG.md").write_text(
            f"# 当前更新说明\n\n## {VERSION}{PENDING_SUFFIX}\n\n{NOTE}", encoding="utf-8"
        )

    def preflight(self) -> dict[str, Any] | None:
        return self.existing

    def changed(self) -> set[str]:
        if self.source_dirty:
            return {"frontend/src/Monitor.vue"}
        if (self.root / ".github/release-notes/archive/0.4.3.md").exists() and not self.notes_committed:
            return {
                "CHANGELOG.md",
                ".github/release-notes/archive/0.4.3.md",
                ".github/release-notes/archive/README.md",
            }
        return set()

    def capture(self, *args: str) -> str:
        if args[:3] == ("git", "tag", "--list") or args[:3] == ("git", "diff", "--name-only"):
            return ""
        return "a" * 40 if args[-1] == "HEAD" else "b" * 40

    def live(self, *args: str) -> None:
        self.calls.append(args)
        if "scripts/build_release.py" in args:
            folder = self.root / "release"
            folder.mkdir(exist_ok=True)
            installer = folder / f"Harness Token Deck_{VERSION}_x64-setup.exe"
            installer.write_bytes(b"synthetic installer")
            report = {
                "version": VERSION,
                "checks": "reused targeted checks",
                "smoke": {"exit_code": 0, "integrity": "ok"},
                "installer": installer.name,
                "installer_bytes": installer.stat().st_size,
            }
            (folder / f"release-{VERSION}.json").write_text(json.dumps(report), encoding="utf-8")
        if "--export" in args:
            (self.root / f"release/HarnessTokenDeck-{VERSION}-source.zip").write_bytes(
                b"synthetic source archive"
            )
        if args[:2] == ("git", "commit"):
            self.source_dirty = False
            self.notes_committed |= args[-1].startswith("docs:")
        if args[:3] == ("gh", "release", "upload"):
            self.assets = [Path(value) for value in args[args.index("--clobber") + 1 :]]
        if "--draft=false" in args:
            assert not (self.root / ".github/release-notes/archive/0.4.3.md").exists()

    def wait_checks(self, commit: str) -> None:
        self.calls.append(("wait-ci", commit))
        if self.fail_ci:
            raise ReleaseError("synthetic CI failure")

    def api(self, endpoint: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
        public = any("--draft=false" in call for call in self.calls)
        return {
            **RELEASE,
            "draft": not public,
            "assets": [
                {
                    "name": asset_name(path),
                    "size": path.stat().st_size,
                    "state": "uploaded",
                    "digest": "sha256:" + sha256(path),
                }
                for path in self.assets
            ],
        }


@pytest.mark.parametrize(
    "url", ["https://github.com/example/project.git", "git@github.com:example/project.git"]
)
def test_origin_repository_is_derived_without_another_destination(url: str) -> None:
    assert repository(url) == "example/project"
    with pytest.raises(ReleaseError):
        repository("https://example.test/example/project.git")


def test_network_git_uses_current_cli_without_changing_global_credentials(tmp_path: Path) -> None:
    gh = tmp_path / "GitHub CLI/gh.exe"
    gh.parent.mkdir()
    gh.touch()
    command = network_command(("git", "push", "origin", "HEAD:main"), str(gh))
    assert command[-3:] == ("push", "origin", "HEAD:main")
    assert "credential.helper=" in command and "credential.https://github.com.helper=" in command
    assert command[-4].endswith("' auth git-credential")
    assert "GitHub CLI/gh.exe" in command[-4] and "--global" not in command
    assert network_command(("git", "commit", "-m", "release"), str(gh)) == ("git", "commit", "-m", "release")


def test_dry_run_cannot_build_commit_push_or_archive(tmp_path: Path) -> None:
    publisher = FakePublisher(tmp_path)
    before = (tmp_path / "CHANGELOG.md").read_bytes()
    assert publisher.publish(dry_run=True) == RELEASE["html_url"]
    assert publisher.calls == [] and (tmp_path / "CHANGELOG.md").read_bytes() == before


def test_ci_failure_does_not_create_tag_or_publish_a_release(tmp_path: Path) -> None:
    publisher = FakePublisher(tmp_path)
    publisher.fail_ci = True
    with pytest.raises(ReleaseError, match="CI failure"):
        publisher.publish(reuse_checks=True)
    assert not any(call[:2] == ("git", "tag") or call[0] == "gh" for call in publisher.calls)
    assert f"## {VERSION}{PENDING_SUFFIX}" in (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")


def test_complete_publish_checks_assets_before_publication_and_archives_afterward(tmp_path: Path) -> None:
    publisher = FakePublisher(tmp_path)
    assert publisher.publish(reuse_checks=True) == RELEASE["html_url"]
    assert len(publisher.assets) == 4
    manifest = json.loads((tmp_path / "release/release-0.4.3.json").read_text(encoding="utf-8"))
    assert manifest["git_commit"] == "a" * 40
    archived = (tmp_path / ".github/release-notes/archive/0.4.3.md").read_text(encoding="utf-8")
    assert "2026-10-22" in archived and NOTE in archived
    assert "暂无待发布更新" in (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8")
    public = next(i for i, call in enumerate(publisher.calls) if "--draft=false" in call)
    archived_commit = next(
        i for i, call in enumerate(publisher.calls) if call[-1] == "docs: archive 0.4.3 release notes"
    )
    assert public < archived_commit and publisher.notes_committed


def test_corrupt_uploaded_asset_is_rejected(tmp_path: Path) -> None:
    publisher = FakePublisher(tmp_path)
    path = tmp_path / "artifact"
    path.write_bytes(b"synthetic")
    with pytest.raises(ReleaseError, match="checksum mismatch"):
        publisher.verify_assets(
            {
                "assets": [
                    {
                        "name": path.name,
                        "size": path.stat().st_size,
                        "state": "uploaded",
                        "digest": "sha256:incorrect",
                    }
                ]
            },
            [path],
        )


def test_published_version_is_not_overwritten_and_notes_commit_can_resume(tmp_path: Path) -> None:
    publisher = FakePublisher(tmp_path)
    publisher.existing = dict(RELEASE)
    with pytest.raises(ReleaseError, match="already published"):
        publisher.publish()
    publisher.source_dirty = False
    archive_notes(tmp_path, VERSION, RELEASE, NOTE)
    assert publisher.publish() == RELEASE["html_url"]
    assert publisher.notes_committed and not any(call[0] == "gh" for call in publisher.calls)
    assert publisher.publish() == RELEASE["html_url"]


def test_changed_notes_and_unverified_build_cannot_be_released(tmp_path: Path) -> None:
    publisher = FakePublisher(tmp_path)
    assert current_notes(tmp_path, VERSION) == NOTE
    with pytest.raises(ReleaseError, match="notes changed"):
        archive_notes(tmp_path, VERSION, RELEASE, "different notes\n")
    publisher.live("python", "scripts/build_release.py")
    path = tmp_path / "release/release-0.4.3.json"
    report = json.loads(path.read_text(encoding="utf-8"))
    report["checks"] = "skipped (local debug)"
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ReleaseError, match="successful release build"):
        validate_build(path, VERSION)


def test_draft_lookup_uses_owner_list_when_tag_endpoint_returns_404(tmp_path: Path) -> None:
    publisher = FakePublisher(tmp_path)
    draft = {"tag_name": "v0.4.3", "draft": True, "id": 1}

    def capture(*args: str) -> str:
        if args[-1].endswith("/tags/v0.4.3"):
            raise subprocess.CalledProcessError(1, args, stderr="gh: Not Found (HTTP 404)")
        assert args[-1].endswith("/releases?per_page=100")
        return json.dumps([draft])

    publisher.capture = capture
    assert Publisher.api(publisher, "repos/example/project/releases/tags/v0.4.3") == draft


def test_installer_asset_name_matches_github_and_checksum(tmp_path: Path) -> None:
    publisher = FakePublisher(tmp_path)
    path = tmp_path / "Harness Token Deck_0.4.3_x64-setup.exe"
    path.write_bytes(b"synthetic")
    assert asset_name(path) == "Harness.Token.Deck_0.4.3_x64-setup.exe"
    publisher.verify_assets(
        {
            "assets": [
                {
                    "name": asset_name(path),
                    "size": path.stat().st_size,
                    "state": "uploaded",
                    "digest": "sha256:" + sha256(path),
                }
            ]
        },
        [path],
    )
