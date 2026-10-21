"""End-to-end P3.2 scans, transaction recovery, pricing and wallet attribution."""

import json
import sqlite3
from pathlib import Path

import zstandard
from htd.adapters.collectors.codex_reader import CodexReader
from htd.adapters.collectors.discovery import source_key
from htd.adapters.sqlite.collection_store import SqliteCollectionStore
from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.wallet_repo import WalletRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.collection.billing import BillInbox
from htd.application.ports import CheckpointRecord, ReadBatch, UsageChanged
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant
from htd.domain.usage.model import Harness

from .scan_helpers import AT, FIXTURES, count, headers, request, scanner, seed_prices, sessions


def test_c15_long_reconciliation_restarts_archive_and_path_change(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    root = sessions(home)
    transcript = root / "usage.jsonl"
    transcript.write_bytes(headers())
    clock = FakeClock(AT)
    for segment in range(4):
        with WriteExecutor(path) as writer, ReadPool(path) as pool:
            if segment == 0:
                writer.execute(seed_prices)
                writer.execute(
                    lambda w: WalletRepository(w.connection).create_vendor(
                        w, vendor_id="vendor", name="Shop", at=AT, operation_id="vendor"
                    )
                )
                writer.execute(lambda w: WalletRepository(w.connection).link_group(w, "g", "vendor", at=AT))
            scan = scanner(writer, pool, home, clock)
            if segment == 0:
                scan.run()
            for offset in range(10):
                index = segment * 10 + offset
                clock.advance(1000)
                with transcript.open("ab") as stream:
                    stream.write(request(index, clock.now_utc()))
                result = scan.run()
                assert result.sources[0].status == "watching"
                assert not result.more
                assert count(writer, "ledger") == index + 1
                assert count(writer, "inbox") == 0
                if index == 14:
                    archive = root.parent / "archived_sessions"
                    archive.mkdir()
                    transcript = transcript.rename(archive / transcript.name)
                if index == 24:
                    root = home / "custom"
                    root.mkdir()
                    transcript = transcript.rename(root / transcript.name)
                    writer.execute(
                        lambda w, root=root: SourcesRepository(w.connection).set_path(
                            w, Harness.CODEX, str(root), available=True
                        )
                    )
            source = SourcesRepository(pool.get()).get(Harness.CODEX)
            assert source is not None and source.started_at == AT
    with WriteExecutor(path) as writer:
        amounts = writer.execute(lambda w: w.connection.execute("SELECT pico FROM ledger").fetchall())
        assert sum(int(row[0]) for row in amounts) == 40 * 84_000_000
        assert count(writer, "identity") == 40
        assert writer.execute(lambda w: WalletRepository(w.connection).totals("vendor")).c == 40 * 84_000_000


def test_c03_duplicate_archive_and_signature_cache(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    root = sessions(home)
    archive = root.parent / "archived_sessions"
    archive.mkdir()
    data = headers() + request(1, AT)
    (root / "one.jsonl").write_bytes(data)
    (archive / "copy.jsonl").write_bytes(data)
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        scan = scanner(writer, pool, home, FakeClock(AT))
        first = scan.run()
        assert first.sources[0].inserted == first.sources[0].duplicates == 1
        assert first.billing.batch.billed == 1
        assert scan.run().sources[0].files_read == 0
        (archive / "copy.jsonl").unlink()
        scan.run()
        # A replacement with the same original signature cannot inherit a removed file's cache.
        (archive / "copy.jsonl").write_bytes(data)
        assert scan.run().sources[0].files_read == 1
        assert count(writer, "ledger") == 1


def test_empty_prices_retains_even_corrupt_inbox_then_restart_recovers(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    (sessions(home) / "one.jsonl").write_bytes(headers() + request(1, AT) + request(2, AT))
    clock = FakeClock(AT)
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        result = scanner(writer, pool, home, clock).run()
        assert result.billing.blocked and count(writer, "inbox") == 2
        writer.execute(
            lambda w: w.connection.execute(
                "UPDATE inbox SET event_json='broken' WHERE dedupe_key=(SELECT MIN(dedupe_key) FROM inbox)"
            )
        )
        assert BillInbox(SqliteCollectionStore(writer, pool), clock).run().blocked
        assert count(writer, "inbox") == 2 and count(writer, "diagnostic_log") == 0
    # All scanner/reader/application objects are discarded between transactions A and B.
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        result = scanner(writer, pool, home, clock).run()
        assert result.billing.batch.billed == result.billing.batch.corrupt == 1
        assert count(writer, "ledger") == 1 and count(writer, "inbox") == 0
        assert count(writer, "diagnostic_log") == 1
        assert scanner(writer, pool, home, clock).run().billing.batch.billed == 0


def test_transaction_a_failure_does_not_cache_or_advance(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    file = sessions(home) / "one.jsonl"
    file.write_bytes(headers() + request(1, AT))
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        scan = scanner(writer, pool, home, FakeClock(AT))
        writer.execute(
            lambda w: w.connection.execute(
                "CREATE TRIGGER fail BEFORE INSERT ON checkpoint BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        )
        failed = scan.collect()
        assert failed[0].status == "unavailable"
        assert count(writer, "inbox") == count(writer, "identity") == count(writer, "checkpoint") == 0
        writer.execute(lambda w: w.connection.execute("DROP TRIGGER fail"))
        retried = scan.collect()
        assert retried[0].inserted == 1 and count(writer, "inbox") == 1


def test_path_race_rejects_old_batch_and_preserves_start(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    file = sessions(home) / "one.jsonl"
    file.write_bytes(headers() + request(1, AT))
    replacement = home / "new"
    replacement.mkdir()
    with WriteExecutor(path) as writer, ReadPool(path) as pool:

        class SwitchingReader(CodexReader):
            def read(self, path: str, checkpoint: CheckpointRecord, started_at: Instant) -> ReadBatch:
                result = super().read(path, checkpoint, started_at)
                writer.execute(
                    lambda w: SourcesRepository(w.connection).set_path(
                        w, Harness.CODEX, str(replacement), available=True
                    )
                )
                return result

        result = scanner(writer, pool, home, FakeClock(AT), codex=SwitchingReader()).run()
        assert result.sources[0].diagnostics == ("source_path_changed",)
        assert count(writer, "inbox") == count(writer, "checkpoint") == 0
        source = SourcesRepository(pool.get()).get(Harness.CODEX)
        assert source is not None and source.started_at == AT and source.path == str(replacement)


def test_more_batches_and_cached_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    file = sessions(home) / "one.jsonl"
    file.write_bytes(headers() + request(1, AT) + b"broken\n" + request(2, AT))
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        scan = scanner(writer, pool, home, FakeClock(AT), codex=CodexReader(line_limit=400, batch_limit=400))
        seen: list[str] = []
        result = scan.run()
        seen.append(result.sources[0].status)
        for _ in range(10):
            result = scan.run()
            seen.append(result.sources[0].status)
            if not result.more:
                break
        assert "baselining" in seen and result.sources[0].status == "degraded"
        assert "invalid_json" in result.sources[0].diagnostics and count(writer, "ledger") == 2
        cached = scan.run().sources[0]
        assert cached.files_read == 0 and cached.diagnostics == result.sources[0].diagnostics
        checkpoint = SourcesRepository(pool.get()).checkpoint(Harness.CODEX, source_key(file))
        assert checkpoint.state is not None and checkpoint.state["offset"] == file.stat().st_size


def test_all_three_sources_isolation_and_bounded_billing(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    (sessions(home) / "one.jsonl").write_bytes(headers() + request(1, AT))
    dsh = home / ".dsh" / "sessions"
    dsh.mkdir(parents=True)
    rows = [json.loads(line) for line in (FIXTURES / "dsh" / "session.jsonl").read_bytes().splitlines()]
    for row in rows:
        row["time"] = 1788220801000
    data = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    (dsh / "one.jsonl.zstd").write_bytes(zstandard.ZstdCompressor().compress(data))
    source_db = home / ".zcode" / "cli" / "db" / "db.sqlite"
    source_db.parent.mkdir(parents=True)
    with sqlite3.connect(source_db) as connection:
        connection.execute("CREATE TABLE model_usage(id TEXT)")
    events: list[object] = []
    with WriteExecutor(path, publish=events.append) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        scan = scanner(writer, pool, home, FakeClock(AT), batch_size=1)
        result = scan.run()
        assert result.sources[1].status == "unavailable"
        assert result.sources[2].inserted == 2 and result.billing.batch.billed == 3
        changed = [e for e in events if isinstance(e, UsageChanged)]
        assert len(changed) == 3 and [e.revision for e in changed] == [1, 2, 3]
        assert result.billing.batch.harnesses == (Harness.CODEX, Harness.DSH)
        rows = writer.execute(
            lambda w: w.connection.execute("SELECT * FROM ledger WHERE harness='dsh'").fetchall()
        )
        assert all(row["unpriced"] == 1 for row in rows)
        with sqlite3.connect(source_db) as connection:
            connection.execute("DROP TABLE model_usage")
            connection.executescript((FIXTURES / "zcode" / "schema.sql").read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO model_usage(id,session_id,logical_request_id,model_id,status,completed_at,"
                "raw_usage_json) VALUES('row','session','request','gpt-6-astra','error',1788220801000,?)",
                (json.dumps({"inputTokens": 100, "cacheReadTokens": 40, "outputTokens": 10}),),
            )
        recovered = scan.run()
        assert recovered.sources[1].status == "watching" and recovered.sources[1].inserted == 1
        assert recovered.billing.batch.billed == 1 and count(writer, "ledger") == 4


def test_zcode_wal_updates_pending_without_main_file_change(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    source_db = home / ".zcode" / "cli" / "db" / "db.sqlite"
    source_db.parent.mkdir(parents=True)
    connection = sqlite3.connect(source_db)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript((FIXTURES / "zcode" / "schema.sql").read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO model_usage(id,session_id,logical_request_id,status,completed_at) "
            "VALUES('id','s','r','running',1788220801000)"
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = source_db.stat()
        with WriteExecutor(path) as writer, ReadPool(path) as pool:
            writer.execute(seed_prices)
            scan = scanner(writer, pool, home, FakeClock(AT))
            assert scan.run().sources[1].inserted == 0
            connection.execute("UPDATE model_usage SET status='completed'")
            connection.commit()
            after = source_db.stat()
            assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
            result = scan.run()
            assert result.sources[1].inserted == 1 and result.billing.batch.billed == 1
    finally:
        connection.close()


def test_unavailable_start_boundary_and_startup_default_fallback(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    home = tmp_path / "home"
    clock = FakeClock(AT)
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        scan = scanner(writer, pool, home, clock)
        first = scan.run()
        assert all(source.status == "unavailable" for source in first.sources)
        source = SourcesRepository(pool.get()).get(Harness.CODEX)
        assert source is not None and source.started_at == AT
        root = sessions(home)
        (root / "one.jsonl").write_bytes(headers() + request(1, AT))
        writer.execute(
            lambda w: SourcesRepository(w.connection).set_path(
                w, Harness.CODEX, str(home / "missing"), available=False
            )
        )
        clock.advance(1000)
        # Startup auto-location may replace an invalid saved path, preserving the first boundary.
        recovered = scanner(writer, pool, home, clock).run()
        assert recovered.sources[0].inserted == 1
        source = SourcesRepository(pool.get()).get(Harness.CODEX)
        assert source is not None and source.started_at == AT and source.path == source_key(root)
