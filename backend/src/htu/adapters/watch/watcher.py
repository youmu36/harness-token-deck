"""Watch source directories with watchdog; event callbacks only wake the coordinator."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from htu.adapters.collectors.discovery import linked, source_key
from htu.application.ports import SourceRecord
from htu.domain.usage.model import Harness


class ObserverPort(Protocol):
    def schedule(self, event_handler: object, path: str, *, recursive: bool) -> object: ...

    def unschedule_all(self) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def join(self, timeout: float) -> None: ...

    def is_alive(self) -> bool: ...


class WakeHandler:
    def __init__(
        self,
        notify: Callable[[], None],
        targets: tuple[Path, ...] = (),
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        self.notify = notify
        self.targets = targets
        self.on_change = on_change

    def dispatch(self, event: object) -> None:
        # Ignore read/open notifications to avoid collection waking itself.
        if getattr(event, "event_type", None) not in ("created", "modified", "moved", "deleted"):
            return
        paths = tuple(
            path
            for path in (getattr(event, "src_path", None), getattr(event, "dest_path", None))
            if isinstance(path, str) and path
        )
        if self.targets:
            paths = tuple(path for path in paths if self._relevant(path))
            if not paths:
                return
        if getattr(event, "is_directory", None) is False:
            paths = tuple(
                path
                for path in paths
                if path.endswith((".jsonl", ".jsonl.zstd", ".sqlite", ".sqlite3", ".db", "-wal", "-shm"))
            )
            if not paths:
                return
        if self.on_change is not None:
            for path in paths:
                self.on_change(path)
        self.notify()

    def _relevant(self, path: str) -> bool:
        candidate = Path(source_key(path))
        return any(
            candidate.is_relative_to(target)
            or target.is_relative_to(candidate)
            or candidate in (Path(str(target) + "-wal"), Path(str(target) + "-shm"))
            for target in self.targets
        )


def new_observer() -> ObserverPort:
    from watchdog.observers import Observer

    return cast(ObserverPort, Observer())


def watch_targets(sources: tuple[SourceRecord, ...]) -> tuple[Path, ...]:
    targets: set[str] = set()
    for source in sources:
        path = Path(source.path)
        if not path.is_absolute():
            continue
        directories = [path.parent if source.harness == Harness.ZCODE else path]
        if source.harness == Harness.CODEX and path.name.lower() == "sessions":
            directories.append(path.parent / "archived_sessions")
        targets.update(source_key(p) for p in directories)
    return tuple(Path(p) for p in sorted(targets))


def watch_roots(sources: tuple[SourceRecord, ...]) -> tuple[str, ...]:
    roots: set[str] = set()
    for candidate in watch_targets(sources):
        target = candidate
        # Parent events are filtered against targets, avoiding wakeups from unrelated files.
        while not target.is_dir() and target != target.parent:
            target = target.parent
        if target.is_dir() and not any(linked(p) for p in (target, *target.parents)):
            roots.add(source_key(target))
    # One recursive parent watch covers all its children, including absent archives.
    return tuple(
        root
        for root in sorted(roots)
        if not any(Path(root).is_relative_to(other) for other in roots - {root})
    )


class Watcher:
    def __init__(
        self,
        *,
        factory: Callable[[], ObserverPort] = new_observer,
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        self._factory = factory
        self._on_change = on_change
        self._observer: ObserverPort | None = None
        self._roots: tuple[str, ...] = ()
        self._targets: tuple[Path, ...] = ()

    def refresh(self, sources: tuple[SourceRecord, ...], notify: Callable[[], None]) -> bool:
        try:
            roots = watch_roots(sources)
            targets = watch_targets(sources)
            if not roots:
                self.close()
                return False
            if self._observer is not None and not self._observer.is_alive():
                self.close()
            if self._observer is not None and (roots, targets) == (self._roots, self._targets):
                return True
            if self._observer is None:
                self._observer = self._factory()
                start = True
            else:
                self._observer.unschedule_all()
                start = False
            for root in roots:
                self._observer.schedule(WakeHandler(notify, targets, self._on_change), root, recursive=True)
            if start:
                self._observer.start()
            self._roots = roots
            self._targets = targets
            return self._observer.is_alive()
        except Exception:
            self.close()
            return False

    def close(self, timeout: float = 1) -> None:
        observer, self._observer = self._observer, None
        self._roots = ()
        self._targets = ()
        if observer is not None:
            observer.stop()
            if observer.is_alive():
                observer.join(timeout)
