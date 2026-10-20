"""P4.4 real-router pricing combinations, failure atomicity and fresh billing."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from htu.adapters.sqlite.ingest_repo import IngestRepository
from htu.adapters.sqlite.sources_repo import SourcesRepository
from htu.adapters.sqlite.writer import UnitOfWork
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import format_instant, parse_instant
from htu.domain.usage.model import Harness
from htu.ipc.dto.pricing import ChangeResponse, PoolResponse, Rates
from pydantic import JsonValue

from backend.tests.integration.collection_helpers import usage

from .command_helpers import failure, invoke
from .management_helpers import catalog, group, price

AT = parse_instant("2026-09-30T00:00:00Z")
MODEL: dict[str, object] = {
    "modelId": "Model",
    "input": "1.500",
    "cache": "0",
    "output": "2",
}


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Application]:
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as instance:
        yield instance


def test_activate_unused_group_preserves_selections_and_replays(app: Application) -> None:
    g, v = group(app)
    added = price(app, "pricing.addVersion", groupId=g, number=2)
    target = added.versionId
    assert target
    payload = {
        "groupId": g,
        "versionId": target,
        "expectedRevision": app.revisions()["pricing"],
        "operationId": str(uuid4()),
    }
    first = app.router.call("pricing.activateVersion", payload)
    assert first["ok"] and app.router.call("pricing.activateVersion", payload) == first
    assert all(selection.groupId is None for selection in catalog(app).current.values())
    assert next(item for item in catalog(app).groups[0].versions if item.current).id == target
    price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=target)
    price(app, "pricing.switchCurrent", harness="zcode", groupId=g, versionId=target)
    activated = price(app, "pricing.activateVersion", groupId=g, versionId=v)
    assert activated.affectedHarnesses == ["codex", "zcode"]
    assert catalog(app).current["codex"].versionId == v
    assert not price(app, "pricing.activateVersion", groupId=g, versionId=v).changed
    other, other_version = group(app, "Other")
    assert (
        failure(
            app,
            "pricing.activateVersion",
            {
                **payload,
                "groupId": other,
                "expectedRevision": app.revisions()["pricing"],
                "operationId": str(uuid4()),
            },
        ).code
        == "VERSION_GROUP_MISMATCH"
    )
    price(app, "pricing.setGroupDisabled", groupId=other, disabled=True)
    assert (
        failure(
            app,
            "pricing.activateVersion",
            {
                **payload,
                "groupId": other,
                "versionId": other_version,
                "expectedRevision": app.revisions()["pricing"],
                "operationId": str(uuid4()),
            },
        ).code
        == "GROUP_DISABLED"
    )
    app.close()
    with Application(app.data_dir, start_scheduler=False) as restarted:
        assert restarted.router.call("pricing.activateVersion", payload) == first
        assert catalog(restarted).current["codex"].versionId == v


@pytest.mark.parametrize(
    ("change_group", "change_version"), [(False, False), (False, True), (True, False), (True, True)]
)
def test_switch_four_combinations_and_preview(
    app: Application, change_group: bool, change_version: bool
) -> None:
    g, v = group(app)
    price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
    price(app, "pricing.switchCurrent", harness="zcode", groupId=g, versionId=v)
    target_g, target_v = group(app, "Other") if change_group else (g, v)
    if change_group:
        price(app, "pricing.switchCurrent", harness="zcode", groupId=target_g, versionId=target_v)
    if change_version:
        added = price(app, "pricing.addVersion", groupId=target_g, number=2)
        assert added.versionId
        target_v = added.versionId
    before = app.revisions()["pricing"]
    preview = invoke(
        app,
        "pricing.previewSwitch",
        {"harness": "codex", "groupId": target_g, "versionId": target_v},
        ChangeResponse,
    )
    assert app.revisions()["pricing"] == before
    result = price(app, "pricing.switchCurrent", harness="codex", groupId=target_g, versionId=target_v)
    assert result.changed == preview.changed == (change_group or change_version)
    assert result.affectedHarnesses == preview.affectedHarnesses
    assert result.affectedHarnesses == (
        ["codex", "zcode"] if change_version else ["codex"] if change_group else []
    )
    assert result.revision == before + int(result.changed)
    current = catalog(app)
    assert current.current["codex"].groupId == target_g and current.current["codex"].versionId == target_v
    assert current.current["zcode"].versionId == target_v


def test_switch_receipt_replay_after_changes_and_restart(app: Application, tmp_path: Path) -> None:
    g, v = group(app)
    payload = {
        "harness": "codex",
        "groupId": g,
        "versionId": v,
        "expectedRevision": app.revisions()["pricing"],
        "operationId": str(uuid4()),
    }
    first = app.router.call("pricing.switchCurrent", payload)
    assert first["ok"]
    noop_payload = payload | {"expectedRevision": app.revisions()["pricing"], "operationId": str(uuid4())}
    noop = invoke(app, "pricing.switchCurrent", noop_payload, ChangeResponse)
    assert not noop.changed
    price(app, "pricing.renameGroup", groupId=g, name="Renamed")
    assert app.router.call("pricing.switchCurrent", payload) == first
    assert failure(app, "pricing.switchCurrent", payload | {"harness": "dsh"}).code == "OPERATION_ID_REUSED"
    app.close()
    with Application(tmp_path / "data", clock=FakeClock(AT), start_scheduler=False) as restarted:
        assert restarted.router.call("pricing.switchCurrent", payload) == first
        assert invoke(restarted, "pricing.switchCurrent", noop_payload, ChangeResponse) == noop
        assert catalog(restarted).current["codex"].versionId == v


@pytest.mark.parametrize("table", ["version_activation", "operation"])
def test_switch_injected_failure_rolls_back_timelines_receipt_and_events(
    app: Application, table: str
) -> None:
    g, _ = group(app)
    v = price(app, "pricing.addVersion", groupId=g, number=2).versionId
    app.events.attach(lambda script: None)
    before = catalog(app)

    def inject(work: UnitOfWork) -> None:
        work.connection.execute(
            f"CREATE TRIGGER injected BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END"
        )

    app.writer.execute(inject)
    payload = {
        "harness": "codex",
        "groupId": g,
        "versionId": v,
        "expectedRevision": before.revision,
        "operationId": str(uuid4()),
    }
    assert failure(app, "pricing.switchCurrent", payload).code == "INTERNAL_ERROR"
    assert catalog(app) == before and app.events.snapshot() == []
    app.writer.execute(lambda work: work.connection.execute("DROP TRIGGER injected"))
    assert invoke(app, "pricing.switchCurrent", payload, ChangeResponse).changed


@pytest.mark.parametrize("reference", ["ledger", "recalc_batch"])
def test_delete_keeps_ledger_and_recalculation_history(app: Application, reference: str) -> None:
    group(app)
    g, v = group(app, "Referenced")

    def insert(work: UnitOfWork) -> None:
        if reference == "ledger":
            work.connection.execute(
                "INSERT INTO ledger(harness,dedupe_key,request_id,model_key,occurred_at,status,issues_json,"
                "rule_applied,pico,incomplete,unpriced,billed_at,price_group_id,price_version_id) "
                "VALUES ('codex',unhex(printf('%064x',1)),'request','',?,'reported',"
                "'[]','base','0',0,0,?,?,?)",
                (format_instant(AT), format_instant(AT), g, v),
            )
        else:
            work.connection.execute(
                "INSERT INTO recalc_batch(id,harness,start_at,end_at,group_id,"
                "version_id,record_count,old_pico,new_pico,unpriced_count,"
                "incomplete_count,fingerprint,committed_at,operation_id) VALUES "
                "('batch','codex',?,?,?,?,0,'0','0',0,0,'fingerprint',?,'op')",
                (format_instant(AT), format_instant(AT), g, v, format_instant(AT)),
            )

    app.writer.execute(insert)
    assert catalog(app).groups[1].canDelete
    result = price(app, "pricing.deleteGroup", groupId=g)
    assert result.changed
    assert all(item.id != g for item in catalog(app).groups)
    assert (
        app.writer.execute(lambda w: w.connection.execute(f"SELECT COUNT(*) FROM {reference}").fetchone()[0])
        == 1
    )
    assert (
        app.writer.execute(
            lambda w: w.connection.execute("SELECT COUNT(*) FROM price_version WHERE id=?", (v,)).fetchone()[
                0
            ]
        )
        == 1
    )


def test_metadata_only_and_normalized_noop_do_not_append_revision(app: Application) -> None:
    g, v = group(app)
    first = price(app, "pricing.saveVersionPrices", versionId=v, models=[MODEL])
    same = price(
        app, "pricing.saveVersionPrices", versionId=v, models=[MODEL | {"input": "1.5", "output": "2.000000"}]
    )
    assert not same.changed and same.revision == first.revision
    price(
        app,
        "pricing.saveVersionPrices",
        versionId=v,
        models=[MODEL],
        hints=[{"modelId": "Model", "multiplier": "1.750"}],
    )
    price(app, "pricing.setVersionLabel", versionId=v, label="Custom")
    version = catalog(app).groups[0].versions[0]
    assert (
        version.revisionCount == 2
        and version.hints[0].multiplier == "1.75"
        and version.displayName == "Custom"
    )
    stale = failure(
        app,
        "pricing.saveVersionPrices",
        {"versionId": v, "models": [MODEL], "expectedRevision": 0, "operationId": str(uuid4())},
    )
    assert stale.code == "PRICING_REVISION_CONFLICT" and stale.conflictRevision == app.revisions()["pricing"]
    assert (
        failure(
            app,
            "pricing.setGroupDisabled",
            {"groupId": g, "disabled": "true", "expectedRevision": 0, "operationId": str(uuid4())},
        ).code
        == "INVALID_REQUEST"
    )


def test_new_inbox_billing_uses_new_revision_without_repricing_old_ledger(app: Application) -> None:
    g, v = group(app)
    price(app, "pricing.saveVersionPrices", versionId=v, models=[MODEL])
    price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
    assert isinstance(app.clock, FakeClock)

    def collect(index: int) -> None:
        event = usage(index, at=app.clock.now_utc())
        root = app.store.sources().sources[0].path

        def add(work: UnitOfWork) -> None:
            SourcesRepository(work.connection).begin_scan(work, Harness.CODEX, root, at=AT)
            IngestRepository(work.connection).commit_batch(
                work, Harness.CODEX, root, root + "/file", {"offset": index}, [event], at=app.clock.now_utc()
            )

        app.writer.execute(add)
        app.scan.billing.run()

    collect(1)
    app.clock.advance(1000)
    price(app, "pricing.saveVersionPrices", versionId=v, models=[MODEL | {"input": "3"}])
    collect(2)
    rows = (
        app.readers.get().execute("SELECT pico,price_revision_id FROM ledger ORDER BY dedupe_key").fetchall()
    )
    assert [row[0] for row in rows] == ["110000000", "200000000"]
    assert rows[0][1] != rows[1][1]


def test_model_editor_pool_exclusions_bases_and_multiplier(app: Application) -> None:
    added = price(app, "models.add", modelIds=["GPT", "gpt", "Other"])
    assert len(invoke(app, "models.pool", {}, PoolResponse).models) == 2
    assert not price(app, "models.add", modelIds=["GPT"]).changed
    price(app, "models.saveBaseRates", models=[{"modelId": "gpt", "rates": {"input": "1.500"}}])
    assert invoke(app, "models.pool", {}, PoolResponse).models[0].rates.input == "1.5"
    price(app, "models.remove", modelId="gpt")
    pool = invoke(app, "models.pool", {}, PoolResponse)
    assert pool.excluded == ["gpt"] and len(pool.models) == 1
    price(app, "models.restore", modelIds=["GPT"])
    assert invoke(app, "models.pool", {}, PoolResponse).excluded == []
    multiplied = invoke(
        app,
        "models.fillByMultiplier",
        {"rates": {"input": "1.5", "cache": "0.000001"}, "multiplier": "1.75"},
        Rates,
    )
    assert multiplied.input == "2.625" and multiplied.cache == "0.000002" and multiplied.output is None
    rounded = invoke(
        app, "models.fillByMultiplier", {"rates": {"input": "0.000001"}, "multiplier": "0.5"}, Rates
    )
    assert rounded.input == "0.000001"
    assert (
        failure(app, "models.fillByMultiplier", {"rates": {"input": "999999999"}, "multiplier": "2"}).code
        == "RATE_OVERFLOW"
    )
    assert (
        added.revision < app.revisions()["models"]
        and app.revisions()["pricing"] == 0
        and catalog(app).groups == []
    )


def test_concurrent_pricing_conflict_and_cross_wallet_operation_namespace(app: Application) -> None:
    requests = [{"name": name, "expectedRevision": 0, "operationId": str(uuid4())} for name in ("A", "B")]

    def send(payload: dict[str, object]) -> dict[str, JsonValue]:
        return app.router.call("pricing.createGroup", payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(send, requests))
    assert sum(bool(reply["ok"]) for reply in replies) == 1
    error = next(reply for reply in replies if not reply["ok"])
    assert (
        error["error"]
        and isinstance(error["error"], dict)
        and error["error"]["code"] == "PRICING_REVISION_CONFLICT"
    )
    used = next(payload for payload, reply in zip(requests, replies, strict=True) if reply["ok"])
    assert (
        failure(app, "wallet.createVendor", {"name": "Vendor", "operationId": used["operationId"]}).code
        == "OPERATION_ID_REUSED"
    )
    assert app.revisions()["wallet"] == 0


def test_copy_disable_delete_and_clock_rollback_rules(app: Application) -> None:
    g, v = group(app)
    price(
        app,
        "pricing.saveVersionPrices",
        versionId=v,
        models=[
            MODEL
            | {
                "billingRule": {
                    "mode": "peak",
                    "rates": {"input": "3"},
                    "weekdays": [2, 1],
                    "hours": [10, 9],
                    "offsetMinutes": 0,
                    "thresholdK": 100,
                }
            }
        ],
    )
    copied = price(app, "pricing.createGroup", name="Copy", copyFromGroupId=g)
    assert copied.groupId and copied.versionId
    copy_version = catalog(app).groups[1].versions[0]
    assert copy_version.models[0].billingRule and copy_version.models[0].billingRule.weekdays == [1, 2]
    version = price(
        app, "pricing.addVersion", groupId=copied.groupId, number=10, copyFromVersionId=copied.versionId
    )
    assert version.versionId and catalog(app).groups[1].versions[0].current
    price(app, "pricing.setGroupDisabled", groupId=copied.groupId, disabled=True)
    assert (
        failure(
            app,
            "pricing.switchCurrent",
            {
                "harness": "codex",
                "groupId": copied.groupId,
                "versionId": copied.versionId,
                "expectedRevision": app.revisions()["pricing"],
                "operationId": str(uuid4()),
            },
        ).code
        == "GROUP_DISABLED"
    )
    assert price(app, "pricing.deleteGroup", groupId=copied.groupId).changed
    assert len(catalog(app).groups) == 1
    price(app, "pricing.switchCurrent", harness="codex", groupId=g, versionId=v)
    assert (
        failure(
            app,
            "pricing.setGroupDisabled",
            {
                "groupId": g,
                "disabled": True,
                "expectedRevision": app.revisions()["pricing"],
                "operationId": str(uuid4()),
            },
        ).code
        == "GROUP_IN_USE"
    )
    assert isinstance(app.clock, FakeClock)
    app.clock.set_wall(parse_instant("2026-09-29T23:59:59Z"))
    assert (
        failure(
            app,
            "pricing.saveVersionPrices",
            {
                "versionId": v,
                "models": [],
                "expectedRevision": app.revisions()["pricing"],
                "operationId": str(uuid4()),
            },
        ).code
        == "CLOCK_BEHIND_PRICE_HISTORY"
    )
