"""Private desktop geometry in the preference table, outside the user preference revision."""

import logging
from dataclasses import asdict, dataclass
from typing import cast

from htd.adapters.sqlite.connection import ReadPool
from htd.adapters.sqlite.usage_codec import dumps, loads
from htd.adapters.sqlite.writer import UnitOfWork, WriteExecutor

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class WindowGeometry:
    x: int
    y: int
    width: int
    height: int


class WindowStateStore:
    def __init__(self, writer: WriteExecutor, readers: ReadPool) -> None:
        self.writer, self.readers = writer, readers

    def load(self) -> WindowGeometry | None:
        with self.readers.connection() as db:
            row = db.execute("SELECT value_json FROM preference WHERE key='desktopWindow'").fetchone()
        if row is None:
            return None
        try:
            value = loads(row[0])
        except ValueError:
            _LOG.warning("窗口几何偏好损坏，使用默认尺寸")
            return None
        if not isinstance(value, dict):
            return None
        fields = cast(dict[str, object], value)
        if set(fields) != {"x", "y", "width", "height"} or any(
            type(item) is not int for item in fields.values()
        ):
            return None
        numbers = cast(dict[str, int], fields)
        return WindowGeometry(numbers["x"], numbers["y"], numbers["width"], numbers["height"])

    def save(self, geometry: WindowGeometry) -> None:
        def save(work: UnitOfWork) -> None:
            work.connection.execute(
                "INSERT INTO preference VALUES ('desktopWindow',?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (dumps(asdict(geometry)),),
            )

        self.writer.execute(save)
