"""Export the live registry in isolation and generate/check the frontend contracts."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast

from htd.adapters.collectors.discovery import Discovery
from htd.bootstrap import Application
from htd.ipc.dto.base import Failure, StrictModel, Success
from htd.ipc.dto.events import (
    CollectionTickEvent,
    RevisionChangedEvent,
    UsageChangedEvent,
    VisibilityEvent,
    WalletChangedEvent,
)
from htd.ipc.dto.tasks import TaskDoneEvent, TaskProgressEvent
from htd.ipc.router import Router
from pydantic import JsonValue
from pydantic.json_schema import models_json_schema

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "frontend/src/infra/generated"
FILES = ("contracts.schema.json", "contracts.d.ts", "validators.js", "validators.d.ts", "commands.ts")
EVENTS: dict[str, type[StrictModel]] = {
    "usage.changed": UsageChangedEvent,
    "wallet.changed": WalletChangedEvent,
    "pricing.changed": RevisionChangedEvent,
    "quality.changed": RevisionChangedEvent,
    "models.changed": RevisionChangedEvent,
    "output.changed": RevisionChangedEvent,
    "preferences.changed": RevisionChangedEvent,
    "sources.changed": RevisionChangedEvent,
    "collection.tick": CollectionTickEvent,
    "task.progress": TaskProgressEvent,
    "task.done": TaskDoneEvent,
    "app.visibility": VisibilityEvent,
}


def contract_schema(router: Router) -> dict[str, JsonValue]:
    commands = sorted(router.commands.values(), key=lambda command: command.name)
    models: list[tuple[type[StrictModel], Literal["validation", "serialization"]]] = []
    for command in commands:
        models.extend([(command.request, "validation"), (Success[command.response], "serialization")])
    models.extend([(Failure, "serialization"), *((dto, "serialization") for dto in EVENTS.values())])
    references, definitions = models_json_schema(models)

    def ref(dto: type[StrictModel], mode: Literal["validation", "serialization"]) -> object:
        return references[dto, mode]

    failure = ref(Failure, "serialization")
    sections = {
        "requests": {command.name: ref(command.request, "validation") for command in commands},
        "responses": {
            command.name: {"oneOf": [ref(Success[command.response], "serialization"), failure]}
            for command in commands
        },
        "events": {name: ref(dto, "serialization") for name, dto in sorted(EVENTS.items())},
    }
    schema: dict[str, object] = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://harness-token-deck.local/ipc",
        "title": "Contracts",
        **definitions,
        "type": "object",
        "additionalProperties": False,
        "required": list(sections),
        "properties": {
            name: {
                "type": "object",
                "additionalProperties": False,
                "required": list(properties),
                "properties": properties,
            }
            for name, properties in sections.items()
        },
    }

    # JSON Schema's oneOf already encodes the discriminator. Ajv does not support
    # Pydantic's boolean/multi-value discriminator mapping extension.
    def normalize(node: object) -> None:
        if isinstance(node, dict):
            value = cast(dict[str, object], node)
            value.pop("discriminator", None)
            if "const" in value:
                value["enum"] = [value.pop("const")]
            title = value.get("title", "")
            if isinstance(title, str) and (title.startswith("Success[") or title == "Failure"):
                value["required"] = ["ok", "data" if title.startswith("Success[") else "error"]
            for child in value.values():
                normalize(child)
        elif isinstance(node, list):
            for child in cast(list[object], node):
                normalize(child)

    normalize(schema)
    return cast(dict[str, JsonValue], schema)


def export_schema() -> dict[str, JsonValue]:
    with TemporaryDirectory(prefix="htd-contract-registry-") as temporary:
        root = Path(temporary)
        discovery = Discovery(
            home=root,
            environ={
                f"HTD_SOURCE_ROOT_{name}": str(root / name) for name in ("CODEX", "ZCODE", "DSH", "CLAUDE")
            },
        )
        with Application(root / "data", discovery=discovery, start_scheduler=False) as app:
            return contract_schema(app.router)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Compare without modifying saved artifacts")
    parser.add_argument("--check-registry", action="store_true", help="Check registered command coverage")
    args = parser.parse_args()
    schema = export_schema()
    if args.check_registry:
        saved = json.loads((DESTINATION / FILES[0]).read_text(encoding="utf-8"))
        if saved != schema:
            print("Registry/DTO schema differs; run uv run python scripts/gen_contracts.py")
            return 1
        print("Registry/DTOs match the saved command and event schema")
        return 0
    with TemporaryDirectory(prefix="htd-contract-generation-") as temporary:
        destination = Path(temporary) if args.check else DESTINATION
        destination.mkdir(parents=True, exist_ok=True)
        (destination / FILES[0]).write_text(
            json.dumps(schema, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or "npm"
        subprocess.run([npm, "run", "gen:contracts", "--", str(destination)], cwd=ROOT, check=True)
        if args.check:
            changed = [
                name
                for name in FILES
                if not (DESTINATION / name).is_file()
                or (DESTINATION / name).read_bytes() != (destination / name).read_bytes()
            ]
            if changed:
                print(f"Stale generated contracts: {', '.join(changed)}")
                return 1
        print(
            "Contracts: generated artifacts match" if args.check else f"Contracts: written to {destination}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
