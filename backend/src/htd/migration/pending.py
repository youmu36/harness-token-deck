"""Retain accepted pending identity and parent evidence when activating a restored dataset."""

from dataclasses import replace

from htd.adapters.sqlite.key_codec import key_bytes
from htd.adapters.sqlite.session_activity import received as activity_received
from htd.adapters.sqlite.session_core import observe
from htd.adapters.sqlite.session_projection import received
from htd.adapters.sqlite.usage_codec import decode_event
from htd.domain.common.errors import DomainError
from htd.domain.common.time import parse_instant
from htd.domain.usage.session import UsageObservation
from htd.migration.legacy.reader import SQLiteConnection


def project(
    active: SQLiteConnection, target: SQLiteConnection, harness: str, key: bytes, event: str
) -> None:
    try:
        usage = decode_event(event)
    except (ValueError, DomainError, RecursionError):
        return  # Keep the original inbox; normal billing will report its corrupt evidence.
    meta = active.execute(
        "SELECT r.*,p.native_identity parent_identity FROM session_request r "
        "LEFT JOIN session_directory d ON d.ref=r.session_ref "
        "LEFT JOIN session_directory p ON p.ref=d.parent_ref WHERE r.harness=? AND r.dedupe_key=?",
        (harness, key),
    ).fetchone()
    if meta is None:
        return
    if (
        usage.harness.value != harness
        or key_bytes(usage.dedupe_key) != key
        or usage.occurred_at != meta["occurred_at"]
    ):
        return
    usage = replace(usage, parent_session_id=meta["parent_identity"])
    observation = UsageObservation(
        usage, parse_instant(meta["received_at"]), meta["watermark"], meta["origin"]
    )
    ref = observe(target, observation)
    activity_received(target, observation, ref)
    received(target, usage)
