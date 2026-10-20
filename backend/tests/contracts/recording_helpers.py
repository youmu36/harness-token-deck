"""Normalize only run-specific IDs/paths in genuine router recordings."""

import json
import os
import re
from pathlib import Path
from uuid import UUID

from htu.bootstrap import Application
from htu.ipc.dto.base import Failure, Success

UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


class Recorder:
    def __init__(self, app: Application, temporary: Path) -> None:
        self.app, self.temporary = app, temporary
        self.records: list[dict[str, object]] = []

    def call(self, command: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        parameters = payload or {}
        envelope = self.app.router.call(command, parameters)
        definition = self.app.router.commands[command]
        assert envelope["ok"], envelope
        response = Success[definition.response].model_validate(envelope)
        recorded = {**parameters, "credential": "[REDACTED]"} if parameters.get("credential") else parameters
        self.records.append({"command": command, "payload": recorded, "response": envelope})
        return response.data.model_dump(mode="json")

    def error(self, command: str, payload: dict[str, object], code: str) -> None:
        envelope = self.app.router.call(command, payload)
        assert Failure.model_validate(envelope).error.code == code
        self.records.append({"command": command, "payload": payload, "response": envelope})

    def save(self, destination: Path) -> None:
        assert {str(record["command"]) for record in self.records} == set(self.app.router.commands)
        serialized = json.dumps(self.records, ensure_ascii=False, indent=2, sort_keys=True)
        replacements: dict[str, str] = {}

        def replace(match: re.Match[str]) -> str:
            value = match[0]
            if value not in replacements:
                replacements[value] = str(UUID(int=len(replacements) + 1, version=4))
            return replacements[value]

        serialized = UUID_PATTERN.sub(replace, serialized)
        # JSON-escaped paths cover both raw Windows and normalized source paths.
        for temporary in {str(self.temporary), os.path.normcase(str(self.temporary))}:
            serialized = serialized.replace(json.dumps(temporary)[1:-1], "C:\\\\htu-contract-fixture")
        serialized = re.sub(r"[0-9a-f]{64}", "0" * 64, serialized)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(serialized + "\n", encoding="utf-8")
