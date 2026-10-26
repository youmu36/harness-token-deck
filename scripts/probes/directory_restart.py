"""Exercise the real native directory move/restart using only an isolated synthetic dataset."""

import json
import os
import subprocess
import sys
import time
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psutil
from htd.adapters.collectors.discovery import Discovery
from htd.adapters.data_directory import resolve_data_directory
from htd.adapters.sqlite.connection import open_connection
from htd.adapters.sqlite.ingest_repo import IngestRepository
from htd.bootstrap import Application
from htd.domain.common.time import Instant

from backend.tests.integration.collection_helpers import usage
from scripts.release_support import isolated_environment

ROOT = Path(__file__).resolve().parents[2]

# Instrument only the test namespace and native polling. The entry point, move,
# after-success callback, shutdown and subprocess restart remain production code.
HOOK = r"""
import json, os, threading, traceback
from pathlib import Path
from uuid import uuid4
import htd.adapters.desktop.single_instance as instances
from htd.adapters.desktop.native import NativeWindow

root = Path(os.environ['HTD_RESTART_PROBE_ROOT']).resolve()
target = root / '新目录 空格'
original_instance = instances.SingleInstance
def isolated(directory, **kwargs):
    assert directory.resolve().is_relative_to(root)
    name = 'HTD.DirectoryRestart.' + root.name
    return original_instance(directory, name=name, pipe_name=name)
instances.SingleInstance = isolated
original_poll = NativeWindow.poll
started = False
def poll(native):
    global started
    original_poll(native)
    controller = native.host_action.__self__
    if started or not controller._frontend_ready:
        return
    started = True
    with (root / 'launches.jsonl').open('a', encoding='utf-8') as stream:
        row = {'pid':os.getpid(),'directory':str(controller.directory)}
        stream.write(json.dumps(row, ensure_ascii=False)+'\n')
    def work():
        try:
            if controller.directory == target:
                result = controller.bridge.call('dataDirectory.status', {})
                assert result['ok'], result
                row = {'pid':os.getpid(), 'status':result['data']}
                (root / 'restarted.json').write_text(json.dumps(row, ensure_ascii=False), encoding='utf-8')
                controller.instance._queue({'cmd':'quit'})
            else:
                preview = controller.bridge.call('dataDirectory.preview', {'target':str(target)})
                assert preview['ok'], preview
                payload = {'target':str(target), 'fingerprint':preview['data']['fingerprint'],
                           'operationId':str(uuid4())}
                result = controller.bridge.call('dataDirectory.move', payload)
                assert result['ok'], result
        except BaseException:
            (root / 'failure.txt').write_text(traceback.format_exc(), encoding='utf-8')
            controller.instance._queue({'cmd':'quit'})
    threading.Thread(target=work, daemon=True).start()
NativeWindow.poll = poll
"""


def main() -> int:
    root = ROOT / ".verify/directory-restart" / uuid4().hex
    root.mkdir(parents=True)
    environment = isolated_environment(root)
    environment.pop("HTD_DATA_DIR", None)
    environment.pop("HTD_SMOKE_ISOLATED", None)
    environment["HTD_RESTART_PROBE_ROOT"] = str(root)
    location = resolve_data_directory(environ=environment)
    with Application(
        data_location=location,
        discovery=Discovery(home=Path(environment["USERPROFILE"]), environ=environment),
        start_scheduler=False,
    ) as app:
        source = next(item for item in app.store.sources().sources if item.harness.value == "codex")
        now = Instant(datetime.now(UTC))
        app.writer.execute(
            lambda work: IngestRepository(work.connection).commit_batch(
                work,
                source.harness,
                source.path,
                source.path + "/synthetic.jsonl",
                {"offset": 1},
                (usage(1, at=now), usage(2, at=now)),
                at=now,
            )
        )
        initial = [
            tuple(row)
            for row in app.readers.get().execute("SELECT * FROM identity ORDER BY harness,dedupe_key")
        ]
        totals = [
            tuple(row)
            for row in app.readers.get().execute("SELECT * FROM session_total ORDER BY harness,session_ref")
        ]
    hooks = root / "hooks"
    hooks.mkdir()
    compile(HOOK, "sitecustomize.py", "exec")
    (hooks / "sitecustomize.py").write_text(HOOK, encoding="utf-8")
    environment["PYTHONPATH"] = os.pathsep.join([str(hooks), str(ROOT / "backend/src"), str(ROOT)])
    report: dict[str, object] = {
        "directory": str(root),
        "build": "source Python + production Vite + native WebView2",
        "passed": False,
    }
    with (root / "stdout.log").open("w") as output, (root / "stderr.log").open("w") as errors:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sitecustomize; from htd.__main__ import main; raise SystemExit(main([]))",
            ],
            cwd=ROOT,
            env=environment,
            stdout=output,
            stderr=errors,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if (root / "failure.txt").exists():
                    raise AssertionError((root / "failure.txt").read_text(encoding="utf-8"))
                if (root / "restarted.json").exists():
                    break
                time.sleep(0.1)
            assert (root / "restarted.json").exists(), "Native automatic restart timed out"
            assert process.wait(timeout=15) == 0
            restarted = json.loads((root / "restarted.json").read_text(encoding="utf-8"))
            with suppress(psutil.NoSuchProcess):
                psutil.Process(restarted["pid"]).wait(timeout=15)
            target = root / "新目录 空格"
            assert restarted["status"]["root"] == str(target), restarted
            resolved = resolve_data_directory(environ=environment)
            assert resolved.root == target and resolved.phase == "active"
            with closing(open_connection(target / "htd.sqlite3")) as db:
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert not db.execute("PRAGMA foreign_key_check").fetchall()
                assert [
                    tuple(row) for row in db.execute("SELECT * FROM identity ORDER BY harness,dedupe_key")
                ] == initial
                assert [
                    tuple(row)
                    for row in db.execute("SELECT * FROM session_total ORDER BY harness,session_ref")
                ] == totals
                assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2
                assert db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0
            report.update(
                passed=True,
                acceptedRequests=2,
                unvaluedRequests=2,
                automaticRestart=True,
                oldCopyRetained=(location.root / "htd.sqlite3").is_file(),
                launches=(root / "launches.jsonl").read_text(encoding="utf-8"),
            )
        finally:
            if process.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False
                )
            (ROOT / ".verify/directory-restart.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
