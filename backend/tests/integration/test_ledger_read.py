"""Verify streamed statistics ordering, complete ledger snapshots and exact large amounts."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.ledger_read import LedgerReader
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.usage_codec import loads
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.application.ports import BilledUsage
from htd.domain.billing import CostEstimate
from htd.domain.common.errors import DomainError
from htd.domain.common.time import parse_instant
from htd.domain.pricing import rules
from htd.domain.statistics import Aggregator
from htd.domain.usage.model import Harness, Tokens, UsageEvent

from .collection_helpers import AT, END, collect, quote, usage


def bill(writer: WriteExecutor) -> None:
    writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))


def test_cursor_order_half_open_range_and_harness_filter(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    one = parse_instant("2026-09-30T01:00:00Z")
    with WriteExecutor(path) as writer:
        collect(
            writer,
            (usage(3, session="z"), usage(2, session="a", at=one), usage(1, session="a"), usage(9, at=END)),
        )
        collect(writer, (usage(4, harness=Harness.DSH),), harness=Harness.DSH)
        collect(
            writer,
            (usage(6, harness=Harness.ZCODE, session="a"), usage(5, harness=Harness.ZCODE, session=None)),
            harness=Harness.ZCODE,
        )
        bill(writer)
    with closing(open_connection(path, query_only=True)) as connection:
        reader = LedgerReader(connection)
        rows = list(reader.iter_rows(AT, END))
        assert [row.order_key for row in rows] == sorted(row.order_key for row in rows)
        assert [row.dedupe_key for row in rows] == [usage(i).dedupe_key for i in (1, 2, 3, 4, 5, 6)]
        assert [row.dedupe_key for row in reader.iter_rows(AT, one, harness=Harness.CODEX)] == [
            usage(i).dedupe_key for i in (1, 3)
        ]


def test_full_ledger_payload_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    event = replace(
        usage(1),
        source_kind="desktop_subagent",
        issues=("extra",),
        tokens=Tokens(input=100, cache_read=40, cache_write=10, output=10, reasoning=3),
    )
    with WriteExecutor(path) as writer:
        collect(writer, (event,))
        writer.execute(
            lambda work: IngestRepository(work.connection).bill_batch(
                work,
                lambda e: BilledUsage(
                    e, CostEstimate(84_000_000, False, False, "tier"), "g", "v", "r", "Named"
                ),
                at=AT,
            )
        )
    with closing(open_connection(path)) as connection:
        row = connection.execute("SELECT * FROM ledger").fetchone()
        assert row["request_id"] == event.request_id and row["source_kind"] == "desktop_subagent"
        assert row["cache_write"] == 10 and row["reasoning"] == 3
        assert loads(row["issues_json"]) == list(event.issues)
        assert tuple(row[name] for name in ("price_group_id", "price_version_id", "price_revision_id")) == (
            "g",
            "v",
            "r",
        )
        assert row["display_name"] == "Named" and row["rule_applied"] == "tier"
        assert row["pico"] == "84000000" and row["billed_at"] == "2026-09-30T00:00:00.000Z"


def test_large_money_flows_through_python_aggregation_exactly(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    amount = 2**126
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1), usage(2)))
        writer.execute(
            lambda work: IngestRepository(work.connection).bill_batch(
                work, lambda e: BilledUsage(e, CostEstimate(amount, False, False, "base")), at=AT
            )
        )
    with closing(open_connection(path)) as connection:
        aggregator = Aggregator(0, "hour", AT, END)
        for row in LedgerReader(connection).iter_rows(AT, END):
            aggregator.push(row)
        assert aggregator.finish().metrics.pico_usd == str(amount * 2)


@pytest.mark.parametrize(
    ("pico", "code"), [("01", "PICO_INVALID"), ("-0", "PICO_INVALID"), (str(2**127), "COST_OVERFLOW")]
)
def test_noncanonical_or_overflowing_ledger_money_is_rejected(tmp_path: Path, pico: str, code: str) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1),))
        bill(writer)
        writer.execute(lambda work: work.connection.execute("UPDATE ledger SET pico=?", (pico,)))
    with closing(open_connection(path)) as connection, pytest.raises(DomainError) as error:
        list(LedgerReader(connection).iter_rows(AT, END))
    assert error.value.code == code


def test_models_use_latest_original_name_and_counts_keep_retained_identities(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1, model="Model X"), usage(2, model="Other"), usage(3, model=None)))
        collect(writer, (usage(4, model="MODELX", harness=Harness.DSH),), harness=Harness.DSH)
        bill(writer)
        collect(writer, (usage(5, harness=Harness.ZCODE),), harness=Harness.ZCODE)
        writer.execute(lambda work: work.connection.execute("DELETE FROM ledger WHERE harness='codex'"))
    with closing(open_connection(path)) as connection:
        reader = LedgerReader(connection)
        assert list(reader.observed_models()) == ["MODELX"]
        assert list(reader.observed_models(harness=Harness.CODEX)) == []
        counts = reader.source_counts()
        assert [row.harness for row in counts] == list(Harness)
        assert (
            counts[0].identity_count,
            counts[0].processed_count,
            counts[0].inbox_count,
            counts[0].ledger_count,
        ) == (3, 3, 0, 0)
        assert (
            counts[1].identity_count,
            counts[1].processed_count,
            counts[1].inbox_count,
            counts[1].ledger_count,
        ) == (1, 0, 1, 0)
        assert (
            counts[2].identity_count,
            counts[2].processed_count,
            counts[2].inbox_count,
            counts[2].ledger_count,
        ) == (1, 1, 0, 1)


def test_observed_models_deduplicate_normalized_keys_and_ignore_unknown(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        collect(writer, (usage(1, model="Model X"), usage(2, model="Other"), usage(3, model=None)))
        collect(writer, (usage(4, model="MODELX", harness=Harness.DSH),), harness=Harness.DSH)
        bill(writer)
    with closing(open_connection(path)) as connection:
        reader = LedgerReader(connection)
        assert list(reader.observed_models()) == ["MODELX", "Other"]
        assert list(reader.observed_models(harness=Harness.CODEX)) == ["Model X", "Other"]


def test_stream_snapshot_excludes_concurrent_additions_and_closes_early(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer, closing(open_connection(path, query_only=True)) as connection:
        collect(writer, (usage(1), usage(2)))
        bill(writer)
        stream = LedgerReader(connection).iter_rows(AT, END)
        assert next(stream).dedupe_key == usage(1).dedupe_key
        collect(writer, (usage(3),))
        bill(writer)
        assert [row.dedupe_key for row in stream] == [usage(2).dedupe_key]
        another = LedgerReader(connection).iter_rows(AT, END)
        next(another)
        another.close()
        assert len(list(LedgerReader(connection).iter_rows(AT, END))) == 3


def test_price_group_deletion_keeps_ledger_reference(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:

        def create(work: UnitOfWork, suffix: str) -> None:
            repo = PricingRepository(work.connection)
            old = repo.load()
            new = rules.create_group(
                old,
                group_id="g" + suffix,
                version_id="v" + suffix,
                revision_id="r" + suffix,
                name="Group " + suffix,
                at=AT,
            )
            repo.save(work, old, new)

        writer.execute(lambda work: create(work, "1"))
        writer.execute(lambda work: create(work, "2"))
        collect(writer, (usage(1),))

        def priced(event: UsageEvent) -> BilledUsage:
            return replace(quote(event), group_id="g2", version_id="v2", revision_id="r2")

        writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, priced, at=AT))
        deleted = writer.execute(lambda work: PricingRepository(work.connection).delete_group(work, "g2"))
        assert deleted.group("g2").deleted_at is not None
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT price_group_id FROM ledger").fetchone()[0])
            == "g2"
        )
