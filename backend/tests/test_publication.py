"""Publication protection catches private data, including tracked/historical blobs."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from scripts.check_publication import ROOT, fixture_hashes, inspect, path_issue, secret_issues
from scripts.make_legacy_fixtures import build_database


@pytest.mark.parametrize(
    "name",
    [
        "docs/Feedback/fb.md",
        "Main/accounts.sqlite3",
        "backend/private.db",
        ".env.local",
        "packaging/signing.key",
    ],
)
def test_private_paths_are_rejected_even_if_force_added_to_git(name: str) -> None:
    assert path_issue(name) is not None
    assert path_issue(name, historical=True) is not None


def test_literal_credentials_are_detected_without_returning_their_value() -> None:
    credential = b"sk-" + b"Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56"
    content = b'api_key = "' + credential + b'"\n'
    findings = secret_issues(content)
    assert findings and all(rule in {"provider-token", "credential-literal"} for _, rule in findings)
    assert credential.decode() not in repr(findings)
    assert secret_issues(b'credential = "synthetic-remote-1234"') == []
    assert secret_issues(b"-----BEGIN " + b"PRIVATE KEY-----") == [(1, "private-key")]
    assert secret_issues(b"C:/Users/" + b"personal-owner/private") == [(1, "local-user-path")]
    assert secret_issues(b"C:/Users/Example/synthetic") == []


def test_database_exception_is_limited_to_the_reproducible_fixture_content(tmp_path: Path) -> None:
    hashes = fixture_hashes()
    for name, expected in hashes.items():
        path = tmp_path / Path(name).name
        build_database(path, path.stem)
        content = path.read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected
        assert inspect(name, content, hashes) == []
        assert inspect(name, content + b"private", hashes) == [f"{name}: synthetic-database-hash-mismatch"]


def test_gitignore_excludes_personal_data_and_keeps_the_three_synthetic_databases() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("Source archive has no Git checkout")
    names = [
        "docs/Feedback/fb.md",
        "Main/htd.sqlite3",
        "data/account.db",
        "logs/runtime.log",
        ".env",
        "secret.pem",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=ROOT,
        input=("\n".join([*names, *fixture_hashes()]) + "\n").encode(),
        capture_output=True,
        check=False,
    )
    # Source ZIPs have no Git metadata; path/content protections above still run.
    if result.returncode == 128:
        pytest.skip("Source archive has no Git checkout")
    assert result.returncode == 0
    assert set(result.stdout.decode().splitlines()) == set(names)
