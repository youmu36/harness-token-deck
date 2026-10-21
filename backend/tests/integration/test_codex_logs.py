"""Inspected log shapes, optional-storage failures and collection supplement isolation."""

import sqlite3
from contextlib import closing
from pathlib import Path

import htd.adapters.sqlite.codex_logs_source as logs_source
import pytest
from htd.adapters.collectors.codex_logs import CodexLogs
from htd.adapters.sqlite.connection import ReadPool, open_connection
from htd.adapters.sqlite.migrate import MIGRATIONS, migrate
from htd.adapters.sqlite.sources_repo import SourcesRepository
from htd.adapters.sqlite.writer import WriteExecutor
from htd.application.ports import UnmeteredRequest
from htd.domain.common.testing import FakeClock
from htd.domain.common.time import Instant, parse_instant
from htd.domain.usage.model import Harness

from backend.tests.integration.scan_helpers import AT, count, scanner, sessions

START = parse_instant("2026-08-31T00:00:00Z")
END = parse_instant("2026-09-02T00:00:00Z")


def log_body(
    *, model: str = "gpt-5.6-luna", status: int = 200, method: str = "POST", path: str = "/responses"
) -> str:
    return (
        f'run{{model=gpt-6-astra}}:title{{model={model}}}:http{{method="{method}" path="{path}"}}: '
        f"Request completed method={method} url=https://example.test/v1{path} status={status} "
        'headers={"sensitive": "not-for-report"}'
    )


