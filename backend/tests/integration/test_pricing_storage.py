"""Verify immutable pricing histories, atomic switches, restart recovery and catalog loading cost."""

import logging
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from time import perf_counter

import pytest
from htu.adapters.sqlite.connection import open_connection
from htu.adapters.sqlite.models_repo import ModelsRepository
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.domain.common.errors import DomainError
from htu.domain.common.time import parse_instant
from htu.domain.pricing import rules
from htu.domain.pricing.catalog import Catalog
from htu.domain.pricing.model import (
    BillingRule,
    EditorHints,
    ModelRate,
    PriceGroup,
    PriceRates,
    PriceRevision,
    PriceVersion,
    VersionActivation,
)
from htu.domain.pricing.model_pool import add_models, remove_model, set_base_price, sync_models
from htu.domain.usage.model import Harness

AT = parse_instant("2026-09-30T00:00:00Z")


def create(work: UnitOfWork) -> Catalog:
    repository = PricingRepository(work.connection)
    old = repository.load()
    new = rules.create_group(old, group_id="g", version_id="v", revision_id="r", name="Default", at=AT)
    repository.save(work, old, new)
    return new


def test_append_restart_equal(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(create)

        def update(work: UnitOfWork) -> Catalog:
            repo = PricingRepository(work.connection)
            old = repo.load()
            rule = BillingRule("tier", PriceRates("2", "1", "3"), (7, 1), (2, 1), 480, 20)
            new = rules.save_version_prices(
                old,
                "v",
                (ModelRate("GPT", "GPT 模型", "1.5", "0", "2", rule),),
                revision_id="r2",
                expected_revision=old.revision,
                at=AT,
            )
            repo.save(work, old, new)
            return new

        expected = writer.execute(update)
    with closing(open_connection(path)) as connection:
        actual = PricingRepository(connection).load()
        assert actual == expected
        assert actual.fingerprint == expected.fingerprint
        assert actual.revisions[-1].models[0].billing_rule is not None


@pytest.mark.parametrize(
    "table,update",
    [
        ("price_revision", "fingerprint='changed'"),
        ("price_revision_model", "display_name='changed'"),
        ("version_activation", "effective_at=effective_at"),
        ("harness_selection", "effective_at=effective_at"),
    ],
)
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_append_only(tmp_path: Path, table: str, update: str, verb: str) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(create)

        def seed(work: UnitOfWork) -> None:
            repo = PricingRepository(work.connection)
            old = repo.load()
            new = rules.save_version_prices(
                old,
                "v",
                (ModelRate("gpt", "GPT", "1", "0", "2"),),
                revision_id="r2",
                expected_revision=old.revision,
                at=AT,
            )
            repo.save(work, old, new)

        writer.execute(seed)

        def select(work: UnitOfWork) -> None:
            repo = PricingRepository(work.connection)
            old = repo.load()
            plan = rules.plan_switch(
                old, Harness.CODEX, "g", "v", expected_revision=old.revision, operation_id="op", at=AT
            )
            repo.save(work, old, rules.apply_switch(old, plan))

        writer.execute(select)
    with (
        closing(open_connection(path)) as connection,
        pytest.raises(sqlite3.IntegrityError, match="append_only"),
    ):
        connection.execute(f"UPDATE {table} SET {update}" if verb == "UPDATE" else f"DELETE FROM {table}")


def test_group_delete_is_guarded_and_high_watermark_survives(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(create)

        def second(work: UnitOfWork) -> Catalog:
            repo = PricingRepository(work.connection)
            old = repo.load()
            new = rules.create_group(
                old, group_id="g2", version_id="v2", revision_id="r2", name="Unused", at=AT
            )
            repo.save(work, old, new)
            return new

        original = writer.execute(second)

        def direct(work: UnitOfWork) -> None:
            work.connection.execute("DELETE FROM price_group WHERE id='g2'")

        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            writer.execute(direct)

        def delete(work: UnitOfWork) -> Catalog:
            repo = PricingRepository(work.connection)
            return repo.delete_group(work, "g2")

        deleted = writer.execute(delete)
        assert sum(g.deleted_at is None for g in deleted.groups) == 1
        assert deleted.sequence == original.sequence

        def _create_next(work: UnitOfWork) -> Catalog:
            repo = PricingRepository(work.connection)
            old = repo.load()
            new = rules.create_group(old, group_id="g3", version_id="v3", revision_id="r3", name="New", at=AT)
            repo.save(work, old, new)
            return new

        recreated = writer.execute(_create_next)
        assert (
            min(record.seq for record in recreated.revisions if record.version_id == "v3") > original.sequence
        )
    with closing(open_connection(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM temp.allow_group_delete").fetchone()[0] == 0
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


def test_switch_restart_idempotence_and_stale_revision(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(create)

        def switch(work: UnitOfWork) -> Catalog:
            repo = PricingRepository(work.connection)
            old = repo.load()
            plan = rules.plan_switch(
                old, Harness.CODEX, "g", "v", expected_revision=1, operation_id="op", at=AT
            )
            new = rules.apply_switch(old, plan)
            repo.save(work, old, new)
            return new

        expected = writer.execute(switch)
    with WriteExecutor(path) as writer:
        assert writer.execute(switch) == expected

        def stale(work: UnitOfWork) -> None:
            repo = PricingRepository(work.connection)
            old = repo.load()
            repo.save(work, replace(old, revision=0), old)

        with pytest.raises(DomainError) as error:
            writer.execute(stale)
        assert error.value.code == "PRICING_REVISION_CONFLICT"


def test_models_exclusion_base_rates_and_hints(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    with WriteExecutor(path) as writer:
        writer.execute(create)

        def update(work: UnitOfWork) -> None:
            repo = ModelsRepository(work.connection)
            old = repo.load()
            new = add_models(old, ("GPT", "Other"))
            new = set_base_price(new, "gpt", PriceRates("1.5", None, "2"))
            new = remove_model(new, "Other")
            new = sync_models(new, ("Other", "Third"))
            repo.save(work, old, new, at=AT)

        writer.execute(update)

        def hint(work: UnitOfWork) -> None:
            repo = PricingRepository(work.connection)
            old = repo.load()
            repo.save(work, old, rules.save_editor_hints(old, EditorHints("v", "G P T", "1.75")))

        writer.execute(hint)
    with closing(open_connection(path)) as connection:
        pool = ModelsRepository(connection).load()
        assert pool.models == ("GPT", "Third")
        assert pool.excluded == ("other",)
        assert pool.base_for("GPT") == PriceRates("1.5", None, "2")
        assert PricingRepository(connection).load().hints == (EditorHints("v", "G P T", "1.75"),)


@pytest.mark.slow
def test_load_fifty_thousand_model_rows_under_200ms(tmp_path: Path) -> None:
    path = tmp_path / "data.sqlite3"
    models = tuple(ModelRate(f"model-{i}", f"Model {i}", "1.5", "0.5", "2") for i in range(50))
    groups = tuple(PriceGroup(f"g{g}", f"Group {g}", False, AT) for g in range(10))
    versions = tuple(PriceVersion(f"v{g}-{v}", f"g{g}", v + 1, "", AT) for g in range(10) for v in range(20))
    revisions = tuple(
        PriceRevision(f"r{i}-{r}", version.id, AT, i * 5 + r + 1, models)
        for i, version in enumerate(versions)
        for r in range(5)
    )
    activations = tuple(
        VersionActivation(group.id, f"v{i}-0", AT, 1001 + i) for i, group in enumerate(groups)
    )
    catalog = Catalog(groups, versions, revisions, activations, revision=1)
    with WriteExecutor(path) as writer:
        writer.execute(lambda work: PricingRepository(work.connection).save(work, Catalog(), catalog))
    with closing(open_connection(path, query_only=True)) as connection:
        start = perf_counter()
        loaded = PricingRepository(connection).load()
        seconds = perf_counter() - start
        assert loaded == catalog
        logging.getLogger(__name__).warning(
            "P2.2 catalog load: %.2f ms (10 groups/200 versions/1000 revisions/50000 models)", seconds * 1000
        )
        assert seconds < 0.2
