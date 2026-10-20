"""Real-process instance tests; no windows, real sources or user data are opened."""

import argparse
import faulthandler
import json
import os
import sys
from pathlib import Path
from typing import Any

import htu.__main__ as entry
from htu.adapters.desktop.single_instance import SingleInstance


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("owner", "launch", "desktop"))
    parser.add_argument("name")
    parser.add_argument("--silent", action="store_true")
    args = parser.parse_args()
    directory = Path(os.environ["HTU_DATA_DIR"])

    def instance(root: Path) -> SingleInstance:
        return SingleInstance(root, name=args.name, pipe_name=args.name)

    if args.mode == "owner":
        with instance(directory) as owned:
            assert owned.primary
            if not args.silent:
                owned.listen()
            sys.stdout.write(json.dumps({"ready": True, "sid": owned.sid}) + "\n")
            sys.stdout.flush()
            # EOF is a bounded parent-controlled shutdown signal, not a product hook.
            sys.stdin.readline()
        return 0

    def forbidden_bootstrap(*arguments: object, **keywords: object) -> None:
        (directory / "unexpected-bootstrap").write_text("called", encoding="utf-8")
        raise AssertionError("Second instance attempted bootstrap")

    def notify(message: str) -> None:
        sys.stdout.write(json.dumps({"dialog": message}) + "\n")

    patched: Any = entry
    patched.SingleInstance = instance
    if args.mode == "desktop":
        faulthandler.dump_traceback_later(8, file=sys.stderr)
        try:
            return entry.main([])
        finally:
            faulthandler.cancel_dump_traceback_later()
    patched.Application = forbidden_bootstrap
    patched.show_error = notify
    return entry.main([])


if __name__ == "__main__":
    raise SystemExit(main())
