"""Stable session identities survive copies, old inbox decoding, restart and bounded backfill."""

import json
from dataclasses import replace
from pathlib import Path

from htd.adapters.collectors.codex_reader import CodexReader
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.adapters.sqlite.session_core import backfill
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import CheckpointRecord
from htd.domain.usage.model import Harness
from htd.domain.usage.session import session_reference

from .collection_helpers import AT, collect, quote, usage
from .scan_helpers import AT as SOURCE_AT
from .scan_helpers import FIXTURES


def test_four_harnesses_share_contract_but_never_session_identity(tmp_path: Path) -> None:
    database = tmp_path / "data.sqlite3"
    with WriteExecutor(database) as writer:
        for index, harness in enumerate(Harness, 1):
            event = usage(index, harness=harness, session="same-native")
            assert collect(writer, (event,), harness=harness).inserted == 1
            assert collect(writer, (event,), harness=harness).duplicates == 1
            writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
        rows = writer.execute(
            lambda work: work.connection.execute(
                "SELECT ref,harness,started_at FROM session_directory ORDER BY harness"
            ).fetchall()
        )
        assert len(rows) == 4 and len({row[0] for row in rows}) == 4
        expected = {session_reference(harness, "same-native") for harness in Harness}
        assert {row[0] for row in rows} == expected
        assert (
            writer.execute(lambda work: work.connection.execute("SELECT COUNT(*) FROM ledger").fetchone()[0])
            == 4
        )
    with WriteExecutor(database) as writer:
        assert (
            set(
                writer.execute(
                    lambda work: [
                        row[0] for row in work.connection.execute("SELECT ref FROM session_directory")
                    ]
                )
            )
            == expected
        )


def test_original_identity_and_legacy_alias_collision_are_kept_separate(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        first = usage(1, session="session")
        other = replace(usage(2, session="session"), native_session_id=" session ")
        collect(writer, (first, other))
        assert (
            writer.execute(
                lambda work: work.connection.execute("SELECT COUNT(*) FROM session_directory").fetchone()[0]
            )
            == 2
        )
        assert (
            writer.execute(
                lambda work: work.connection.execute(
                    "SELECT COUNT(*) FROM session_identity_conflict WHERE reason='legacy_alias_collision'"
                ).fetchone()[0]
            )
            == 1
        )
        memento = writer.execute(
            lambda work: work.connection.execute("SELECT event_json FROM inbox LIMIT 1").fetchone()[0]
        )
        assert "native_session_id" not in json.loads(memento) and "parent_session_id" not in json.loads(
            memento
        )
        collect(writer, (replace(first, session_id="another", native_session_id="another"),))
        assert (
            writer.execute(
                lambda work: work.connection.execute("SELECT COUNT(*) FROM session_request").fetchone()[0]
            )
            == 2
        )


def test_verified_codex_parent_does_not_take_child_requests(tmp_path: Path) -> None:
    path = tmp_path / "child.jsonl"
    path.write_bytes((FIXTURES / "codex/fork-subagent.jsonl").read_bytes())
    batch = CodexReader().read(str(path), CheckpointRecord(), SOURCE_AT)
    assert len(batch.events) == 1
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        collect(writer, batch.events, at=SOURCE_AT)
        ref = session_reference(Harness.CODEX, "synthetic-child")
        row = writer.execute(
            lambda work: work.connection.execute(
                "SELECT parent_ref,started_at FROM session_directory WHERE ref=?", (ref,)
            ).fetchone()
        )
        assert row[0] == session_reference(Harness.CODEX, "synthetic-parent")
        assert (
            writer.execute(
                lambda work: work.connection.execute(
                    "SELECT COUNT(*) FROM session_request WHERE session_ref=?", (row[0],)
                ).fetchone()[0]
            )
            == 0
        )


def test_missing_session_remains_unassigned_and_backfill_resumes_without_money_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "data.sqlite3"
    with WriteExecutor(database) as writer:
        collect(writer, (usage(1, harness=Harness.ZCODE, session=None),), harness=Harness.ZCODE)
        assert (
            writer.execute(
                lambda work: work.connection.execute("SELECT session_ref FROM session_request").fetchone()[0]
            )
            is None
        )
        for index in range(2, 7):
            collect(writer, (usage(index, session=f"session-{index}"),))
        writer.execute(lambda work: IngestRepository(work.connection).bill_batch(work, quote, at=AT))
        before = writer.execute(
            lambda work: [
                tuple(row)
                for row in work.connection.execute("SELECT * FROM ledger ORDER BY harness,dedupe_key")
            ]
        )
        writer.execute(lambda work: work.connection.execute("DELETE FROM session_request"))
        assert writer.execute(lambda work: backfill(work.connection, limit=2)) == 2
    with WriteExecutor(database) as writer:
        assert writer.execute(lambda work: backfill(work.connection, limit=2)) == 2
        assert writer.execute(lambda work: backfill(work.connection, limit=2)) == 2
        assert writer.execute(lambda work: backfill(work.connection, limit=2)) == 0
        assert (
            writer.execute(
                lambda work: [
                    tuple(row)
                    for row in work.connection.execute("SELECT * FROM ledger ORDER BY harness,dedupe_key")
                ]
            )
            == before
        )
