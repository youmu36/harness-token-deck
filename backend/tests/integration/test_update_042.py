"""0.4.2: independent books, proven repricing, bounded daily TPS and rollback data."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.dsh_repair import repair
from htu.adapters.sqlite.ingest_repo import IngestRepository
from htu.adapters.sqlite.local_wallet import entries, reclassify, remote_summary, set_totals, summary
from htu.adapters.sqlite.output_speed import close_quiet, ranking, record, references
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.wallet_book import read
from htu.adapters.sqlite.wallet_closing import close_days
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.application.ports import BilledUsage
from htu.bootstrap import Application
from htu.domain.billing import estimate_cost
from htu.domain.common.errors import DomainError
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import Instant, format_instant, parse_instant
from htu.domain.pricing import rules
from htu.domain.pricing.model import ModelRate
from htu.domain.sources.dsh import DshContext, parse_record
from htu.domain.usage.model import Harness, Tokens, UsageEvent
from htu.ipc.dto.wallet import AccountEntriesResponse, DailyBookResponse, WalletChangeResponse

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.management_helpers import vendor as create_vendor
from backend.tests.integration.collection_helpers import AT, collect, usage
from backend.tests.integration.test_account_book import observe, setup
from backend.tests.integration.test_pricing_storage import create
from backend.tests.integration.test_wallet_storage import USD, consumption, recharge, vendor


def test_local_amount_edit_preserves_remote_book_and_legacy_balance(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer, ReadPool(writer.path) as readers:
        store = setup(writer, readers, "10", used=0)
        observe(store, 9 * USD, 5, USD)
        before = writer.execute(lambda w: remote_summary(w.connection, "vendor"))

        def edit(work: UnitOfWork) -> None:
            set_totals(
                work.connection,
                work,
                "vendor",
                {
                    "R": str(20 * USD),
                    "C": str(3 * USD),
                    "pastTopUpPico": str(2 * USD),
                    "pastConsumptionPico": str(USD),
                    "expectedWalletRevision": work.revision("wallet"),
                },
                "edit",
                AT,
            )

        writer.execute(edit)
        local = writer.execute(lambda w: summary(w.connection, "vendor"))
        assert (local["R"], local["C"], local["B"]) == tuple(map(str, (20 * USD, 3 * USD, 27 * USD)))
        assert local["pastTopUpPico"] == str(2 * USD)
        assert writer.execute(lambda w: remote_summary(w.connection, "vendor")) == before
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor").b) == int(local["B"])
    with WriteExecutor(tmp_path / "a.db") as writer:
        assert writer.execute(lambda w: summary(w.connection, "vendor")) == local


def test_local_command_replay_and_stale_revision_are_atomic(tmp_path: Path) -> None:
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as app:
        model = create_vendor(app)
        operation = str(uuid4())
        args = {
            "vendorId": model.id,
            "C": str(USD),
            "expectedWalletRevision": app.revisions()["wallet"],
            "operationId": operation,
        }
        first = invoke(app, "wallet.setLocalTotals", args, WalletChangeResponse)
        assert invoke(app, "wallet.setLocalTotals", args, WalletChangeResponse) == first
        assert first.vendor and first.vendor.localWallet and str(USD) == first.vendor.localWallet.C
        assert (
            app.writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM local_wallet_posting").fetchone()[0]
            )
            == 1
        )


def test_local_classification_cannot_reuse_original_amount(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(lambda w: vendor(w, initial="-5"))
        original = writer.execute(lambda w: entries(w.connection, "vendor", None, 10))["entries"][0]

        def change(work: UnitOfWork, amount: int) -> None:
            reclassify(
                work.connection,
                work,
                "vendor",
                {
                    "entryId": original["id"],
                    "amountPico": str(amount),
                    "targetKind": "consumption",
                    "expectedAccountRevision": work.revision("wallet"),
                },
                str(uuid4()),
                AT,
            )

        writer.execute(lambda w: change(w, 3 * USD))
        assert writer.execute(lambda w: summary(w.connection, "vendor"))["C"] == str(3 * USD)
        with pytest.raises(DomainError):
            writer.execute(lambda w: change(w, 3 * USD))
        page = writer.execute(lambda w: entries(w.connection, "vendor", None, 10))
        AccountEntriesResponse.model_validate(page)
        assert next(row for row in page["entries"] if row["id"] == original["id"])["availablePico"] == str(
            2 * USD
        )


def test_daily_book_retains_original_and_recalc_correction(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(vendor)
        writer.execute(recharge)
        writer.execute(consumption)
        tomorrow = Instant(AT.value + timedelta(days=1))
        for _ in range(5):
            writer.execute(lambda w: close_days(w, w.connection, at=tomorrow))
        result = DailyBookResponse.model_validate(
            writer.execute(lambda w: read(w.connection, "vendor", "local", "2026-09", tomorrow))
        )
        assert str(30 * USD) == result.C and str(100 * USD) == result.R
        assert not result.pending
        assert writer.execute(lambda w: read(w.connection, "vendor", "local", "2026-09", AT))["days"] == []


def test_proven_dsh_cache_repair_uses_frozen_price_and_is_idempotent(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(create)

        def prices(work: UnitOfWork) -> None:
            repo = PricingRepository(work.connection)
            old = repo.load()
            repo.save(
                work,
                old,
                rules.save_version_prices(
                    old,
                    "v",
                    (ModelRate("Model", "Model", "0.24", "0.0048", "0.96"),),
                    revision_id="r2",
                    expected_revision=old.revision,
                    at=AT,
                ),
            )

        writer.execute(prices)
        writer.execute(vendor)
        writer.execute(lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=AT))
        old = usage(101, harness=Harness.DSH, tokens=Tokens(313231, None, 0, 181))

        def quote(event: UsageEvent) -> BilledUsage:
            return BilledUsage(
                event,
                estimate_cost(event.tokens, ModelRate("Model", "Model", "0.24", "0.0048", "0.96"), AT),
                "g",
                "v",
                "r2",
                "Model",
            )

        collect(writer, (old,), harness=Harness.DSH)
        writer.execute(lambda w: IngestRepository(w.connection).bill_batch(w, quote, at=AT))
        writer.execute(lambda w: WalletRepository(w.connection).append_consumption(w, quote(old), at=AT))
        original = writer.execute(lambda w: dict(w.connection.execute("SELECT * FROM ledger").fetchone()))
        assert original["incomplete"] == 1
        collect(writer, (replace(old, tokens=Tokens(313231, 0, 0, 181)),), harness=Harness.DSH)
        assert writer.execute(lambda w: repair(w.connection, w, AT)) == 1
        assert writer.execute(lambda w: repair(w.connection, w, AT)) == 0
        after = writer.execute(lambda w: dict(w.connection.execute("SELECT * FROM ledger").fetchone()))
        assert int(after["pico"]) - int(original["pico"]) == 75_175_440_000
        assert after["incomplete"] == 0 and after["cache_read"] is None
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor").c) == int(
            after["pico"]
        )
        assert (
            writer.execute(lambda w: w.connection.execute("SELECT COUNT(*) FROM recalc_item").fetchone()[0])
            == 1
        )


def test_output_crosses_business_day_and_discards_consumed_raw_arrays(tmp_path: Path) -> None:
    before = parse_instant("2026-10-18T15:59:00Z")
    after = parse_instant("2026-10-18T16:01:00Z")
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(
            lambda w: record(
                w.connection,
                harness="dsh",
                model_key="m",
                display_name="M",
                output=100,
                occurred_at=format_instant(after),
                at=after,
                samples=[(format_instant(before), 100, 1000), (format_instant(after), 200, 1000)],
            )
        )
        later = Instant(after.value + timedelta(minutes=11))
        writer.execute(lambda w: close_quiet(w.connection, later))
        assert writer.execute(lambda w: ranking(w.connection, "dsh", ("m",)))["m"] == "100.0"
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM output_daily_speed").fetchone()[0]
            )
            == 1
        )
        frozen = writer.execute(
            lambda w: w.connection.execute("SELECT buckets_json,timing_json FROM output_live").fetchone()
        )
        assert frozen[0] == "{}" and json.loads(frozen[1]) == {"rate": "200.0", "timed": True}
        next_day = Instant(after.value + timedelta(days=1))
        writer.execute(lambda w: close_quiet(w.connection, next_day))
        assert writer.execute(lambda w: ranking(w.connection, "dsh", ("m",)))["m"] == "150.0"
        assert (
            writer.execute(
                lambda w: w.connection.execute("SELECT COUNT(*) FROM output_reference").fetchone()[0]
            )
            == 0
        )
        points = writer.execute(
            lambda w: references(
                w.connection, "dsh", "2020-01-01T00:00:00.000Z", format_instant(next_day), "m"
            )
        )
        assert len(points) == 2
        old = Instant(after.value + timedelta(days=40))
        assert (
            writer.execute(
                lambda w: references(
                    w.connection, "dsh", "2020-01-01T00:00:00.000Z", format_instant(old), "m"
                )
            )
            == []
        )
        assert writer.execute(lambda w: ranking(w.connection, "dsh", ("m",)))["m"] == "150.0"


def test_model_selection_applies_without_another_request_header() -> None:
    ctx = DshContext(session_id="s", raw_session_id="s", model_id="old")
    parse_record({"type": "model/selection", "data": {"model": "new", "provider": "test"}}, ctx)
    event = parse_record(
        {
            "type": "assistant/message",
            "time": int(AT.value.timestamp() * 1000),
            "data": {"turn": 1, "step": 1, "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}},
        },
        ctx,
    )
    assert isinstance(event, UsageEvent) and event.model_id == "new"


def test_local_daily_entry_uses_its_saved_business_timezone(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "a.db") as writer:
        writer.execute(vendor)
        writer.execute(
            lambda w: w.connection.execute(
                "INSERT INTO wallet_daily_close(vendor_id,day,timezone,original,current,seq,count) "
                "VALUES('vendor','2026-09-30','UTC','1','1',1,1)"
            )
        )
        page = writer.execute(lambda w: entries(w.connection, "vendor", None, 50))
        assert page["entries"][0]["occurredAt"] == "2026-09-30T23:59:00.000Z"
