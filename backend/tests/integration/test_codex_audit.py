"""Audit coverage, pricing, comparisons, privacy and protected read-only inputs."""

import json
from pathlib import Path
from typing import BinaryIO

import htu.adapters.collectors.codex_audit_reader as audit_reader
import pytest
from htu.adapters.collectors.codex_audit_reader import read_audit_file
from htu.adapters.sqlite.connection import ReadPool
from htu.adapters.sqlite.pricing_repo import PricingRepository
from htu.adapters.sqlite.writer import UnitOfWork, WriteExecutor
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import parse_instant
from htu.domain.pricing.model import ModelRate
from htu.domain.pricing.rules import save_version_prices
from htu.tools.codex_audit import audit, main, markdown

from backend.tests.integration.scan_helpers import (
    AT,
    FIXTURES,
    headers,
    request,
    scanner,
    seed_prices,
    sessions,
)

END = parse_instant("2026-09-02T00:00:00Z")


def test_default_audit_finds_current_database_before_legacy_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from htu.tools.codex_audit import default_db

    monkeypatch.setenv("HTU_DATA_DIR", str(tmp_path))
    legacy, current = tmp_path / "ledger.sqlite3", tmp_path / "htu.sqlite3"
    legacy.touch()
    assert default_db() == legacy
    current.touch()
    assert default_db() == current


def test_fixture_counts_subagent_fork_and_duplicate_snapshots() -> None:
    report = audit(FIXTURES / "codex", AT, END, now=END)
    assert len(report.events) == 2 and report.duplicates == 1 and report.snapshots == 3
    assert report.files == 3 and report.conflicts == 0
    assert {key[0] for key in report.groups} == {"desktop", "desktop_subagent"}
    assert sum(group.tokens["input"] for group in report.groups.values()) == 200
    assert sum(group.tokens["cache_read"] for group in report.groups.values()) == 80
    assert sum(group.tokens["output"] for group in report.groups.values()) == 20
    output = markdown(report)
    assert "不可用" in output and "未验证来源与账本相等" in output


def test_conflicting_request_identity_is_reported_without_counting_twice(tmp_path: Path) -> None:
    root = sessions(tmp_path / "home")
    changed = json.loads(request(1, END))
    changed["timestamp"] = "2026-09-01T00:05:00.000Z"
    changed["payload"]["usage"]["input_tokens"] = 150
    (root / "one.jsonl").write_bytes(headers() + request(1, AT) + json.dumps(changed).encode() + b"\n")
    report = audit(root, AT, END, now=END)
    assert len(report.events) == 1 and report.conflicts == 1
    assert report.conflict_fields == {"tokens": 1, "occurred_at": 1}
    assert report.conflict_token_delta["input"] == 50
    assert report.conflict_locations == {"same_file": 1}
    output = markdown(report)
    assert "后见减首见，仅诊断，不追加计费" in output
    assert "response-1" not in output and "synthetic-desktop" not in output


def test_real_chain_matches_read_only_database_and_uses_current_prices(tmp_path: Path) -> None:
    root = sessions(tmp_path / "home")
    (root / "one.jsonl").write_bytes(headers() + request(1, AT))
    path = tmp_path / "ledger.sqlite3"
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        assert scanner(writer, pool, tmp_path / "home", FakeClock(AT)).run().billing.batch.billed == 1

        def revise(work: UnitOfWork) -> None:
            repo = PricingRepository(work.connection)
            old = repo.load()
            new = save_version_prices(
                old,
                "v",
                (ModelRate("gpt-6-astra", "AsTra", "2", "0.1", "2"),),
                at=END,
                revision_id="new-rate",
                expected_revision=old.revision,
            )
            repo.save(work, old, new)

        writer.execute(revise)
    before = path.read_bytes()
    report = audit(root, AT, END, db=path, now=END)
    assert report.ledger is not None and len(report.ledger.rows) == 1
    assert report.ledger.last_scan_at == AT
    assert report.groups[("desktop", "gpt-6-astra", "accepted")].pico == 144_000_000
    assert next(iter(report.ledger.rows.values()))[1] == 84_000_000
    output = markdown(report)
    assert "来源可计费请求 = 账本请求" in output and "0.000084000000 USD" in output
    assert path.read_bytes() == before


