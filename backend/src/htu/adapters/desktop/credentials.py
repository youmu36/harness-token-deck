"""Per-launch credentials; the discoverable pointer and random key both have private ACLs."""

import json
import os
import re
import secrets
from pathlib import Path
from typing import cast
from uuid import uuid4

from htu.adapters.desktop._win32 import write_private_file

_KEY_NAME = re.compile(r"instance-[0-9a-f]{32}\.key\Z")


def key_path(directory: Path, user_hash: str) -> Path:
    pointer = directory / f".instance-{user_hash}.json"
    metadata = json.loads(pointer.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("无效的单实例凭据索引")
    fields = cast(dict[str, object], metadata)
    if set(fields) != {"keyFile"}:
        raise ValueError("无效的单实例凭据索引")
    name = fields["keyFile"]
    if not isinstance(name, str) or not _KEY_NAME.fullmatch(name):
        raise ValueError("无效的单实例凭据文件名")
    return directory / name


def read_key(directory: Path, user_hash: str) -> bytes:
    path = key_path(directory, user_hash)
    if path.stat().st_size != 32:
        raise ValueError("无效的单实例凭据长度")
    data = path.read_bytes()
    if len(data) != 32:
        raise ValueError("无效的单实例凭据长度")
    return data


class Credentials:
    def __init__(self, directory: Path, user_hash: str, sid: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory, self.user_hash, self.sid = directory, user_hash, sid
        self.key = secrets.token_bytes(32)
        self.path = directory / f"instance-{uuid4().hex}.key"
        self.pointer = directory / f".instance-{user_hash}.json"
        write_private_file(self.path, self.key, sid)

    def publish(self) -> None:
        try:
            previous = key_path(self.directory, self.user_hash)
        except (OSError, ValueError):
            previous = None
        temporary = self.directory / f".instance-{uuid4().hex}.tmp"
        try:
            write_private_file(temporary, json.dumps({"keyFile": self.path.name}).encode("utf-8"), self.sid)
            os.replace(temporary, self.pointer)
        finally:
            temporary.unlink(missing_ok=True)
        if previous is not None and previous != self.path:
            previous.unlink(missing_ok=True)

    def close(self) -> None:
        try:
            if key_path(self.directory, self.user_hash) == self.path:
                self.pointer.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        self.path.unlink(missing_ok=True)
