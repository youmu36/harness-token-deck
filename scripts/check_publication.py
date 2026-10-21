"""Audit Git publication candidates/history without printing suspected secret values."""

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIRS = {"backend", "frontend", "fixtures", "scripts", "packaging", ".github"}
PUBLIC_FILES = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "VERSION",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "uv.lock",
}
PRIVATE_PARTS = {
    "docs",
    "main",
    "data",
    "logs",
    "backups",
    "private",
    "local",
    "screenshots",
    "reports",
    "node_modules",
    ".venv",
    "venv",
    ".verify",
    ".git",
    ".codex",
    ".playwright-cli",
    ".pytest_cache",
    ".hypothesis",
    ".ruff_cache",
    "__pycache__",
}
PRIVATE_NAMES = {
    "auth.json",
    "auth.toml",
    ".credentials.yaml",
    ".credentials.yml",
    ".netrc",
    ".npmrc",
    ".pypirc",
}
PRIVATE_SUFFIX = re.compile(
    r"\.(?:db|db3|sqlite|sqlite3|jsonl)(?:[.-].*)?$|"
    r"\.(?:pem|key|p12|pfx|exe|dll|msi|msix|bak|backup|dump|dmp|log|zip|7z)$",
    re.IGNORECASE,
)
SECRET_PATTERNS = {
    "local-user-path": re.compile(
        rb"(?:[A-Za-z]:[\\/]+Users[\\/]+[^\\/\x22\x27\r\n ]+|/(?:home|Users)/[A-Za-z0-9_.-]+)"
    ),
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "provider-token": re.compile(
        rb"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|"
        rb"github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})\b"
    ),
    "credential-literal": re.compile(
        rb"(?im)[\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|credential|authorization)[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{20,})[\"']"
    ),
}
SYNTHETIC = re.compile(
    rb"(?i)(?:synthetic|example|placeholder|redacted|private (?:prompt|secret)|test[-_ ]|fake[-_ ]|dummy)"
)


def git(*arguments: str, data: bytes | None = None) -> bytes:
    return subprocess.run(["git", *arguments], cwd=ROOT, input=data, capture_output=True, check=True).stdout


def fixture_hashes() -> dict[str, str]:
    expected = json.loads((ROOT / "fixtures/legacy/expected.json").read_text(encoding="utf-8"))
    return {f"fixtures/legacy/{name}": value["sha256"] for name, value in expected["databases"].items()}


def path_issue(name: str, *, historical: bool = False) -> str | None:
    parts = PurePosixPath(name).parts
    if not parts or ".." in parts:
        return "invalid-path"
    # Earlier AGENTS.md contains only project workflow rules, not runtime data.
    if historical and name in {"AGENTS.md", "LICENSE.pending"}:
        return None
    if parts[0] not in PUBLIC_DIRS and name not in PUBLIC_FILES:
        return "unreviewed-root-path"
    if set(part.lower() for part in parts[:-1]) & PRIVATE_PARTS:
        return "private-directory"
    leaf = parts[-1].lower()
    if leaf in PRIVATE_NAMES or (leaf.startswith(".env") and leaf != ".env.example"):
        return "private-configuration"
    if leaf.startswith(("id_rsa", "id_ed25519")):
        return "private-key-file"
    if name in {
        "fixtures/legacy/v022.sqlite3",
        "fixtures/legacy/v023.sqlite3",
        "fixtures/legacy/v030.sqlite3",
        "fixtures/codex/desktop.jsonl",
        "fixtures/codex/fork-subagent.jsonl",
        "fixtures/codex/repeated-snapshots.jsonl",
        "fixtures/dsh/session.jsonl",
    }:
        return None
    if PRIVATE_SUFFIX.search(leaf):
        return "runtime-or-private-file"
    return None


def secret_issues(content: bytes) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for rule, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(content):
            value = match[1] if match.lastindex else match[0]
            if rule != "private-key" and (
                SYNTHETIC.search(value) or len(set(value.replace(b"sk-", b""))) < 5
            ):
                continue
            findings.append((content.count(b"\n", 0, match.start()) + 1, rule))
    return findings


def inspect(name: str, content: bytes, hashes: dict[str, str], *, historical: bool = False) -> list[str]:
    issues: list[str] = []
    if issue := path_issue(name, historical=historical):
        issues.append(f"{name}: {issue}")
    if name in hashes and hashlib.sha256(content).hexdigest() != hashes[name]:
        issues.append(f"{name}: synthetic-database-hash-mismatch")
    issues.extend(f"{name}:{line}: {rule}" for line, rule in secret_issues(content))
    return issues


def history(hashes: dict[str, str]) -> tuple[int, list[str]]:
    objects = git("rev-list", "--objects", "--all").splitlines()
    names = {row.split(b" ", 1)[0]: row.split(b" ", 1)[1].decode("utf-8") for row in objects if b" " in row}
    if not names:
        return 0, []
    metadata = git("cat-file", "--batch-check", data=b"\n".join(names) + b"\n").splitlines()
    blobs = [row.split()[0] for row in metadata if row.split()[1] == b"blob"]
    output = git("cat-file", "--batch", data=b"\n".join(blobs) + b"\n")
    cursor = 0
    issues: list[str] = []
    for oid in blobs:
        boundary = output.index(b"\n", cursor)
        length = int(output[cursor:boundary].split()[2])
        content = output[boundary + 1 : boundary + 1 + length]
        issues.extend(
            f"history {oid[:12].decode()} {item}"
            for item in inspect(names[oid], content, hashes, historical=True)
        )
        cursor = boundary + 1 + length + 1
    return len(blobs), issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="Also scan all locally reachable Git history")
    parser.add_argument(
        "--export", action="store_true", help="Write an audited current-source ZIP under release/"
    )
    args = parser.parse_args()
    try:
        checkout = Path(git("rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
    except subprocess.CalledProcessError:
        parser.error(
            "A Git checkout is required; initialize an extracted source archive before auditing Git files"
        )
    if checkout != ROOT:
        parser.error("This source directory must be the Git checkout root")
    hashes = fixture_hashes()
    candidates = sorted(
        set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0"))
        - {""}
    )
    issues: list[str] = []
    contents: dict[str, bytes] = {}
    for name in candidates:
        path = ROOT / name
        if not path.exists():
            continue  # A tracked deletion is not part of the proposed tree.
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
            issues.append(f"{name}: external-or-nonregular-file")
            continue
        content = path.read_bytes()
        contents[name] = content
        issues.extend(inspect(name, content, hashes))
    print(f"Publication candidates: {len(contents)} files; {len(issues)} finding(s)")
    if args.history:
        count, historical_issues = history(hashes)
        issues.extend(historical_issues)
        print(f"Reachable Git history: {count} blobs; {len(historical_issues)} finding(s)")
    for issue in issues:
        print(f"FAIL {issue}")
    if issues:
        return 1
    if args.export:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        destination = ROOT / f"release/HarnessTokenDeck-{version}-source.zip"
        destination.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in contents.items():
                archive.writestr(name, content)
        print(f"Audited source archive: {destination.relative_to(ROOT).as_posix()}")
    print("No matching secret or private-file finding; manual review is still required.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
