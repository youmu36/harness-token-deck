"""Validate catalog command persistence and transaction failure recovery against real SQLite."""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from htu.adapters.sqlite.connection import open_connection
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.domain.common.errors import DomainError
from htu.domain.common.time import parse_instant
from htu.domain.pricing import rules
from htu.domain.pricing.catalog import Catalog
from htu.domain.pricing.model import EditorHints, ModelRate
from htu.domain.usage.model import Harness

AT = parse_instant("2026-09-30T00:00:00Z")


def command(writer: WriteExecutor, change: Callable[[Catalog], Catalog]) -> Catalog:
    def apply(work: UnitOfWork) -> Catalog:
        repo = PricingRepository(work.connection)
        old = repo.load()
        new = change(old)
        repo.save(work, old, new)
        return new

    return writer.execute(apply)


def seed(writer: WriteExecutor) -> Catalog:
    return command(
        writer,
        lambda old: rules.create_group(
            old, group_id="g", version_id="v", revision_id="r", name="Default", at=AT
        ),
    )


def switch(old: Catalog, *, operation: str = "op", target: str = "v2") -> Catalog:
    plan = rules.plan_switch(
        old, Harness.CODEX, "g", target, expected_revision=old.revision, operation_id=operation, at=AT
    )
    return rules.apply_switch(old, plan)


def test_combined_switch_failure_rolls_back_both_timelines(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        seed(writer)
        before = command(
            writer,
            lambda old: rules.add_version(
                old, "g", version_id="v2", revision_id="r2", number=2, label="New", at=AT
            ),
        )

        def inject(work: UnitOfWork) -> None:
            work.connection.execute(
                "CREATE TRIGGER fail_activation BEFORE INSERT ON version_activation "
                "WHEN NEW.version_id='v2' BEGIN SELECT RAISE(ABORT,'injected'); END"
            )

        writer.execute(inject)
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            command(writer, switch)
        with closing(open_connection(path, query_only=True)) as connection:
            assert PricingRepository(connection).load() == before
            assert connection.execute("SELECT COUNT(*) FROM harness_selection").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM operation").fetchone()[0] == 0
        writer.execute(lambda work: work.connection.execute("DROP TRIGGER fail_activation"))
        after = command(writer, switch)
        assert [record.seq for record in after.selections] == [before.sequence + 1]
        assert after.activations[-1].seq == before.sequence + 2


def test_price_noop_and_metadata_only_updates(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        seed(writer)
        first = command(
            writer,
            lambda old: rules.save_version_prices(
                old,
                "v",
                (ModelRate("GPT", "GPT", "1.500", "0", "2"), ModelRate("Other", "Other")),
                revision_id="r2",
                expected_revision=old.revision,
                at=AT,
            ),
        )
        repeated = command(
            writer,
            lambda old: rules.save_version_prices(
                old,
                "v",
                (ModelRate("Other", "Other"), ModelRate("GPT", "GPT", "1.5", "0.0", "2.000")),
                revision_id="unused",
                expected_revision=old.revision,
                at=AT,
            ),
        )
        assert first == repeated
        label = command(writer, lambda old: rules.set_version_label(old, "v", "Current"))
        hinted = command(writer, lambda old: rules.save_editor_hints(old, EditorHints("v", "GPT", "1.75")))
        renamed = command(writer, lambda old: rules.rename_group(old, "g", "Primary"))
        assert hinted.revisions == label.revisions == first.revisions
        assert renamed.sequence == first.sequence
    with closing(open_connection(path)) as connection:
        assert PricingRepository(connection).load() == renamed


def test_reusing_operation_id_from_another_command_is_structured(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        seed(writer)
        writer.execute(
            lambda work: work.connection.execute(
                "INSERT INTO operation VALUES ('op','other.command','{}','2026-09-30T00:00:00.000Z')"
            )
        )
        with pytest.raises(DomainError) as error:
            command(writer, lambda old: switch(old, target="v"))
        assert error.value.code == "OPERATION_ID_REUSED"


def test_noop_switch_receipt_survives_restart_without_increment(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        seed(writer)
        first = command(writer, lambda old: switch(old, target="v"))
        expected = command(writer, lambda old: switch(old, operation="noop", target="v"))
        assert expected.revision == first.revision
        assert expected.sequence == first.sequence
        assert not expected.switches[-1].changed
    with WriteExecutor(path) as writer:
        assert command(writer, lambda old: switch(old, operation="noop", target="v")) == expected


def test_global_sequence_includes_other_history_users(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        original = seed(writer)
        writer.execute(lambda work: work.connection.execute("INSERT INTO history_seq DEFAULT VALUES"))
        after = command(
            writer,
            lambda old: rules.save_version_prices(
                old,
                "v",
                (ModelRate("GPT", "GPT", "1", "0", "2"),),
                revision_id="r2",
                expected_revision=old.revision,
                at=AT,
            ),
        )
        assert after.revisions[-1].seq == original.sequence + 2


def test_insert_replace_does_not_bypass_append_only(tmp_path: Path) -> None:
    with WriteExecutor(tmp_path / "data.sqlite3") as writer:
        seed(writer)
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            writer.execute(
                lambda work: work.connection.execute(
                    "INSERT OR REPLACE INTO price_revision "
                    "SELECT id,version_id,effective_at,seq,'changed' FROM price_revision"
                )
            )
