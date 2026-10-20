"""Reconstruct bounded active minutes from delayed per-session output reports."""

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import cast

from htu.domain.common.time import Instant, format_instant, parse_instant

SCALE = 1_000_000
MINUTE = 60_000
HOUR = 60 * MINUTE
QUIET = 10 * MINUTE
RECENT_MINUTES = 3


@dataclass(slots=True)
class OutputWindow:
    # Minute values: micro-tokens, active milliseconds, source-timed milliseconds.
    minutes: dict[str, list[int]] = field(default_factory=dict[str, list[int]])
    # Session hash -> [last completion, last receipt, inferred span start, inferred flag].
    cursors: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    latest: str | None = None

    @classmethod
    def load(cls, value: object) -> "OutputWindow":
        if not isinstance(value, dict):
            return cls()
        raw = cast(dict[str, object], value)
        if raw.get("v") != 1:
            return cls()
        return cls(
            {
                key: list(map(int, values))
                for key, values in cast(dict[str, list[str]], raw["minutes"]).items()
            },
            cast(dict[str, list[str]], raw["cursors"]),
            cast(str | None, raw["latest"]),
        )

    def dump(self) -> dict[str, object]:
        return {
            "v": 1,
            "minutes": {key: list(map(str, values)) for key, values in self.minutes.items()},
            "cursors": self.cursors,
            "latest": self.latest,
        }

    def add(
        self, stamp: str, count: int | None, duration: int | None, session: str | None, received: str
    ) -> None:
        end = parse_instant(stamp).value
        key = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16] if session else "unknown"
        previous = self.cursors.get(key)
        prior_end = parse_instant(previous[0]).value if previous else None
        source_timed = duration is not None and duration > 0
        repeat_span = bool(not source_timed and previous and previous[0] == stamp and previous[3] == "1")
        if source_timed:
            assert duration is not None
            start = end - timedelta(milliseconds=duration)
        elif repeat_span:
            assert previous is not None
            start = parse_instant(previous[2]).value
            duration = (end - start) // timedelta(milliseconds=1)
        elif (
            session
            and previous
            and prior_end is not None
            and prior_end < end
            and (end - prior_end) // timedelta(milliseconds=1) < QUIET
            and parse_instant(received).value - parse_instant(previous[1]).value < timedelta(minutes=10)
        ):
            start = prior_end
            duration = (end - start) // timedelta(milliseconds=1)
        else:
            # With no same-session clock, one completed positive minute is the
            # only supported coarse sample. Never borrow another session's gap.
            start = end - timedelta(milliseconds=MINUTE)
            duration = MINUTE
        if prior_end is None or end >= prior_end:
            self.cursors[key] = [
                stamp,
                received,
                format_instant(Instant(start)),
                "1" if repeat_span or (not source_timed and count is not None and count > 0) else "0",
            ]
        if count is not None and count > 0:
            self.latest = max(self.latest or stamp, stamp)
            assert duration is not None and duration > 0
            cursor = max(start, end - timedelta(milliseconds=HOUR))
            allocated = count * SCALE * ((cursor - start) // timedelta(milliseconds=1)) // duration
            while cursor < end:
                minute = cursor.replace(second=0, microsecond=0)
                stop = min(end, minute + timedelta(minutes=1))
                accumulated = count * SCALE * ((stop - start) // timedelta(milliseconds=1)) // duration
                milliseconds = (stop - cursor) // timedelta(milliseconds=1)
                values = self.minutes.setdefault(format_instant(Instant(minute)), [0, 0, 0])
                values[0] += accumulated - allocated
                # Repeated coarse-timestamp members share one inferred span,
                # even when a billing batch is split or the app restarts.
                if not repeat_span:
                    values[1] += milliseconds
                    values[2] += milliseconds if source_timed else 0
                allocated, cursor = accumulated, stop
        if self.latest:
            edge = format_instant(Instant(parse_instant(self.latest).value - timedelta(hours=1)))
            self.minutes = {key: values for key, values in self.minutes.items() if key >= edge}
        if len(self.cursors) > 100:
            self.cursors = dict(sorted(self.cursors.items(), key=lambda pair: pair[1][1])[-100:])

    def totals(self, recent: bool = False) -> tuple[int, int, bool]:
        keys = sorted(key for key, values in self.minutes.items() if values[0] > 0 and values[1] > 0)
        if recent:
            keys = keys[-RECENT_MINUTES:]
        tokens = sum(self.minutes[key][0] for key in keys)
        milliseconds = sum(self.minutes[key][1] for key in keys)
        source_ms = sum(self.minutes[key][2] for key in keys)
        return tokens, milliseconds, bool(milliseconds and milliseconds == source_ms)
