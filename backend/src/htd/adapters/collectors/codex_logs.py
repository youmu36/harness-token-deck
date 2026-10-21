"""Expose optional Codex title counts without exposing logs or estimating their usage."""

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from struct import iter_unpack, pack
from threading import Lock
from zlib import compress, decompress

from htd.adapters.sqlite.codex_title_cache import Tail, read_title_update
from htd.application.ports import TitleRequest, UnmeteredRequest
from htd.domain.common.time import Instant, from_epoch_ms


@dataclass(frozen=True, slots=True)
class _Snapshot:
    path: Path
    identity: tuple[int, int]
    signature: tuple[tuple[int, int], ...]
    start: Instant
    checked_end: Instant
    tail: Tail | None
    requests: tuple[TitleRequest, ...] | bytes


def _requests(value: tuple[TitleRequest, ...] | bytes) -> tuple[TitleRequest, ...]:
    if isinstance(value, tuple):
        return value
    return tuple(
        TitleRequest(key.hex(), from_epoch_ms(at)) for key, at in iter_unpack("<32sq", decompress(value))
    )


def _retained(requests: tuple[TitleRequest, ...]) -> tuple[TitleRequest, ...] | bytes:
    if len(requests) <= 4096:
        return requests
    return compress(
        b"".join(
            pack(
                "<32sq", bytes.fromhex(request.dedupe_key), int(request.occurred_at.value.timestamp() * 1000)
            )
            for request in requests
        ),
        level=1,
    )


class CodexLogs:
    def __init__(self) -> None:
        self._snapshot: _Snapshot | None = None
        self._lock = Lock()

    def release_cache(self) -> None:
        with self._lock:
            self._snapshot = None

    def read_titles(self, root: str, start: Instant, end: Instant) -> tuple[TitleRequest, ...] | None:
        with self._lock:
            return self._read_titles(root, start, end)

    def _read_titles(self, root: str, start: Instant, end: Instant) -> tuple[TitleRequest, ...] | None:
        source = Path(root)
        home = source.parent if source.name.lower() in ("sessions", "archived_sessions") else source
        path = home / "logs_2.sqlite"
        try:
            info = path.stat()
            identity = (info.st_dev, info.st_ino)
            wal = Path(str(path) + "-wal")
            wal_info = wal.stat() if wal.exists() else None
            signature = (
                (info.st_size, info.st_mtime_ns),
                (wal_info.st_size, wal_info.st_mtime_ns) if wal_info else (0, 0),
            )
            old = self._snapshot
            reusable = old is not None and (old.path, old.identity) == (path, identity) and start >= old.start
            if not reusable or old is None or old.signature != signature or old.checked_end != end:
                update = read_title_update(path, start, old.tail if reusable and old else None)
                if update is None:
                    return None
                previous = _requests(old.requests) if reusable and old and not update.reset else ()
                requests = tuple(
                    request for request in (*previous, *update.requests) if request.occurred_at >= start
                )
                if len(requests) > 50_000:
                    return None
                self._snapshot = _Snapshot(
                    path, identity, signature, start, end, update.tail, _retained(requests)
                )
            assert self._snapshot is not None
            return tuple(
                request
                for request in _requests(self._snapshot.requests)
                if start <= request.occurred_at < end
            )
        except OSError:
            self._snapshot = None
            return None

    def read(self, root: str, start: Instant, end: Instant) -> tuple[UnmeteredRequest, ...] | None:
        requests = self.read_titles(root, start, end)
        if requests is None:
            return None
        counts = Counter(request.occurred_at.value.date().isoformat() for request in requests)
        return tuple(UnmeteredRequest(day, count) for day, count in sorted(counts.items()))