def test_mismatch_missing_extra_and_start_boundary_are_reported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = sessions(home)
    file = root / "one.jsonl"
    file.write_bytes(headers() + request(1, AT) + request(2, AT))
    path = tmp_path / "ledger.sqlite3"
    with WriteExecutor(path) as writer, ReadPool(path) as pool:
        writer.execute(seed_prices)
        scanner(writer, pool, home, FakeClock(AT)).run()
        writer.execute(
            lambda work: work.connection.execute("UPDATE ledger SET input=101 WHERE request_id='response-1'")
        )
    file.write_bytes(headers() + request(1, AT) + request(3, AT))
    output = markdown(audit(root, AT, END, db=path, now=END))
    assert "来源未入账 1" in output and "账本来源缺失 1" in output and "字段不一致 1" in output
    assert "来源可计费请求 = 账本请求" not in output


@pytest.mark.parametrize(
    "originator,source,reason",
    [
        ("Codex Desktop", "cli", "source_unverified"),
        ("codex_cli_rs", "vscode", "originator_unverified"),
        ("Codex Desktop", "future-desktop", "source_unverified"),
    ],
)
def test_rejected_request_counts_and_tokens(
    tmp_path: Path, originator: str, source: str, reason: str
) -> None:
    records = [json.loads(line) for line in (headers() + request(1, AT)).splitlines()]
    records[0]["payload"].update(originator=originator, source=source)
    (tmp_path / "rejected.jsonl").write_text(
        "\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8"
    )
    report = audit(tmp_path, AT, END, now=END)
    assert not report.events
    group = report.groups[("rejected", "gpt-6-astra", reason)]
    assert group.count == 1 and group.tokens["input"] == 100 and group.tokens["output"] == 10


def test_invalid_unknown_unscoped_interrupted_and_privacy(tmp_path: Path) -> None:
    bad = json.loads(request(1, AT))
    bad["payload"]["usage"]["input_tokens"] = -1
    unscoped = json.loads(request(2, AT))
    unscoped.pop("timestamp")
    private = {
        "type": "response_item",
        "timestamp": "2026-09-01T00:00:00Z",
        "payload": {"text": "PRIVATE_PROMPT_SENTINEL"},
    }
    aborted = {
        "type": "event_msg",
        "timestamp": "2026-09-01T00:00:00Z",
        "payload": {"type": "turn_aborted", "reason": "interrupted"},
    }
    tail = request(4, END)  # exclusive end
    data = headers() + b"not json\n" + request(3, AT)
    data += ("\n".join(json.dumps(row) for row in (bad, unscoped, private, aborted)) + "\n").encode()
    (tmp_path / "file.jsonl").write_bytes(data + tail + b'{"type":"token_usage_record"')
    report = audit(tmp_path, AT, END, now=END)
    assert len(report.events) == 1 and report.interrupted == 1
    assert report.diagnostics["invalid_json"] == 1
    assert report.groups[("desktop", "(unknown)", "accepted")].unpriced == 1
    assert sum(group.count for key, group in report.groups.items() if key[2] == "timestamp_unscoped") == 1
    assert sum(group.count for key, group in report.groups.items() if key[2] == "invalid_token_count") == 1
    assert "PRIVATE_PROMPT_SENTINEL" not in markdown(report)


def test_archive_dedup_and_line_limit(tmp_path: Path) -> None:
    root = sessions(tmp_path)
    archive = root.parent / "archived_sessions"
    archive.mkdir()
    data = headers() + request(1, AT)
    (root / "one.jsonl").write_bytes(data)
    (archive / "copy.jsonl").write_bytes(data)
    report = audit(root, AT, END, now=END)
    assert len(report.events) == 1 and report.duplicates == 1 and report.files == 2
    with pytest.raises(ValueError, match="source_line_too_large"):
        tuple(read_audit_file(str(root / "one.jsonl"), AT, END, line_limit=20))


