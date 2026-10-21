"""Shared synthetic collection and billing inputs without accessing source files."""

from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import BilledUsage, CommitBatchResult
from htd.domain.billing import CostEstimate
from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.usage.model import Harness, Tokens, UsageEvent

AT = parse_instant("2026-09-30T00:00:00Z")
END = parse_instant("2026-10-01T00:00:00Z")


def usage(
    index: int,
    *,
    harness: Harness = Harness.CODEX,
    at: Instant = AT,
    model: str | None = "Model",
    session: str | None = "session",
    tokens: Tokens | None = None,
) -> UsageEvent:
    return UsageEvent(
        harness,
        f"{index:064x}",
        session,
        f"request-{index}",
        model,
        format_instant(at),
        tokens or Tokens(input=100, cache_read=40, cache_write=0, output=10, reasoning=0),
        "completed" if harness == Harness.ZCODE else "reported",
        source_kind="desktop" if harness == Harness.CODEX else None,
    )


def ensure_source(writer: WriteExecutor, harness: Harness = Harness.CODEX) -> str:
    root = str(writer.path.parent / harness.value)
    writer.execute(
        lambda work: SourcesRepository(work.connection).ensure(work, harness, root, available=True)
    )
    return root


def collect(
    writer: WriteExecutor,
    events: tuple[UsageEvent, ...],
    *,
    harness: Harness = Harness.CODEX,
    offset: int = 1,
    at: Instant = AT,
) -> CommitBatchResult:
    root = ensure_source(writer, harness)
    return writer.execute(
        lambda work: IngestRepository(work.connection).commit_batch(
            work, harness, root, root + "/session.jsonl", {"offset": offset}, events, at=at
        )
    )


def quote(event: UsageEvent) -> BilledUsage:
    tokens = event.tokens
    return BilledUsage(
        event,
        CostEstimate(
            84_000_000,
            tokens.input is None or tokens.cache_read is None or tokens.output is None,
            event.model_id is None,
            "base",
        ),
        display_name="Display",
    )
