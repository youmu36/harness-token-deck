"""Setup upgrade, historical price fallback and silent title charges on isolated ledgers."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from htu.adapters.collectors.codex_logs import CodexLogs
from htu.adapters.sqlite.connection import ReadPool, open_connection
from htu.adapters.sqlite.migrate import MIGRATIONS, migrate
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.title_repo import TITLE_PICO, TitleRepository
from htu.adapters.sqlite.wallet_repo import WalletRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.application.ports import TitleRequest, UsageChanged, WalletChanged
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import parse_instant
from htu.domain.pricing.model import VersionActivation

from .scan_helpers import AT, count, headers, request, scanner, seed_prices, sessions
from .test_codex_logs import add_log, create_logs, log_body


def seed(work: UnitOfWork) -> None:
    seed_prices(work)
    wallet = WalletRepository(work.connection)
    wallet.create_vendor(work, vendor_id="vendor", name="Shop", at=AT, operation_id="vendor")
    wallet.link_group(work, "g", "vendor", at=AT)


def test_first_activation_does_not_zero_preceding_requests(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (sessions(home) / "one.jsonl").write_bytes(headers() + request(1, AT))
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as pool:
        writer.execute(seed)

        def later_activation(work: UnitOfWork) -> None:
            repo = PricingRepository(work.connection)
            old = repo.load()
            updated = replace(
                old,
                activations=(
                    VersionActivation(
                        "g",
                        "v",
                        parse_instant("2026-09-01T00:30:00Z"),
                        old.sequence + 1,
                    ),
                ),
                revision=old.revision + 1,
            )
            repo.save(work, old, updated)

        writer.execute(later_activation)
        assert scanner(writer, pool, home, FakeClock(AT)).run().billing.batch.billed == 1
        row = pool.get().execute("SELECT pico,unpriced,price_version_id FROM ledger").fetchone()
        assert tuple(row) == ("84000000", 0, "v")
        assert WalletRepository(pool.get()).totals("vendor").c == 84_000_000


def test_titles_fixed_once_across_rescan_restart_and_excluded_from_usage(tmp_path: Path) -> None:
    home = tmp_path / "home"
    source = sessions(home)
    clock = FakeClock(AT)
    events: list[object] = []
    path = tmp_path / "ledger.sqlite3"
    with closing(create_logs(source.parent / "logs_2.sqlite")) as logs:
        with WriteExecutor(path, publish=events.append) as writer, ReadPool(path) as pool:
            writer.execute(seed)
            scan = scanner(writer, pool, home, clock)
            scan.titles = CodexLogs()
            scan.run()  # Establish the real collection boundary.
            add_log(logs, body=log_body(model="another-title-model"))
            add_log(logs, body=log_body(status=503))
            add_log(logs, body=log_body().replace(":title{", ":run{"))
            clock.advance(1000)
            scan.run()
            scan.run()
            assert count(writer, "title_request") == 1
            assert count(writer, "ledger") == count(writer, "identity") == count(writer, "inbox") == 0
            assert WalletRepository(pool.get()).totals("vendor").c == TITLE_PICO
            assert not any(isinstance(event, UsageChanged) for event in events)
            assert len([event for event in events if isinstance(event, WalletChanged)]) == 1
        with WriteExecutor(path) as writer, ReadPool(path) as pool:
            scan = scanner(writer, pool, home, clock)
            scan.titles = CodexLogs()
            scan.run()
            add_log(logs, clock.now_utc())
            clock.advance(1000)
            scan.run()
            assert count(writer, "title_request") == count(writer, "wallet_entry") == 2
            assert WalletRepository(pool.get()).totals("vendor").c == 2 * TITLE_PICO


def test_title_wallet_failure_rolls_back_and_late_calibration_is_preserved(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as pool:
        writer.execute(seed)
        title = TitleRequest("a" * 64, AT)
        after = parse_instant("2026-09-01T00:01:00Z")
        writer.execute(
            lambda w: WalletRepository(w.connection).calibrate(
                w,
                "vendor",
                "50",
                expected_revision=w.revision("wallet"),
                at=after,
                operation_id="calibrate",
            )
        )
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail_title BEFORE UPDATE ON title_request "
                "BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError):
            writer.execute(lambda w: TitleRepository(w.connection).collect(w, (title,), at=after))
        assert count(writer, "title_request") == 0
        assert WalletRepository(pool.get()).totals("vendor").c == 0
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail_title"))
        writer.execute(lambda w: TitleRepository(w.connection).collect(w, (title,), at=after))
        totals = WalletRepository(pool.get()).totals("vendor")
        assert totals.c == TITLE_PICO and totals.b == 50 * 10**12


def test_missing_title_attribution_remains_pending_without_guessing(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as pool:
        writer.execute(seed_prices)
        title = TitleRequest("b" * 64, AT)
        writer.execute(lambda w: TitleRepository(w.connection).collect(w, (title,), at=AT))
        assert pool.get().execute("SELECT wallet_entry_id FROM title_request").fetchone()[0] is None

        def wallet(w: UnitOfWork) -> WalletRepository:
            return WalletRepository(w.connection)

        writer.execute(
            lambda w: wallet(w).create_vendor(w, vendor_id="v", name="Shop", at=AT, operation_id="v")
        )
        writer.execute(lambda w: wallet(w).link_group(w, "g", "v", at=AT))
        writer.execute(lambda w: TitleRepository(w.connection).collect(w, (), at=AT))
        assert WalletRepository(pool.get()).totals("v").c == TITLE_PICO


def test_existing_ledger_skips_welcome_but_fresh_ledger_does_not(tmp_path: Path) -> None:
    older = tmp_path / "older"
    older.mkdir()
    for script in sorted(MIGRATIONS.glob("*.sql"))[:12]:
        (older / script.name).write_bytes(script.read_bytes())
    path = tmp_path / "old.sqlite3"
    migrate(path, migrations=older)
    with closing(open_connection(path)) as db:
        db.execute("INSERT INTO preference VALUES('theme','\"dark\"')")
    migrate(path)
    with closing(open_connection(path)) as db:
        assert db.execute("SELECT value_json FROM preference WHERE key='guideSeen'").fetchone()[0] == "true"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    fresh = tmp_path / "fresh.sqlite3"
    migrate(fresh)
    with closing(open_connection(fresh)) as db:
        assert db.execute("SELECT value_json FROM preference WHERE key='guideSeen'").fetchone() is None
