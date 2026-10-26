"""Persistent session identity is separate from request deduplication and display aliases."""

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid5

from htd.domain.common.hashing import identity_key
from htd.domain.common.time import Instant
from htd.domain.usage.model import Harness, UsageEvent

SESSION_NAMESPACE = UUID("95d7bb3b-315c-4b4d-99c2-a3ea196f93d6")
RECENT_SESSION_IDLE = timedelta(minutes=20)


def session_reference(harness: Harness, native_identity: str) -> str:
    return str(
        uuid5(
            SESSION_NAMESPACE,
            json.dumps(
                [harness.value, native_identity],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
    )


def source_session_key(harness: Harness, native_identity: str) -> str:
    return identity_key([harness.value, native_identity])


@dataclass(frozen=True, slots=True)
class UsageObservation:
    event: UsageEvent
    received_at: Instant
    watermark: str
    origin: Literal["live", "history", "legacy"] = "live"


@dataclass(frozen=True, slots=True)
class EvidenceObservation:
    harness: Harness
    request_key: str
    received_at: Instant
    kind: Literal["compaction", "cache", "timing", "context"]
    value: int | None


@dataclass(frozen=True, slots=True)
class SourceObservation:
    harness: Harness
    observed_at: Instant
    state: Literal["reading", "ready", "paused", "disabled", "failed", "pending"]
    watermark: str | None = None
    capabilities: tuple[str, ...] = ()
