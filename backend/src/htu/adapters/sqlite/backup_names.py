"""Reserve short, readable backup names without replacing an existing file."""

from datetime import datetime
from itertools import count
from pathlib import Path


def reserve_backup(directory: Path, at: datetime) -> Path:
    stem = f"htu-{at.astimezone().strftime('%Y%m%d-%H%M%S')}"
    for index in count(1):
        path = directory / f"{stem}{'' if index == 1 else f'-{index}'}.sqlite3"
        try:
            path.touch(exist_ok=False)
        except FileExistsError:
            continue
        return path
    raise AssertionError("Backup name sequence exhausted")