def test_duplicate_conflict_is_reported_once(tmp_path: Path) -> None:
    data = headers() + request(1, AT)
    (tmp_path / "a.jsonl").write_bytes(data)
    (tmp_path / "b.jsonl").write_bytes(data.replace(b'"input_tokens":100', b'"input_tokens":101'))
    # request() uses compact separators, preserving the request identity.
    report = audit(tmp_path, AT, END, now=END)
    assert len(report.events) == 1 and report.duplicates == 1 and report.conflicts == 1


def test_changed_source_discards_entire_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "one.jsonl"
    file.write_bytes(headers() + request(1, AT))
    original_anchor = audit_reader.anchor
    calls = 0

    def rewrite_anchor(stream: BinaryIO, offset: int) -> str:
        nonlocal calls
        calls += 1
        # Use the real file mutation between the initial and final anchor checks.
        if calls == 2:
            file.write_bytes(b"rewritten\n")
        return original_anchor(stream, offset)

    monkeypatch.setattr(audit_reader, "anchor", rewrite_anchor)
    report = audit(tmp_path, AT, END, now=END)
    assert report.files == 0 and not report.events
    assert report.diagnostics["source_changed_during_read"] == 1


def test_missing_meta_has_rejection_reason_and_never_becomes_billable(tmp_path: Path) -> None:
    (tmp_path / "one.jsonl").write_bytes(request(1, AT))
    report = audit(tmp_path, AT, END, now=END)
    assert not report.events and report.groups[("rejected", "(unknown)", "missing_session_meta")].count == 1


def test_empty_object_is_skipped_without_clearing_model(tmp_path: Path) -> None:
    (tmp_path / "one.jsonl").write_bytes(headers() + b"{}\n" + request(1, AT))
    report = audit(tmp_path, AT, END, now=END)
    assert len(report.events) == 1 and report.diagnostics == {}
    assert report.groups[("desktop", "gpt-6-astra", "accepted")].count == 1


def test_cli_stdout_markdown_out_and_no_database_creation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTU_DATA_DIR", str(tmp_path / "missing-data"))
    args = [
        "--start",
        "2026-09-01T00:00:00Z",
        "--end",
        "2026-09-02T00:00:00Z",
        "--source",
        str(FIXTURES / "codex"),
    ]
    assert main(args) == 0 and "来源可计费请求 2" in capsys.readouterr().out
    output = tmp_path / "report.md"
    assert main([*args, "--out", str(output)]) == 0
    assert "desktop_subagent" in output.read_text(encoding="utf-8")
    assert not (tmp_path / "missing-data").exists()
    db = tmp_path / "missing.sqlite3"
    assert main([*args, "--db", str(db)]) == 1 and not db.exists()
    assert "审计失败" in capsys.readouterr().err


def test_cli_protects_sources_and_database_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTU_DATA_DIR", str(tmp_path / "none"))
    source = tmp_path / "one.jsonl"
    source.write_bytes(headers() + request(1, AT))
    before = source.read_bytes()
    args = ["--start", "2026-09-01T00:00:00Z", "--end", "2026-09-02T00:00:00Z", "--source", str(tmp_path)]
    assert main([*args, "--out", str(source)]) == 1 and source.read_bytes() == before
    db = tmp_path / "database.md"
    db.write_bytes(b"private database")
    assert main([*args, "--out", str(db), "--db", str(db)]) == 1
    assert db.read_bytes() == b"private database"


@pytest.mark.parametrize("input_kind", ["source", "db"])
def test_cli_protects_hardlinked_report_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_kind: str
) -> None:
    monkeypatch.setenv("HTU_DATA_DIR", str(tmp_path / "none"))
    input_path = tmp_path / ("one.jsonl" if input_kind == "source" else "ledger.sqlite3")
    data = headers() + request(1, AT) if input_kind == "source" else b"private database"
    input_path.write_bytes(data)
    output = tmp_path / "report.md"
    output.hardlink_to(input_path)
    args = [
        "--start",
        "2026-09-01T00:00:00Z",
        "--end",
        "2026-09-02T00:00:00Z",
        "--source",
        str(tmp_path),
        "--out",
        str(output),
    ]
    if input_kind == "db":
        args.extend(["--db", str(input_path)])
    assert main(args) == 1
    assert input_path.read_bytes() == data and output.read_bytes() == data
