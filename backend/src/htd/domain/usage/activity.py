"""One quiet policy for sessions, monitor freezing and account coverage."""

from datetime import timedelta
from typing import Literal

from htd.domain.common.time import Instant

QUIET = timedelta(minutes=10)


def activity_state(
    last_received: Instant,
    now: Instant,
    *,
    complete: bool,
    pending: bool = False,
) -> Literal["active", "quiet", "incomplete"]:
    if not complete or pending:
        return "incomplete"
    return "quiet" if now.value - last_received.value >= QUIET else "active"
