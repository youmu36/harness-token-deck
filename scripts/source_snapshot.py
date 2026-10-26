"""Identify the exact public working-tree contents, including uncommitted changes."""

import hashlib
import subprocess
from pathlib import Path


def source_files(root: Path) -> list[tuple[str, Path]]:
    root = root.resolve()
    names = (
        subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    files: list[tuple[str, Path]] = []
    for name in sorted(set(names) - {""}):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Source snapshot contains an external or symbolic path: {name}")
        if not path.exists():
            continue  # A tracked deletion is absent from the proposed source tree.
        if not path.is_file():
            raise ValueError(f"Source snapshot contains a nonregular file: {name}")
        files.append((name, path))
    return files


def source_fingerprint(root: Path) -> str:
    digest = hashlib.sha256(b"htd-source-v1\0")
    for name, path in source_files(root):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        with path.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()
