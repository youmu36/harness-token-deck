"""Best-effort, query-only reads of the inspected Codex logs_2.sqlite schema."""

import hashlib
import re
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from htu.application.ports import TitleRequest, UnmeteredRequest
from htu.domain.common.time import Instant, from_epoch_ms

_FIELD = re.compile(r'\b(model|method|path|url|status)=(?:"([^"\r\n]*)"|([^\s}]+))')


def title_model(body: str) -> str | None:
    from htu.domain.usage.model import optional_text

    fields = {name: quoted or bare for name, quoted, bare in _FIELD.findall(body)}
    return optional_text(fields.get("model"))


def successful_title(body: str) -> bool:
    if "Request completed" not in body or re.search(r"(?:^|:)title\{", body) is None:
        return False
    fields: dict[str, str] = {name: quoted or bare for name, quoted, bare in _FIELD.findall(body)}
    if fields.get("method") != "POST":
        return False
    status = fields.get("status", "")
    if not re.fullmatch(r"2[0-9]{2}", status):
        return False
    route = urlsplit(fields["url"]).path if "url" in fields else fields.get("path", "")
    return route == "/responses" or route.endswith("/responses")


def read_title_events(path: Path, start: Instant, end: Instant) -> tuple[TitleRequest, ...] | None:
    """None means unavailable; an empty tuple is a successful query with zero requests."""
    try:
        deadline = time.monotonic() + 2
        with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute("BEGIN")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(logs)")}
            if not {"id", "ts", "ts_nanos", "target", "feedback_log_body"} <= columns:
                return None
            requests: list[TitleRequest] = []
            cursor = conn.execute(
                "SELECT id,ts,ts_nanos,feedback_log_body FROM logs WHERE ts>=? AND ts<=? "
                "AND target='codex_http_client::client' AND feedback_log_body LIKE '%title{%' "
                "AND feedback_log_body LIKE '%Request completed%' ORDER BY ts,ts_nanos,id",
                (int(start.value.timestamp()), int(end.value.timestamp())),
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
                if start <= at < end and type(body) is str and successful_title(body):
                    # Stable across log copies and path changes, with nanosecond precision.
                    key = hashlib.sha256(
                        f"codex:title:v1:{identifier}:{seconds}:{nanos}".encode()
                    ).hexdigest()
                    requests.append(TitleRequest(key, at, title_model(body)))
                if time.monotonic() >= deadline:
                    return None
            return tuple(requests)
    except Exception:
        # Optional, undocumented upstream storage cannot fail a scan or expose log contents.
        return None


def read_title_requests(path: Path, start: Instant, end: Instant) -> tuple[UnmeteredRequest, ...] | None:
    requests = read_title_events(path, start, end)
    if requests is None:
        return None
    counts = Counter(request.occurred_at.value.date().isoformat() for request in requests)
    return tuple(UnmeteredRequest(day, count) for day, count in sorted(counts.items()))
