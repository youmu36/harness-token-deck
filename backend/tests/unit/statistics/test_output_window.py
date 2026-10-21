"""Late reports carry their elapsed time rather than becoming zero-minute polls."""

from datetime import timedelta

from htd.domain.common.time import Instant, format_instant, parse_instant
from htd.domain.statistics.output_window import SCALE, OutputWindow

START = parse_instant("2026-10-20T00:10:00.000Z")


def stamp(seconds: int) -> str:
    return format_instant(Instant(START.value + timedelta(seconds=seconds)))


def test_untimed_report_does_not_fill_intervening_empty_minutes() -> None:
    window = OutputWindow()
    window.add(stamp(0), 600, 10_000, "session", stamp(0))
    window.add(stamp(180), 5400, None, "session", stamp(180))
    assert stamp(0) not in window.minutes and stamp(60) not in window.minutes
    assert window.minutes[stamp(120)] == [5400 * SCALE, 60_000, 0]
    assert window.totals(recent=True) == (6000 * SCALE, 70_000, False)


def test_no_polling_sample_is_added_before_the_delayed_report_arrives() -> None:
    window = OutputWindow()
    window.add(stamp(0), 600, 10_000, "session", stamp(0))
    original = window.totals(recent=True)
    assert OutputWindow.load(window.dump()).totals(recent=True) == original
    # A genuine zero-output request is not a positive speed sample either.
    window.add(stamp(60), 0, None, "session", stamp(60))
    assert window.totals(recent=True) == original


def test_buffered_millisecond_burst_keeps_prior_speed_and_breaks_the_inferred_clock() -> None:
    window = OutputWindow()
    window.add(stamp(0), 600, 10_000, "session", stamp(0))
    prior = window.totals()
    window.add(stamp(1), 93, 3, "session", stamp(1))
    assert window.totals() == prior and not window.cursors
    window.add(stamp(2), 600, None, "session", stamp(2))
    assert window.totals() == (1200 * SCALE, 70_000, False)


def test_a_different_session_does_not_borrow_the_previous_sessions_silence() -> None:
    window = OutputWindow()
    window.add(stamp(0), 600, 10_000, "one", stamp(0))
    window.add(stamp(240), 600, None, "two", stamp(240))
    assert window.minutes[stamp(180)] == [600 * SCALE, 60_000, 0]
    assert stamp(60) not in window.minutes and stamp(120) not in window.minutes


def test_distinct_requests_with_one_coarse_timestamp_keep_their_own_duration() -> None:
    window = OutputWindow()
    for _ in range(250):
        window.add(stamp(0), 60, None, "session", stamp(0))
    window = OutputWindow.load(window.dump())
    for _ in range(250):
        window.add(stamp(0), 60, None, "session", stamp(1))
    assert window.totals() == (30_000 * SCALE, 30_000_000, False)


def test_zero_output_breaks_inferred_clock_without_lowering_the_last_speed() -> None:
    window = OutputWindow()
    window.add(stamp(0), 600, None, "session", stamp(0))
    original = window.totals()
    for seconds in (60, 120, 180, 240, 300):
        window.add(stamp(seconds), 0, None, "session", stamp(seconds))
        assert window.totals() == original
    window.add(stamp(301), 600, None, "session", stamp(301))
    assert window.totals() == (1200 * SCALE, 120_000, False)


def test_continuous_timed_minutes_preserve_the_source_rate_with_parallel_sessions() -> None:
    window = OutputWindow()
    for minute in range(8):
        for session in ("one", "two"):
            window.add(stamp(minute * 60), 27000, 60_000, session, stamp(minute * 60))
        tokens, milliseconds, timed = window.totals(recent=True)
        assert tokens * 1000 == 450 * milliseconds * SCALE and timed


def test_source_timing_excludes_the_gap_even_when_usage_was_delayed() -> None:
    window = OutputWindow()
    window.add(stamp(0), 600, 10_000, "session", stamp(0))
    window.add(stamp(240), 1200, 20_000, "session", stamp(240))
    assert stamp(60) not in window.minutes and stamp(120) not in window.minutes
    assert window.minutes[stamp(180)] == [1200 * SCALE, 20_000, 20_000]


def test_silence_past_freezing_does_not_become_a_long_generation_interval() -> None:
    window = OutputWindow()
    window.add(stamp(0), 600, None, "session", stamp(0))
    window.add(stamp(600), 600, None, "session", stamp(600))
    assert len(window.minutes) == 2
    assert window.minutes[stamp(540)][1] == 60_000


def test_late_records_do_not_move_the_session_clock_backwards() -> None:
    window = OutputWindow()
    window.add(stamp(120), 600, None, "session", stamp(120))
    window.add(stamp(60), 60, None, "session", stamp(121))
    window.add(stamp(180), 600, None, "session", stamp(180))
    assert window.minutes[stamp(120)] == [600 * SCALE, 60_000, 0]


def test_tokens_are_conserved_across_partial_minutes_and_state_is_bounded() -> None:
    window = OutputWindow()
    window.add(stamp(100), 7, 80_000, "session", stamp(100))
    assert window.totals() == (7 * SCALE, 80_000, True)
    for second in range(150):
        window.add(stamp(second), 1, 1000, str(second), stamp(second))
    assert len(window.cursors) == 100
    window.add(stamp(7200), 3600, 7_200_000, "long", stamp(7200))
    assert len(window.minutes) <= 3
    assert window.totals() == (90 * SCALE, 180_000, True)
