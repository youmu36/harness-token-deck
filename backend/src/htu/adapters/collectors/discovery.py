"""Locate source roots and discover transcripts without following links or junctions."""

import os
import stat
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from sys import getsizeof
from threading import Lock

from htu.application.ports import DiscoveryBatch, ScanTrigger
from htu.domain.usage.model import Harness


def source_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


@dataclass(frozen=True, slots=True)
class _Directory:
    signature: tuple[int, int, int]
    directories: tuple[Path, ...]
    files: tuple[str, ...]


class Discovery:
    def __init__(self, *, home: Path | None = None, environ: Mapping[str, str] | None = None) -> None:
        self.environ = os.environ if environ is None else environ
        self.home = home or Path(self.environ.get("USERPROFILE", str(Path.home())))
        self._directories: OrderedDict[tuple[Harness, str], _Directory] = OrderedDict()
        self._signatures: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._directory_sizes: dict[tuple[Harness, str], int] = {}
        self._directory_bytes = 0
        self._cache_at = time.monotonic()
        self._changes: set[str] = set()
        self._round_changes: tuple[Path, ...] = ()
        self._incremental = False
        self._full_at = float("-inf")
        self._lock = Lock()

    def invalidate(self, path: str) -> None:
        """Watch callbacks retain paths without accessing files or doing collection."""
        with self._lock:
            self._changes.add(source_key(path))
            if len(self._changes) > 8192:
                self._changes.clear()
                self._full_at = float("-inf")

    def release_cache(self) -> None:
        with self._lock:
            self._directories.clear()
            self._signatures.clear()
            self._directory_sizes.clear()
            self._directory_bytes = 0
            self._full_at = float("-inf")
            self._cache_at = time.monotonic()

    def diagnostics(self) -> dict[str, int]:
        with self._lock:
            return {
                "directories": len(self._directories),
                "signatures": len(self._signatures),
                "estimatedDirectoryBytes": self._directory_bytes,
            }

    def prepare(self, trigger: ScanTrigger) -> None:
        if time.monotonic() - self._cache_at >= 300:
            self.release_cache()
        with self._lock:
            self._round_changes = tuple(Path(path) for path in self._changes)
            self._changes.clear()
            now = time.monotonic()
            self._incremental = trigger == "notification" and now - self._full_at < 30
            if not self._incremental:
                self._full_at = now

    def _changed(self, path: Path, *, listing: bool = False) -> bool:
        return any(
            path == changed or path.is_relative_to(changed) or (listing and changed.parent == path)
            for changed in self._round_changes
        )

    def _directory(self, harness: Harness, directory: Path) -> _Directory:
        key = (harness, source_key(directory))
        with self._lock:
            cached = self._directories.get(key)
            if cached is not None:
                self._directories.move_to_end(key)
            if self._incremental and cached is not None and not self._changed(directory, listing=True):
                return cached
        info = directory.stat(follow_symlinks=False)
        signature = (info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            return _Directory(signature, (), ())
        if (
            cached is not None
            and cached.signature == signature
            and not self._changed(directory, listing=True)
        ):
            return cached
        directories: list[Path] = []
        files: list[str] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                # DirEntry caches Windows attributes; do not lstat every transcript again.
                attributes = entry.stat(follow_symlinks=False)
                if (
                    entry.is_symlink()
                    or getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and (
                    entry.name.endswith(".jsonl")
                    or (harness == Harness.DSH and entry.name.endswith(".jsonl.zstd"))
                ):
                    files.append(source_key(entry.path))
        result = _Directory(signature, tuple(directories), tuple(files))
        size = (
            getsizeof(key[1])
            + 256
            + sum(getsizeof(name) + 16 for name in files)
            + sum(getsizeof(str(path)) * 2 + 128 for path in directories)
        )
        with self._lock:
            previous = self._directory_sizes.pop(key, 0)
            self._directories.pop(key, None)
            self._directory_bytes -= previous
            if size <= 2 * 1024 * 1024:
                while self._directories and (
                    len(self._directories) >= 1024 or self._directory_bytes + size > 2 * 1024 * 1024
                ):
                    removed, _ = self._directories.popitem(last=False)
                    self._directory_bytes -= self._directory_sizes.pop(removed)
                self._directories[key] = result
                self._directory_sizes[key] = size
                self._directory_bytes += size
        return result

    def default_path(self, harness: Harness) -> str:
        override = self.environ.get(f"HTU_SOURCE_ROOT_{harness.value.upper()}")
        if override:
            return source_key(override)
        if harness == Harness.CLAUDE:
            path = Path(self.environ.get("CLAUDE_CONFIG_DIR", str(self.home / ".claude"))) / "projects"
        elif harness == Harness.CODEX:
            path = Path(self.environ.get("CODEX_HOME", str(self.home / ".codex"))) / "sessions"
        elif harness == Harness.ZCODE:
            path = self.home / ".zcode" / "cli" / "db" / "db.sqlite"
        else:
            path = self.home / ".dsh" / "sessions"
        return source_key(path)

    def available(self, harness: Harness, path: str) -> bool:
        candidate = Path(path)
        try:
            return (
                candidate.is_absolute()
                and not linked(candidate)
                and (candidate.is_file() if harness == Harness.ZCODE else candidate.is_dir())
            )
        except OSError:
            return False

    def roots(self, harness: Harness, path: str) -> tuple[Path, ...]:
        root = Path(path)
        roots = [root]
        archive = root.parent / "archived_sessions"
        if (
            harness == Harness.CODEX
            and root.name.lower() == "sessions"
            and archive.is_dir()
            and not linked(archive)
        ):
            roots.append(archive)
        return tuple(roots)

    def discover(self, harness: Harness, path: str) -> DiscoveryBatch:
        if not self.available(harness, path):
            code = (
                "source_database_unavailable" if harness == Harness.ZCODE else "source_directory_unavailable"
            )
            return DiscoveryBatch((), (code,), False)
        if harness == Harness.ZCODE:
            return DiscoveryBatch((source_key(path),))
        files: set[str] = set()
        diagnostics: set[str] = set()
        stack = list(self.roots(harness, path))
        while stack:
            directory = stack.pop()
            try:
                found = self._directory(harness, directory)
                stack.extend(found.directories)
                files.update(found.files)
            except OSError:
                diagnostics.add("source_directory_unavailable")
        return DiscoveryBatch(tuple(sorted(files)), tuple(sorted(diagnostics)), not diagnostics)

    def signature(self, harness: Harness, path: str) -> tuple[int, int] | None:
        # WAL commits and updates to pending rows need not change the main database file.
        if harness == Harness.ZCODE:
            return None
        with self._lock:
            cached = self._signatures.get(path)
            if cached is not None:
                self._signatures.move_to_end(path)
            if self._incremental and cached is not None and not self._changed(Path(path)):
                return cached
        info = Path(path).stat()
        signature = info.st_size, info.st_mtime_ns
        with self._lock:
            if path not in self._signatures and len(self._signatures) >= 8192:
                self._signatures.popitem(last=False)
            self._signatures[path] = signature
        return signature
