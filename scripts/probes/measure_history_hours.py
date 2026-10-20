"""One bounded isolated comparison of coarse history, rebuilding, and model block decoding."""

import json
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from htu.adapters.sqlite import usage_archive
from htu.adapters.sqlite.statistics_store import SqliteStatisticsSnapshot
from htu.adapters.sqlite.usage_hours import refresh_hours
from htu.bootstrap import Application
from htu.domain.common.testing import FakeClock
from htu.domain.common.time import parse_instant
from htu.domain.statistics import Window

from scripts.measure_feedback_queries import END, START, measurements, seed

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    report = {"fixture": {"details": 12000, "minutes": 45000, "archivedMinutes": 45000}, "queries": []}
    with tempfile.TemporaryDirectory(prefix="htu-hours-") as temporary:  # noqa: SIM117
        with Application(Path(temporary), clock=FakeClock(END), start_scheduler=False) as app:
            # Recreate the preceding mixed-model archive layout.
            with patch.object(usage_archive, "_insert", usage_archive._insert_model):
                seed(app)
            app.writer.execute(lambda w: w.connection.execute("UPDATE usage_archive SET hours_json=NULL"))
            windows = [
                ("30-days", Window(START, END, 480, "day"), None),
                (
                    "model-hour",
                    Window(
                        parse_instant("2026-09-10T00:00:00Z"),
                        parse_instant("2026-09-11T00:00:00Z"),
                        480,
                        "hour",
                    ),
                    "model-3",
                ),
                (
                    "model-minute",
                    Window(
                        parse_instant("2026-09-10T00:00:00Z"),
                        parse_instant("2026-09-11T00:00:00Z"),
                        480,
                        "minute",
                    ),
                    "model-3",
                ),
                ("fractional-offset", Window(START, END, 345, "day"), None),
            ]
            expected = {}
            raw = SqliteStatisticsSnapshot.rows
            for phase in ("before", "after"):
                for name, window, model in windows:
                    app.statistics.invalidate()

                    def baseline(self, *args, **kwargs):
                        return raw(self, *args, **(kwargs | {"coarse": False}))

                    with patch.object(
                        SqliteStatisticsSnapshot, "rows", baseline if phase == "before" else raw
                    ):
                        stack, counters = measurements(app)
                        with stack:
                            began = perf_counter()
                            result = app.statistics.summary("all", window, model_key=model)[0]
                            elapsed = (perf_counter() - began) * 1000
                    if phase == "before":
                        expected[name] = result
                    else:
                        assert result == expected[name], name
                    report["queries"].append(
                        {"phase": phase, "name": name, "ms": round(elapsed, 3), **dict(counters)}
                    )
                if phase == "before":
                    began = perf_counter()

                    def rebuild(work):
                        while refresh_hours(work.connection):
                            pass
                        while usage_archive.optimize_archives(work.connection, limit=10):
                            pass

                    app.writer.execute(rebuild)
                    report["rebuildMs"] = round((perf_counter() - began) * 1000, 3)
                    # Compare one late-minute write with and without projection rebuilding.
                    began = perf_counter()
                    app.writer.execute(
                        lambda w: w.connection.execute(
                            "UPDATE usage_rollup SET pico=pico "
                            "WHERE bucket=(SELECT MIN(bucket) FROM usage_rollup)"
                        )
                    )
                    report["lateWriteMs"] = round((perf_counter() - began) * 1000, 3)
                    began = perf_counter()
                    app.writer.execute(lambda w: refresh_hours(w.connection))
                    report["lateHourRebuildMs"] = round((perf_counter() - began) * 1000, 3)
            report["exactResultsEqual"] = True
    destination = ROOT / ".verify/history-hours.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