def create_logs(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE logs(id INTEGER PRIMARY KEY,ts INTEGER,ts_nanos INTEGER,"
        "target TEXT,feedback_log_body TEXT)"
    )
    return connection


def add_log(
    conn: sqlite3.Connection,
    at: Instant = AT,
    *,
    body: str | None = None,
    target: str = "codex_http_client::client",
    nanos: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO logs(ts,ts_nanos,target,feedback_log_body) VALUES(?,?,?,?)",
        (int(at.value.timestamp()), nanos, target, body if body is not None else log_body()),
    )
    conn.commit()


def test_success_only_explicit_title_transport_and_utc_days(tmp_path: Path) -> None:
    source = sessions(tmp_path)
    path = source.parent / "logs_2.sqlite"
    with closing(create_logs(path)) as conn:
        add_log(conn, START)
        add_log(conn)
        add_log(conn, body=log_body(status=201))
        add_log(conn, END)  # exclusive end
        for body in (
            log_body(status=503),
            log_body(status=404),
            log_body(method="GET"),
            log_body().replace(":title{", ":run{"),
            log_body(model="gpt-6-astra").replace(":title{", ":run{"),
            log_body(path="/responses/other"),
            log_body().replace("Request completed", "Retry"),
        ):
            add_log(conn, body=body)
        add_log(conn, target="feedback_tags")  # duplicate tracing record, never a request
    before = path.read_bytes()
    assert CodexLogs().read(str(source), START, END) == (
        UnmeteredRequest("2026-08-31", 1),
        UnmeteredRequest("2026-09-01", 2),
    )
    assert path.read_bytes() == before


@pytest.mark.parametrize("state", ["missing", "corrupt", "schema", "bad_timestamp", "busy"])
def test_unavailable_storage_is_silent_and_does_not_create(tmp_path: Path, state: str) -> None:
    source = sessions(tmp_path)
    path = source.parent / "logs_2.sqlite"
    conn: sqlite3.Connection | None = None
    try:
        if state == "corrupt":
            path.write_bytes(b"not a database")
        elif state == "schema":
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE logs(ts INTEGER)")
        elif state in ("bad_timestamp", "busy"):
            conn = create_logs(path)
            add_log(conn, nanos=-1 if state == "bad_timestamp" else 0)
            if state == "busy":
                conn.execute("BEGIN EXCLUSIVE")
        assert CodexLogs().read(str(source), START, END) is None
        assert path.exists() == (state != "missing")
    finally:
        if conn is not None:
            conn.close()


def test_nanosecond_boundaries_and_available_zero(tmp_path: Path) -> None:
    source = sessions(tmp_path)
    with closing(create_logs(source.parent / "logs_2.sqlite")) as conn:
        assert CodexLogs().read(str(source), START, END) == ()
        add_log(conn, nanos=999_000_000)
        add_log(conn, nanos=998_999_999)
    start = parse_instant("2026-09-01T00:00:00.999Z")
    end = parse_instant("2026-09-01T00:00:01.000Z")
    assert CodexLogs().read(str(source), start, end) == (UnmeteredRequest("2026-09-01", 1),)


def test_query_budget_discards_partial_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = sessions(tmp_path)
    with closing(create_logs(source.parent / "logs_2.sqlite")) as conn:
        add_log(conn)
    samples = iter((0.0, 3.0))
    monkeypatch.setattr(logs_source.time, "monotonic", lambda: next(samples, 3.0))
    assert CodexLogs().read(str(source), START, END) is None


def test_supplement_persisted_refreshed_without_billing_or_degraded_status(tmp_path: Path) -> None:
    source = sessions(tmp_path / "home")
    path = tmp_path / "ledger.sqlite3"
    clock = FakeClock(parse_instant("2026-09-01T00:00:01Z"))
    with closing(create_logs(source.parent / "logs_2.sqlite")) as logs:
        add_log(logs)
        with WriteExecutor(path) as writer, ReadPool(path) as pool:
            scan = scanner(writer, pool, tmp_path / "home", clock)
            scan.unmetered = CodexLogs()
            first = scan.run().sources[0]
            assert first.unmetered_requests == (UnmeteredRequest("2026-09-01", 1),)
            assert first.status == "watching" and first.diagnostics == ()
            assert count(writer, "identity") == count(writer, "inbox") == count(writer, "ledger") == 0
            add_log(logs)
            assert scan.run().sources[0].unmetered_requests == (UnmeteredRequest("2026-09-01", 2),)
            stored = SourcesRepository(pool.get()).get(Harness.CODEX)
            assert stored is not None and stored.unmetered_requests == (UnmeteredRequest("2026-09-01", 2),)
            assert stored.unmetered_requests is not None
            assert stored.unmetered_requests[0].to_dict() == {
                "date": "2026-09-01",
                "kind": "title_generation",
                "count": 2,
            }
        with ReadPool(path) as pool:
            source_record = SourcesRepository(pool.get()).get(Harness.CODEX)
            assert source_record is not None and source_record.unmetered_requests == stored.unmetered_requests


def test_log_failure_does_not_fail_scan_or_add_diagnostic(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sessions(home)

    class FailedLogs:
        def read(self, root: str, start: Instant, end: Instant) -> tuple[UnmeteredRequest, ...] | None:
            raise RuntimeError("private upstream error")

    with WriteExecutor(tmp_path / "ledger.sqlite3") as writer, ReadPool(writer.path) as pool:
        scan = scanner(writer, pool, home, FakeClock(AT))
        scan.unmetered = FailedLogs()
        result = scan.run().sources[0]
        assert result.unmetered_requests is None and result.status == "watching" and not result.diagnostics


def test_p34_upgrade_preserves_start_checkpoint_and_clears_supplement_on_path_change(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for script in sorted(MIGRATIONS.glob("*.sql"))[:5]:
        (migrations / script.name).write_bytes(script.read_bytes())
    path = tmp_path / "ledger.sqlite3"
    migrate(path, migrations=migrations)
    with closing(open_connection(path)) as conn:
        conn.execute(
            "INSERT INTO source VALUES('codex','original',?,?, 'watching','[]')",
            ("2026-09-01T00:00:00.000Z", "2026-09-01T00:00:00.000Z"),
        )
        conn.execute(
            "INSERT INTO checkpoint VALUES('codex','file','{\"offset\":123}',?)",
            ("2026-09-01T00:00:00.000Z",),
        )
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        repo = SourcesRepository(pool.get())
        source = repo.get(Harness.CODEX)
        assert source is not None and source.started_at == AT and source.unmetered_requests is None
        assert repo.checkpoint(Harness.CODEX, "file").state == {"offset": 123}
        writer.execute(
            lambda work: SourcesRepository(work.connection).finish_scan(
                work,
                Harness.CODEX,
                "original",
                at=AT,
                status="watching",
                diagnostics=(),
                unmetered_requests=(UnmeteredRequest("2026-09-01", 2),),
            )
        )
        writer.execute(
            lambda work: SourcesRepository(work.connection).set_path(
                work, Harness.CODEX, "replacement", available=True
            )
        )
        replaced = repo.get(Harness.CODEX)
        assert replaced is not None and replaced.started_at == AT and replaced.unmetered_requests is None
