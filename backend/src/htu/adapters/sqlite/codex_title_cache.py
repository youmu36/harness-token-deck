"""Read only appended log IDs; keep optional log checkpoints and titles in memory."""

import hashlib
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from htu.adapters.sqlite.codex_logs_source import successful_title, title_model
from htu.application.ports import TitleRequest
from htu.domain.common.time import Instant, from_epoch_ms

type Tail = tuple[int, int, int, str]


@dataclass(frozen=True, slots=True)
class TitleLogBatch:
    tail: Tail | None
    requests: tuple[TitleRequest, ...]
    reset: bool


def read_title_update(path: Path, start: Instant, tail: Tail | None) -> TitleLogBatch | None:
    try:
        deadline = time.monotonic() + 2
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute("BEGIN")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(logs)")}
            if not {"id", "ts", "ts_nanos", "target", "feedback_log_body"} <= columns:
                return None
            reset = (
                tail is not None
                and conn.execute("SELECT id,ts,ts_nanos,target FROM logs WHERE id=?", (tail[0],)).fetchone()
                != tail
            )
            after = tail[0] if tail is not None and not reset else 0
            latest = conn.execute(
                "SELECT id,ts,ts_nanos,target FROM logs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            requests: list[TitleRequest] = []
            cursor = conn.execute(
                "SELECT id,ts,ts_nanos,feedback_log_body FROM logs WHERE id>? AND ts>=? "
                "AND target='codex_http_client::client' AND feedback_log_body LIKE '%title{%' "
                "AND feedback_log_body LIKE '%Request completed%' ORDER BY id",
                (after, int(start.value.timestamp())),
            )
            for identifier, seconds, nanos, body in cursor:
                if (
                    type(identifier) is not int
                    or type(seconds) is not int
                    or type(nanos) is not int
                    or not 0 <= nanos < 10**9
                ):
                    return None
                at = from_epoch_ms(seconds * 1000 + nanos // 10**6)
                if start <= at and type(body) is str and successful_title(body):
                    key = hashlib.sha256(
                        f"codex:title:v1:{identifier}:{seconds}:{nanos}".encode()
                    ).hexdigest()
                    requests.append(TitleRequest(key, at, title_model(body)))
                if time.monotonic() >= deadline or len(requests) > 50_000:
                    return None
            if time.monotonic() >= deadline:
                return None
            return TitleLogBatch(latest, tuple(requests), reset)
    except Exception:
        return None
