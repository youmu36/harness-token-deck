"""Completed business days and appended corrections survive pagination and compaction."""

from datetime import timedelta
from pathlib import Path
from typing import Any

from htd.adapters.sqlite.rollup_repo import RollupRepository
from htd.adapters.sqlite.wallet_closing import bounds, close_days
from htd.adapters.sqlite.wallet_entries import daily_spending, wallet_entries
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.domain.common.time import Instant, parse_instant
from htd.domain.wallet.model import WalletEntry

from .collection_helpers import AT
from .test_wallet_storage import USD, consumption, vendor


def page(
    writer: WriteExecutor, cursor: int | None = None, limit: int = 50, kind: str = "all", offset: int = 0
) -> dict[str, Any]:
    return writer.execute(
        lambda w: wallet_entries(
            w.connection,
            "vendor",
            cursor,
            limit,
            kind,
            offset,
            daily=daily_spending(w.connection, "vendor", 0),
        )
    )


def entries(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result["entries"]


def test_today_is_live_estimate_and_next_day_has_stable_2359_label(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(vendor)
        writer.execute(lambda w: consumption(w, amount=USD, at=AT))
        writer.execute(lambda w: close_days(w, w.connection, at=AT))
        assert not page(writer)["entries"]
        tomorrow = Instant(AT.value + timedelta(days=1))
        writer.execute(lambda w: close_days(w, w.connection, at=tomorrow))
        row = page(writer)["entries"][0]
        assert row["id"] == "local-day:vendor:2026-09-30"
        assert row["occurredAt"] == "2026-09-30T15:59:00.000Z"
        assert row["amountPico"] == str(USD) and "23:59" in row["note"]


def test_late_repricing_keeps_original_and_appends_before_after_correction(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(vendor)
        original = writer.execute(lambda w: consumption(w, amount=USD, at=AT))
        tomorrow = Instant(AT.value + timedelta(days=1))
        writer.execute(lambda w: close_days(w, w.connection, at=tomorrow))

        def revise(w: UnitOfWork) -> None:
            repo = WalletRepository(w.connection)
            repo.append_with_offsets(
                w,
                WalletEntry(
                    "delta",
                    "vendor",
                    "consumption_delta",
                    USD,
                    AT,
                    repo.next_seq(),
                    usage_ref=original.usage_ref,
                    related_entry_id=original.id,
                    recalc_batch_id="batch",
                ),
                at=tomorrow,
            )
            close_days(w, w.connection, at=tomorrow)

        writer.execute(revise)
        rows = page(writer)["entries"]
        assert next(row for row in rows if row["kind"] == "consumption_daily")["amountPico"] == str(USD)
        assert any(f"{USD} → {2 * USD}" in row["note"] for row in rows)
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor").c) == 2 * USD


def test_offline_catchup_cold_restart_and_page_append_are_equivalent(tmp_path: Path) -> None:
    path = tmp_path / "a.db"
    with WriteExecutor(path) as writer:
        writer.execute(vendor)
        for i in range(3):
            writer.execute(
                lambda w, i=i: consumption(w, i, amount=USD + i, at=Instant(AT.value + timedelta(days=i)))
            )
        writer.execute(lambda w: close_days(w, w.connection, at=Instant(AT.value + timedelta(days=4))))
        expected = page(writer)["entries"]
        assert len(expected) == 3
        first = page(writer, limit=1)
        writer.execute(lambda w: consumption(w, 5, amount=USD, at=Instant(AT.value + timedelta(days=5))))
        writer.execute(lambda w: close_days(w, w.connection, at=Instant(AT.value + timedelta(days=6))))
        assert page(writer, cursor=first["nextCursor"])["entries"] == expected[1:]
        later = Instant(AT.value + timedelta(days=100))
        while writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=later)):
            pass
        before = page(writer)["entries"]
    with WriteExecutor(path) as writer:
        assert page(writer)["entries"] == before


def test_business_day_boundaries_support_dst_and_quarter_hour_zones() -> None:
    for day, hours in [("2026-03-08", 23), ("2026-11-01", 25)]:
        start, end = bounds(day, "America/New_York")
        assert (end.value - start.value).total_seconds() == hours * 3600
    start, end = bounds("2026-10-18", "Asia/Kathmandu")
    assert start == parse_instant("2026-10-17T18:15:00Z")
    assert end == parse_instant("2026-10-18T18:15:00Z")
