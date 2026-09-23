"""Versioned provisional closure rules shared by the offline strategy replay.

These are research choices, not broker execution rules: feature windows count
actual open bars, zones and touch episodes persist across scheduled closures,
confirmation expiry counts elapsed time, and closure-time entries are cancelled.
The finite calendar is evidence, never permission to invent missing open bars.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from .session_calendar import SessionCalendarArtifact, SessionCalendarError


SESSION_STRATEGY_SEMANTICS = "xauusd-session-strategy-v1"


def session_semantics() -> dict:
    """The deliberately small, fixed and unapproved closure interpretation."""
    return {
        "version": SESSION_STRATEGY_SEMANTICS,
        "status": "provisional_unapproved",
        "closures": "explicit_whole_utc_hours_only",
        "calendar_knowledge": "supplied_schedule_assumed_known_not_verified_point_in_time",
        "history_windows": "actual_open_bars_no_seed_reset",
        "zones": "preserved_until_observed_h1_close_invalidation",
        "touch_episodes": "preserved_until_next_observed_price_exit_or_invalidation",
        "confirmation_expiry": "elapsed_m15_intervals_including_closures",
        "entry_at_closure": "cancel_do_not_queue_for_reopening",
        "entry_beyond_calendar": "cancel_unverified_next_open",
        "execution": None,
    }


def validate_calendar(calendar: Optional[SessionCalendarArtifact]) -> None:
    if calendar is None:
        return
    if not isinstance(calendar, SessionCalendarArtifact):
        raise TypeError("calendar must be a SessionCalendarArtifact")
    calendar.require_hour_aligned_closures()


def _endpoint(calendar: SessionCalendarArtifact, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo != timezone.utc:
        raise SessionCalendarError("session endpoint must be normalized to UTC")
    if not calendar.coverage_start <= value <= calendar.coverage_end:
        raise SessionCalendarError("session endpoint is outside calendar coverage")


def open_bar_count(
    calendar: SessionCalendarArtifact,
    start: datetime,
    end: datetime,
    duration: timedelta,
) -> int:
    """Count scheduled open M15/H1 slots, not the presence of source candles.

    Callers must separately validate their actual bar stream. Endpoints may
    bound a closure, but both must align to the requested UTC timeframe.
    """
    validate_calendar(calendar)
    if calendar is None:
        raise TypeError("open_bar_count requires a calendar")
    if duration not in (timedelta(minutes=15), timedelta(hours=1)):
        raise ValueError("duration must be M15 or H1")
    _endpoint(calendar, start)
    _endpoint(calendar, end)
    if end < start:
        raise SessionCalendarError("open bar interval must not run backwards")
    minutes = int(duration.total_seconds() // 60)
    for value in (start, end):
        if value.minute % minutes or value.second or value.microsecond:
            raise SessionCalendarError("open bar interval must align to its timeframe")
    elapsed = end - start
    for closure in calendar.closures:
        overlap_start = max(start, closure.start)
        overlap_end = min(end, closure.end)
        if overlap_end > overlap_start:
            elapsed -= overlap_end - overlap_start
    count, remainder = divmod(elapsed, duration)
    if remainder:
        raise SessionCalendarError("open interval contains a partial bar")
    return count


def next_open_start(calendar: SessionCalendarArtifact, timestamp: datetime) -> datetime:
    """Advance an aligned slot through an explicit closure, never past coverage.

    ``coverage_end`` is an exhausted-calendar sentinel, not a tradable open.
    Consumers proposing entries must reject it and verify a full bar is covered.
    """
    validate_calendar(calendar)
    if calendar is None:
        raise TypeError("next_open_start requires a calendar")
    _endpoint(calendar, timestamp)
    if timestamp.minute % 15 or timestamp.second or timestamp.microsecond:
        raise SessionCalendarError("next open must align to an M15 boundary")
    for closure in calendar.closures:
        if closure.start <= timestamp < closure.end:
            return closure.end
    return timestamp
