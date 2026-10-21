"""Exercise storage startup, transactions, concurrency and recovery on real temporary databases."""

import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier, Event, get_ident

import pytest
from htd.adapters.sqlite.connection import ReadPool, open_connection
from htd.adapters.sqlite.migrate import MIGRATIONS, migrate
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htd.domain.common.errors import DomainError
from htd.version import app_version


def test_migration_is_idempotent_and_strict(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    migrate(path)
    migrate(path)
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0] >= 1
        assert (
            connection.execute("SELECT value FROM meta WHERE key='storage_generation'").fetchone()[0] == "3"
        )
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert (
            connection.execute("SELECT value FROM meta WHERE key='created_by_version'").fetchone()[0]
            == app_version()
        )
        assert all(row[5] == 1 for row in connection.execute("PRAGMA table_list") if row[1] == "preference")


def test_migration_checksum_tampering_fails(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    migrate(path)
    with closing(open_connection(path)) as connection:
        connection.execute("UPDATE schema_migration SET checksum='changed'")
    with pytest.raises(DomainError, match="校验") as error:
        migrate(path)
    assert error.value.code == "MIGRATION_CHECKSUM_MISMATCH"


def test_future_generation_is_rejected_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    migrate(path)
    with closing(open_connection(path)) as connection:
        connection.execute("UPDATE meta SET value='4' WHERE key='storage_generation'")
    before = path.read_bytes()
    with pytest.raises(DomainError) as error:
        migrate(path)
    assert error.value.code == "STORAGE_GENERATION_UNSUPPORTED"
    assert path.read_bytes() == before


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_core.sql").write_bytes((MIGRATIONS / "0001_core.sql").read_bytes())
    (migrations / "0002_broken.sql").write_text("CREATE TABLE doomed(id INTEGER) STRICT;\nINVALID SQL;\n")
    path = tmp_path / "data.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        migrate(path, migrations=migrations)
    with closing(open_connection(path)) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='doomed'").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0] == 1


def test_read_pool_per_thread_and_query_only(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    migrate(path)
    with ReadPool(path) as pool:
        assert pool.get() is pool.get()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            pool.get().execute("INSERT INTO preference VALUES ('x','1')")
        with ThreadPoolExecutor(max_workers=1) as threads:
            other = threads.submit(pool.get).result()
        assert other is not pool.get()
        assert pool.get().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(RuntimeError):
        pool.get()


def test_connection_pragmas_match_for_read_and_write(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    migrate(path)
    for query_only in (False, True):
        with closing(open_connection(path, query_only=query_only)) as connection:
            for name, expected in (
                ("synchronous", 1),
                ("foreign_keys", 1),
                ("busy_timeout", 5000),
                ("temp_store", 2),
                ("recursive_triggers", 1),
            ):
                assert connection.execute(f"PRAGMA {name}").fetchone()[0] == expected


def test_writer_rolls_back_then_continues_and_publishes_after_commit(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    published: list[object] = []
    with WriteExecutor(path, publish=published.append) as writer:

        def fail(work: UnitOfWork) -> None:
            work.connection.execute("INSERT INTO preference VALUES ('bad','1')")
            work.touch("preferences")
            work.emit("bad")
            raise ValueError("failure")

        with pytest.raises(ValueError):
            writer.submit(fail).result()

        def succeed(work: UnitOfWork) -> int:
            work.connection.execute("INSERT INTO preference VALUES ('good','1')")
            work.touch("preferences")
            work.touch("preferences")
            work.emit("good")
            return work.revision("preferences")

        assert writer.submit(succeed).result() == 1
        assert published == ["good"]
    with closing(open_connection(path)) as connection:
        assert (
            connection.execute("SELECT key FROM preference WHERE key IN ('good','bad')").fetchall()[0][0]
            == "good"
        )
        assert connection.execute("SELECT value FROM revision WHERE scope='preferences'").fetchone()[0] == 1


def test_twenty_submitters_run_on_one_thread_and_close_drains(tmp_path: Path) -> None:
    writer = WriteExecutor(tmp_path / "data.sqlite3")
    futures: list[Future[int]] = []
    identities: list[int] = []
    submitters = Barrier(20)

    def task(work: UnitOfWork) -> int:
        identities.append(get_ident())
        return (
            work.connection.execute(
                "INSERT INTO diagnostic_log VALUES(NULL,?,NULL,'test','')", ("2026-09-30T00:00:00.000Z",)
            ).lastrowid
            or 0
        )

    def submit(_: int) -> Future[int]:
        submitters.wait(timeout=2)
        return writer.submit(task)

    with ThreadPoolExecutor(max_workers=20) as threads:
        futures = list(threads.map(submit, range(20)))
    writer.close()
    assert sorted(future.result() for future in futures) == list(range(1, 21))
    assert len(set(identities)) == 1
    with pytest.raises(RuntimeError):
        writer.submit(task)


def test_priority_and_fifo(tmp_path: Path) -> None:
    started, release = Event(), Event()
    order: list[str] = []
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:

        def block(_: UnitOfWork) -> None:
            started.set()
            assert release.wait(2)

        first = writer.submit(block)
        assert started.wait(2)
        low = writer.submit(lambda _: order.append("low"), priority=20)
        high1 = writer.submit(lambda _: order.append("high1"), priority=0)
        high2 = writer.submit(lambda _: order.append("high2"), priority=0)
        release.set()
        for future in (first, low, high1, high2):
            future.result()
    assert order == ["high1", "high2", "low"]


def test_timeout_cancels_queued_work(tmp_path: Path) -> None:
    started, release = Event(), Event()
    ran: list[bool] = []
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:

        def block(_: UnitOfWork) -> None:
            started.set()
            assert release.wait(2)

        blocker = writer.submit(block)
        assert started.wait(2)
        with pytest.raises(DomainError) as error:
            writer.execute(lambda _: ran.append(True), timeout=0.01)
        assert error.value.code == "WRITE_TIMEOUT"
        release.set()
        blocker.result()
    assert ran == []


def test_task_timeout_error_is_not_a_wait_timeout(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:

        def fail(_: UnitOfWork) -> None:
            raise TimeoutError("source request timed out")

        with pytest.raises(TimeoutError, match="source request timed out"):
            writer.execute(fail)


def test_existing_schema_is_backed_up_before_next_migration(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_core.sql").write_bytes((MIGRATIONS / "0001_core.sql").read_bytes())
    migrate(path, migrations=migrations)
    with closing(open_connection(path)) as connection:
        connection.execute("INSERT INTO preference VALUES ('theme','\"dark\"')")
    (migrations / "0002_extra.sql").write_text("CREATE TABLE extra(id INTEGER PRIMARY KEY) STRICT;")
    migrate(path, migrations=migrations)
    backups = list((tmp_path / "backups").glob("htd-*.sqlite3"))
    assert len(backups) == 1
    with closing(open_connection(backups[0], query_only=True)) as connection:
        assert (
            connection.execute("SELECT value_json FROM preference WHERE key='theme'").fetchone()[0]
            == '"dark"'
        )
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='extra'").fetchall()
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='extra'").fetchone()


def test_foreign_key_violation_does_not_apply_migration(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_core.sql").write_bytes((MIGRATIONS / "0001_core.sql").read_bytes())
    (migrations / "0002_bad_fk.sql").write_text(
        "CREATE TABLE child(id TEXT PRIMARY KEY REFERENCES preference(key)) STRICT;\n"
        "INSERT INTO child VALUES ('missing');\n"
    )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        migrate(path, migrations=migrations)
    with closing(open_connection(path)) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name='child'").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0] == 1


def test_postcommit_listener_reads_committed_data_and_failure_keeps_commit(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    observations: list[str] = []

    def listen(_: object) -> None:
        with closing(open_connection(path, query_only=True)) as connection:
            observations.append(
                connection.execute("SELECT value_json FROM preference WHERE key='theme'").fetchone()[0]
            )
        raise ValueError("listener unavailable")

    with WriteExecutor(path, publish=listen) as writer:

        def task(work: UnitOfWork) -> str:
            work.connection.execute("INSERT INTO preference VALUES ('theme','1')")
            work.touch("preferences")
            work.emit("changed")
            return "committed"

        assert writer.execute(task) == "committed"
    assert observations == ["1"]
