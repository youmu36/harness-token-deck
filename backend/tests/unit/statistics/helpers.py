from collections.abc import Iterable

from htd.domain.common.time import parse_instant
from htd.domain.statistics import Aggregator, LedgerRow, Summary
from htd.domain.statistics.window import Grain
from htd.domain.usage.model import Harness


def row(
    index: int,
    minute: int = 0,
    *,
    hour: int = 0,
    day: int = 13,
    harness: Harness = Harness.CODEX,
    session: str | None = "s",
    model: str | None = "a",
    display: str | None = None,
    input_count: int | None = 100,
    cache: int | None = 40,
    output: int | None = 10,
    pico: int = 84_000_000,
    incomplete: bool = False,
    unpriced: bool = False,
) -> LedgerRow:
    return LedgerRow(
        harness=harness,
        dedupe_key=f"{index:064x}",
        session_id=session,
        occurred_at=parse_instant(f"2026-09-{day:02}T{hour:02}:{minute:02}:00Z"),
        model_id=model,
        display_name=display,
        input=input_count,
        cache_read=cache,
        output=output,
        pico_usd=pico,
        incomplete=incomplete,
        unpriced=unpriced,
    )


def summarize(
    rows: Iterable[LedgerRow],
    *,
    start: str = "2026-09-13T00:00:00Z",
    end: str = "2026-09-13T02:00:00Z",
    grain: Grain | None = "hour",
    offset: int = 480,
    harness: Harness | str = "all",
) -> Summary:
    aggregator = Aggregator(offset, grain, parse_instant(start), parse_instant(end), harness=harness)
    for record in sorted(rows, key=lambda item: item.order_key):
        aggregator.push(record)
    return aggregator.finish()
