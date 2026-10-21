"""Shared identity and freeze rules for the two current-session projections."""

import hashlib

from htd.adapters.sqlite.token_activity import QUIET
from htd.domain.common.time import Instant, parse_instant


def session_key(session: str) -> str:
    return hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]


def frozen(last_received_at: str, now: Instant) -> bool:
    return now.value - parse_instant(last_received_at).value >= QUIET
