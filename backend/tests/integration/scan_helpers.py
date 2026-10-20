"""Real collector composition and synthetic requests for scan integration tests."""

import json
from pathlib import Path

from htu.adapters.collectors.claude_reader import ClaudeReader
from htu.adapters.collectors.codex_reader import CodexReader
from htu.adapters.collectors.discovery import Discovery
from htu.adapters.collectors.dsh_reader import DshReader
from htu.adapters.collectors.zcode_reader import ZcodeReader
from htu.adapters.sqlite.collection_store import SqliteCollectionStore
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.application.collection.billing import BillInbox
from htu.application.collection.scan import ScanRound
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.pricing.catalog import Catalog
from htu.domain.pricing.model import HarnessSelection, ModelRate, PriceGroup, PriceRevision, PriceVersion
from htu.domain.usage.model import Harness

AT = parse_instant("2026-09-01T00:00:00Z")
FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def headers() -> bytes:
    return b"".join((FIXTURES / "codex" / "desktop.jsonl").read_bytes().splitlines(keepends=True)[:2])


def request(index: int, at: Instant) -> bytes:
    record = json.loads((FIXTURES / "codex" / "desktop.jsonl").read_bytes().splitlines()[2])
    record["timestamp"] = format_instant(at)
    record["payload"]["response_id"] = f"response-{index}"
    return (json.dumps(record, separators=(",", ":")) + "\n").encode()


def sessions(home: Path) -> Path:
    path = home / ".codex" / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def seed_prices(work: UnitOfWork, at: Instant = AT, *, input_price: str = "1") -> None:
    old = PricingRepository(work.connection).load()
    rate = ModelRate("gpt-6-astra", "AsTra", input_price, "0.1", "2")
    catalog = Catalog(
        groups=(PriceGroup("g", "Default", False, at),),
        versions=(PriceVersion("v", "g", 1, "", at),),
        revisions=(PriceRevision("r", "v", at, 1, (rate,)),),
        selections=tuple(
            HarnessSelection(harness, "g", at, index + 2) for index, harness in enumerate(Harness)
        ),
        revision=old.revision + 1,
    )
    PricingRepository(work.connection).save(work, old, catalog)


def scanner(
    writer: WriteExecutor,
    pool: ReadPool,
    home: Path,
    clock: FakeClock,
    *,
    codex: CodexReader | None = None,
    batch_size: int = 500,
) -> ScanRound:
    store = SqliteCollectionStore(writer, pool)
    return ScanRound(
        store,
        Discovery(home=home, environ={}),
        {
            Harness.CLAUDE: ClaudeReader(),
            Harness.CODEX: codex or CodexReader(),
            Harness.ZCODE: ZcodeReader(),
            Harness.DSH: DshReader(),
        },
        BillInbox(store, clock, batch_size=batch_size),
        clock,
    )


def count(writer: WriteExecutor, table: str) -> int:
    return writer.execute(lambda w: w.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
