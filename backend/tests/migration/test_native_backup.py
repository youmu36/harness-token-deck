"""Native backups use lossless restore, validation, activation and existing safeguards."""

from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from htu.adapters.sqlite.connection import open_connection
from htu.bootstrap import Application
from htu.domain.common.errors import DomainError
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import parse_instant
from htu.domain.usage.model import Tokens
from htu.ipc.dto.migration import MigrationRunResponse
from htu.ipc.dto.wallet import WalletListResponse
from htu.migration.convert import require_empty_target
from htu.migration.legacy.reader import backup
from htu.migration.snapshot import sha256_file
from htu.migration.verify import verify

from backend.tests.contracts.command_helpers import invoke
from backend.tests.contracts.maintenance_helpers import add_usage
from backend.tests.contracts.management_helpers import price
from backend.tests.contracts.test_unpriced_commands import setup

from .test_wizard import activate, finished
from .test_wizard import stopped as stopped


def test_native_backup_roundtrip_replay_and_tampered_staging_rejected(tmp_path: Path) -> None:
    clock = FakeClock(parse_instant("2026-10-17T00:00:00Z"))
    source = tmp_path / "backup.sqlite3"
    with Application(tmp_path / "source", clock=clock, start_scheduler=False) as app:
        _, v, _ = setup(app, clock)
        price(
            app,
            "pricing.saveVersionPrices",
            versionId=v,
            models=[{"modelId": "Model", "input": "2", "cache": "0", "output": "0"}],
        )
        from backend.tests.integration.collection_helpers import usage

        add_usage(app, [usage(2, at=clock.now_utc(), tokens=Tokens(1_000_000, 0, 0, 0, 0))])
        backup(app.writer.path, source)
        expected_wallet = invoke(app, "wallet.list", {}, WalletListResponse)
    before = sha256_file(source)
    with Application(tmp_path / "target", clock=clock, legacy_path=source, start_scheduler=False) as app:
        result = app.router.call("migration.run", {"operationId": str(uuid4()), "dryRun": True})
        assert result["ok"]
        started = MigrationRunResponse.model_validate(result["data"])
        completed = finished(app, started)
        assert completed.result and completed.result["ok"] is True
        frozen = app.data_dir / "migration" / started.migrationId / "legacy-snapshot.sqlite3"
        staging = frozen.with_name("staging.sqlite3")
        with closing(open_connection(staging)) as db:
            db.execute("UPDATE vendor_accounting SET past_top_up='1'")
        checked = verify(frozen, staging)
        assert not checked.ok and not checked.checks["vendor_accounting"]
        with pytest.raises(DomainError) as error:
            app.migration.engine.activate(started.migrationId, str(uuid4()))
        assert error.value.code == "MIGRATION_VERIFICATION_FAILED"
        with closing(open_connection(staging)) as db:
            db.execute("UPDATE vendor_accounting SET past_top_up='0'")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")
        operation = str(uuid4())
        assert activate(app, started, operation).activated
        assert activate(app, started, operation).activated
        actual = invoke(app, "wallet.list", {}, WalletListResponse)
        assert [
            v.model_dump(exclude={"walletRevision": True, "account": {"revision"}}) for v in actual.vendors
        ] == [
            v.model_dump(exclude={"walletRevision": True, "account": {"revision"}})
            for v in expected_wallet.vendors
        ]
    assert sha256_file(source) == before


def test_rolled_up_history_alone_makes_a_target_nonempty(tmp_path: Path) -> None:
    from htu.adapters.sqlite.rollup_repo import RollupRepository

    clock = FakeClock(parse_instant("2026-10-17T00:00:00Z"))
    with Application(tmp_path, clock=clock, start_scheduler=False) as app:
        setup(app, clock)
        app.writer.execute(lambda w: w.connection.execute("UPDATE ledger SET unpriced=0"))
        clock.advance(4 * 86_400_000)
        app.writer.execute(lambda w: RollupRepository(w.connection).compact(w, at=clock.now_utc()))
        with pytest.raises(DomainError) as error:
            require_empty_target(app.writer.path)
        assert error.value.code == "MIGRATION_TARGET_NOT_EMPTY"
