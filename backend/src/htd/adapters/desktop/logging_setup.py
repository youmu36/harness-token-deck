"""Scoped desktop file logging with the specified 5MB/five-backup rotation."""

import logging
from collections.abc import Generator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path


@contextmanager
def logging_session(directory: Path, *, debug: bool = False) -> Generator[Path]:
    path = directory / "logs/htd.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8", delay=True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.addHandler(handler)
    try:
        yield path
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous)
