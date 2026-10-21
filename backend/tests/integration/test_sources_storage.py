"""Exercise checkpoint atomicity, source path races and first-scan boundaries."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.migrate import MIGRATIONS, migrate
from htd.adapters.sqlite.pricing_codec import fingerprint
from htd.adapters.sqlite.pricing_repo import PricingRepository
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.domain.common.errors import DomainError
from htd.domain.common.time import parse_instant
from htd.domain.pricing import rules
from htd.domain.usage.model import Harness

from .collection_helpers import AT, collect, ensure_source, usage


def test_C06_checkpoint_failure_rolls_back_identity_inbox_and_revision(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        root = ensure_source(writer)
        writer.execute(
            lambda work: IngestRepository(work.connection).commit_batch(
                work, Harness.CODEX, root, "file", {"offset": 0}, (), at=AT
            )
        )
        with closing(open_connection(path)) as connection:
            previous = connection.execute("SELECT value FROM revision WHERE scope='sources'").fetchone()[0]
        writer.execute(
            lambda work: work.connection.execute(
                "CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON checkpoint "
                "BEGIN SELECT RAISE(ABORT,'injected_checkpoint_failure'); END"
            )
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected_checkpoint_failure"):
            writer.execute(
                lambda work: IngestRepository(work.connection).commit_batch(
                    work, Harness.CODEX, root, "file", {"offset": 100}, (usage(1),), at=AT
                )
            )
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM identity").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
        assert SourcesRepository(connection).checkpoint(Harness.CODEX, "file").state == {"offset": 0}
        assert (
            connection.execute("SELECT value FROM revision WHERE scope='sources'").fetchone()[0] == previous
        )


def test_C07_old_path_submit_preserves_start_and_checkpoints(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    later = parse_instant("2026-09-30T01:00:00Z")
    with WriteExecutor(path) as writer:
        root = ensure_source(writer)
        collect(writer, (usage(1),))
        writer.execute(
            lambda work: SourcesRepository(work.connection).set_path(
                work, Harness.CODEX, str(tmp_path / "new"), available=True
            )
        )
        with pytest.raises(DomainError) as error:
            writer.execute(
                lambda work: IngestRepository(work.connection).commit_batch(
                    work, Harness.CODEX, root, "file", {"offset": 500}, (usage(2),), at=later
                )
            )
        assert error.value.code == "source_path_changed"
    with closing(open_connection(path)) as connection:
        sources = SourcesRepository(connection)
        state = sources.get(Harness.CODEX)
        assert state is not None and state.started_at == AT and state.status == "pending"
        assert not state.diagnostics
        assert sources.checkpoint(Harness.CODEX, root + "/session.jsonl").state == {"offset": 1}
        assert sources.checkpoint(Harness.CODEX, "file").state is None
        assert connection.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1


def test_first_scan_filters_old_events_and_accepts_exact_boundary(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    old = parse_instant("2026-09-29T23:59:59.999Z")
    with WriteExecutor(path) as writer:
        result = collect(writer, (usage(1, at=old), usage(2), usage(2)))
        assert (result.inserted, result.duplicates, result.before_start) == (1, 1, 1)
    with closing(open_connection(path)) as connection:
        state = SourcesRepository(connection).get(Harness.CODEX)
        assert state is not None and state.started_at == AT
        assert connection.execute("SELECT dedupe_key FROM inbox").fetchone()[0] == usage(2).dedupe_key


def test_same_event_in_two_source_files_only_enters_once(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        first = collect(writer, (usage(1),))
        root = ensure_source(writer)
        second = writer.execute(
            lambda work: IngestRepository(work.connection).commit_batch(
                work, Harness.CODEX, root, root + "/archived.jsonl", {"offset": 100}, (usage(1),), at=AT
            )
        )
        assert first.inserted == 1 and second.inserted == 0 and second.duplicates == 1
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM identity").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0] == 2


@pytest.mark.parametrize("state_json", ["broken", "[]", "null", '{"offset":NaN}', '{"offset":1,"offset":2}'])
def test_invalid_checkpoint_returns_empty_state_and_diagnostic(tmp_path: Path, state_json: str) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        root = ensure_source(writer)
        writer.execute(
            lambda work: IngestRepository(work.connection).commit_batch(
                work, Harness.CODEX, root, "file", {"offset": 0}, (), at=AT
            )
        )
        writer.execute(
            lambda work: work.connection.execute("UPDATE checkpoint SET state_json=?", (state_json,))
        )
    with closing(open_connection(path)) as connection:
        checkpoint = SourcesRepository(connection).checkpoint(Harness.CODEX, "file")
        assert checkpoint.state is None and checkpoint.diagnostics == ("invalid_checkpoint",)


def test_finish_scan_sorts_diagnostics_and_rejects_old_path(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        root = ensure_source(writer)
        state = writer.execute(
            lambda work: SourcesRepository(work.connection).finish_scan(
                work, Harness.CODEX, root, at=AT, status="degraded", diagnostics=("b", "a", "a")
            )
        )
        assert state.last_scan_at == AT and state.diagnostics == ("a", "b")
        writer.execute(
            lambda work: SourcesRepository(work.connection).set_path(
                work, Harness.CODEX, str(tmp_path / "next"), available=False
            )
        )
        with pytest.raises(DomainError) as error:
            writer.execute(
                lambda work: SourcesRepository(work.connection).finish_scan(
                    work, Harness.CODEX, root, at=AT, status="watching", diagnostics=()
                )
            )
        assert error.value.code == "source_path_changed"


def test_wrong_harness_event_rolls_back_whole_batch(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer, pytest.raises(ValueError, match="Harness"):
        collect(writer, (usage(1), usage(2, harness=Harness.DSH)))
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM identity").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM checkpoint").fetchone()[0] == 0
        state = SourcesRepository(connection).get(Harness.CODEX)
        assert state is not None and state.started_at is None


def test_upgrade_from_pricing_schema_keeps_catalog_and_creates_collection_tables(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    for name in ("0001_core.sql", "0002_pricing.sql"):
        (earlier / name).write_bytes((MIGRATIONS / name).read_bytes())
    migrate(path, migrations=earlier)
    with closing(open_connection(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        # Seed the published v2 shape without asking current repositories to read it.
        from htd.domain.common.time import format_instant
        from htd.domain.pricing.catalog import Catalog

        expected = rules.create_group(
            Catalog(), group_id="g", version_id="v", revision_id="r", name="Group", at=AT
        )
        connection.execute("INSERT INTO price_group VALUES('g','Group',0,?,1)", (format_instant(AT),))
        connection.execute("INSERT INTO price_version VALUES('v','g',1,?,1)", (format_instant(AT),))
        revision = expected.revisions[0]
        connection.execute(
            "INSERT INTO price_revision VALUES('r','v',?,?,?)",
            (format_instant(AT), revision.seq, fingerprint(revision.models)),
        )
        activation = expected.activations[0]
        connection.execute(
            "INSERT INTO version_activation VALUES('g','v',?,?)", (format_instant(AT), activation.seq)
        )
        connection.execute("INSERT INTO history_seq(id) VALUES(?)", (activation.seq,))
        connection.execute("UPDATE revision SET value=? WHERE scope='pricing'", (expected.revision,))
        connection.commit()
    migrate(path)
    with closing(open_connection(path)) as connection:
        assert PricingRepository(connection).load() == expected
        for table in ("source", "checkpoint", "identity", "inbox", "ledger"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        tables = {row[1]: row[5] for row in connection.execute("PRAGMA table_list")}
        assert all(tables[name] == 1 for name in ("source", "checkpoint", "identity", "inbox", "ledger"))
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
    assert len(list((tmp_path / "backups").glob("htd-*.sqlite3"))) == 1
