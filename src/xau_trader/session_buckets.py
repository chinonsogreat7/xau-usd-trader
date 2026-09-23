"""Versioned import-only M15 observations against exact finite session evidence.

These are not ``QuoteBar`` values or strategy inputs.  Scheduled closure time is
kept separately from missing observations; every original complete bid/ask tick
is retained.  Calendar boundaries are never rounded to a candle boundary.
"""

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .domain import AvailabilityBasis
from .mt5_ticks import BidAskTick, Mt5TickError, ParsedMt5Ticks
from .session_calendar import (
    ScheduledClosure, SessionCalendarArtifact, parse_session_calendar_bytes,
)


SCHEMA_NAME = "xau_trader.session_bucket_observations"
SCHEMA_VERSION = 1
MAX_SESSION_BUCKETS = 100000
BUCKET_SECONDS = 900
_BUCKET = timedelta(seconds=BUCKET_SECONDS)
_BUCKET_MICROSECONDS = BUCKET_SECONDS * 1000000


class SessionBucketError(Mt5TickError):
    """Invalid evidence or an inconsistent import-only aggregation."""


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SessionBucketError("{} must be an explicit UTC datetime".format(field))
    return value.astimezone(timezone.utc)


def _micros(value: timedelta) -> int:
    # total_seconds() rounds floating point fractions in long intervals.
    return (value.days * 86400 + value.seconds) * 1000000 + value.microseconds


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SessionInterval:
    start_time: datetime
    end_time: datetime

    @property
    def duration_microseconds(self) -> int:
        return _micros(self.end_time - self.start_time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "start_time": _iso(self.start_time), "end_time": _iso(self.end_time),
            "duration_microseconds": self.duration_microseconds,
        }


@dataclass(frozen=True)
class OpenSessionSegment:
    start_time: datetime
    end_time: datetime
    tick_count: int
    source_row_start: Optional[int]
    source_row_end: Optional[int]
    first_tick_at: Optional[datetime]
    last_tick_at: Optional[datetime]
    maximum_intertick_microseconds: Optional[int]
    start_boundary_silence_microseconds: Optional[int]
    end_boundary_silence_microseconds: Optional[int]

    @property
    def duration_microseconds(self) -> int:
        return _micros(self.end_time - self.start_time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "start_time": _iso(self.start_time), "end_time": _iso(self.end_time),
            "duration_microseconds": self.duration_microseconds,
            "tick_count": self.tick_count,
            "source_row_start": self.source_row_start, "source_row_end": self.source_row_end,
            "first_tick_at": _iso(self.first_tick_at), "last_tick_at": _iso(self.last_tick_at),
            "maximum_intertick_microseconds": self.maximum_intertick_microseconds,
            "start_boundary_silence_microseconds": self.start_boundary_silence_microseconds,
            "end_boundary_silence_microseconds": self.end_boundary_silence_microseconds,
        }


@dataclass(frozen=True)
class ObservedQuoteOhlc:
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float

    def as_dict(self) -> Dict[str, float]:
        return {
            "bid_open": self.bid_open, "bid_high": self.bid_high,
            "bid_low": self.bid_low, "bid_close": self.bid_close,
            "ask_open": self.ask_open, "ask_high": self.ask_high,
            "ask_low": self.ask_low, "ask_close": self.ask_close,
        }


@dataclass(frozen=True)
class SessionBucket:
    start_time: datetime
    end_time: datetime
    state: str
    observation_status: str
    scheduled_open_microseconds: int
    scheduled_closed_microseconds: int
    tick_count: int
    source_row_start: Optional[int]
    source_row_end: Optional[int]
    first_tick_at: Optional[datetime]
    last_tick_at: Optional[datetime]
    observed_ohlc: Optional[ObservedQuoteOhlc]
    open_segments: Tuple[OpenSessionSegment, ...]
    closed_intervals: Tuple[SessionInterval, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "start_time": _iso(self.start_time), "end_time": _iso(self.end_time),
            "state": self.state, "observation_status": self.observation_status,
            "scheduled_open_microseconds": self.scheduled_open_microseconds,
            "scheduled_closed_microseconds": self.scheduled_closed_microseconds,
            "tick_count": self.tick_count,
            "source_row_start": self.source_row_start, "source_row_end": self.source_row_end,
            "first_tick_at": _iso(self.first_tick_at), "last_tick_at": _iso(self.last_tick_at),
            "observed_ohlc": None if self.observed_ohlc is None else self.observed_ohlc.as_dict(),
            "open_segments": [segment.as_dict() for segment in self.open_segments],
            "closed_intervals": [interval.as_dict() for interval in self.closed_intervals],
        }


@dataclass(frozen=True)
class SessionBucketAggregation:
    parsed: ParsedMt5Ticks
    calendar: SessionCalendarArtifact
    coverage_start: datetime
    coverage_end: datetime
    availability_basis: AvailabilityBasis
    buckets: Tuple[SessionBucket, ...]

    @property
    def ticks(self) -> Tuple[BidAskTick, ...]:
        return self.parsed.ticks

    def as_dict(self) -> Dict[str, Any]:
        """Serialize observations, never the full retained tick payload.

        Recompute rather than treating publicly constructible/replaced metadata
        as authority. IO writes retained source ticks separately.
        """
        checked = validate_session_bucket_aggregation(self)
        counts = {state: 0 for state in ("full_open", "partial_open", "closed")}
        missing = empty_segments = segment_count = 0
        for bucket in checked.buckets:
            counts[bucket.state] += 1
            missing += bucket.observation_status == "missing_open_data"
            segment_count += len(bucket.open_segments)
            empty_segments += sum(segment.tick_count == 0 for segment in bucket.open_segments)
        return {
            "schema": SCHEMA_NAME, "version": SCHEMA_VERSION,
            "purpose": "import_only_observations_not_strategy_bars_or_executable_prices",
            "coverage_start": _iso(checked.coverage_start), "coverage_end": _iso(checked.coverage_end),
            "bucket_seconds": BUCKET_SECONDS,
            "availability_basis": checked.availability_basis.value,
            "calendar_id": checked.calendar.calendar_id, "calendar_revision": checked.calendar.revision,
            "calendar_fingerprint": checked.calendar.fingerprint,
            "raw_sha256": checked.parsed.raw_sha256,
            "summary": {
                "tick_count": len(checked.ticks), "bucket_count": len(checked.buckets),
                "full_open_count": counts["full_open"], "partial_open_count": counts["partial_open"],
                "closed_count": counts["closed"], "missing_open_data_count": missing,
                "open_segment_count": segment_count, "empty_open_segment_count": empty_segments,
            },
            "warnings": [
                "Observed tick extrema do not establish complete feed coverage or executable boundary prices.",
                "No receipt times, forward-filled prices, strategy bars, or broker orders are created.",
            ],
            "buckets": [bucket.as_dict() for bucket in checked.buckets],
        }


def _validated_source(parsed: ParsedMt5Ticks) -> ParsedMt5Ticks:
    if type(parsed) is not ParsedMt5Ticks or type(parsed.ticks) is not tuple or type(parsed.columns) is not tuple:
        raise SessionBucketError("parsed must contain immutable ParsedMt5Ticks metadata")
    # Validate tuple length and immutable parser metadata before allocating copies.
    ParsedMt5Ticks(parsed.ticks, parsed.encoding, parsed.delimiter, parsed.columns, parsed.raw_sha256)
    ticks = []
    for tick in parsed.ticks:
        if type(tick) is not BidAskTick:
            raise SessionBucketError("parsed ticks must contain exact BidAskTick values")
        ticks.append(BidAskTick(
            tick.timestamp, tick.bid, tick.ask, tick.source_row,
            last=tick.last, volume=tick.volume, flags=tick.flags,
        ))
    return ParsedMt5Ticks(tuple(ticks), parsed.encoding, parsed.delimiter, parsed.columns, parsed.raw_sha256)


def _validated_calendar(calendar: SessionCalendarArtifact) -> SessionCalendarArtifact:
    if type(calendar) is not SessionCalendarArtifact or type(calendar.closures) is not tuple:
        raise SessionBucketError("calendar must contain immutable SessionCalendarArtifact metadata")
    if any(type(closure) is not ScheduledClosure for closure in calendar.closures):
        raise SessionBucketError("calendar closures must contain exact ScheduledClosure values")
    return parse_session_calendar_bytes(calendar.canonical_json.encode("utf-8"))


def aggregate_mt5_session_buckets(
    parsed: ParsedMt5Ticks, *, coverage_start: datetime, coverage_end: datetime,
    calendar: SessionCalendarArtifact, availability_basis: AvailabilityBasis,
) -> SessionBucketAggregation:
    """Partition complete snapshots into exact M15 session observations.

    Overall coverage remains a half-open, whole-UTC-hour range inside the finite
    supplied calendar. Closure boundaries may have microsecond precision. Work
    is linear in ticks + buckets + closure intersections, not ticks * closures.
    No empty open interval is silently identified as a scheduled closure.
    """
    coverage_start = _utc(coverage_start, "coverage_start")
    coverage_end = _utc(coverage_end, "coverage_end")
    if coverage_end <= coverage_start:
        raise SessionBucketError("coverage_end must be after coverage_start")
    if any(value.minute or value.second or value.microsecond for value in (coverage_start, coverage_end)):
        raise SessionBucketError("session import requires whole UTC hour coverage boundaries")
    bucket_count = _micros(coverage_end - coverage_start) // _BUCKET_MICROSECONDS
    if bucket_count > MAX_SESSION_BUCKETS:
        raise SessionBucketError("coverage exceeds MAX_SESSION_BUCKETS ({})".format(MAX_SESSION_BUCKETS))
    if availability_basis not in (AvailabilityBasis.SYNTHETIC, AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION):
        raise SessionBucketError("session import requires synthetic or historical_close_assumption availability")
    availability_basis = AvailabilityBasis(availability_basis)
    calendar = _validated_calendar(calendar)
    if coverage_start < calendar.coverage_start or coverage_end > calendar.coverage_end:
        raise SessionBucketError("coverage must lie wholly inside finite calendar coverage")
    parsed = _validated_source(parsed)
    if parsed.ticks[0].timestamp < coverage_start or parsed.ticks[-1].timestamp >= coverage_end:
        raise SessionBucketError("all ticks must lie inside half-open coverage; trimming is not supported")

    ticks = parsed.ticks
    closures = calendar.closures
    closure_index = tick_index = 0
    buckets: List[SessionBucket] = []
    bucket_start = coverage_start
    for _ in range(bucket_count):
        bucket_end = bucket_start + _BUCKET
        while closure_index < len(closures) and closures[closure_index].end <= bucket_start:
            closure_index += 1
        closed: List[SessionInterval] = []
        open_intervals: List[SessionInterval] = []
        cursor = bucket_start
        scan = closure_index
        while scan < len(closures) and closures[scan].start < bucket_end:
            closure = closures[scan]
            start = max(bucket_start, closure.start)
            end = min(bucket_end, closure.end)
            if cursor < start:
                open_intervals.append(SessionInterval(cursor, start))
            closed.append(SessionInterval(start, end))
            cursor = end
            scan += 1
        if cursor < bucket_end:
            open_intervals.append(SessionInterval(cursor, bucket_end))

        bucket_first_index = tick_index
        segments: List[OpenSessionSegment] = []
        for interval in open_intervals:
            if tick_index < len(ticks) and ticks[tick_index].timestamp < interval.start_time:
                raise SessionBucketError("source tick lies inside a scheduled closure")
            segment_first_index = tick_index
            maximum_gap = 0
            previous_at = None
            while tick_index < len(ticks) and ticks[tick_index].timestamp < interval.end_time:
                timestamp = ticks[tick_index].timestamp
                if previous_at is not None:
                    maximum_gap = max(maximum_gap, _micros(timestamp - previous_at))
                previous_at = timestamp
                tick_index += 1
            count = tick_index - segment_first_index
            first = ticks[segment_first_index] if count else None
            last = ticks[tick_index - 1] if count else None
            segments.append(OpenSessionSegment(
                interval.start_time, interval.end_time, count,
                None if first is None else first.source_row, None if last is None else last.source_row,
                None if first is None else first.timestamp, None if last is None else last.timestamp,
                maximum_gap if count else None,
                None if first is None else _micros(first.timestamp - interval.start_time),
                None if last is None else _micros(interval.end_time - last.timestamp),
            ))
        if tick_index < len(ticks) and ticks[tick_index].timestamp < bucket_end:
            raise SessionBucketError("source tick lies inside a scheduled closure")
        count = tick_index - bucket_first_index
        first = ticks[bucket_first_index] if count else None
        last = ticks[tick_index - 1] if count else None
        observed = None
        if count:
            # Do not synthesize a snapshot at the scheduled segment/bucket edge.
            group = ticks[bucket_first_index:tick_index]
            observed = ObservedQuoteOhlc(
                first.bid, max(tick.bid for tick in group), min(tick.bid for tick in group), last.bid,
                first.ask, max(tick.ask for tick in group), min(tick.ask for tick in group), last.ask,
            )
        closed_micros = sum(interval.duration_microseconds for interval in closed)
        state = "closed" if not open_intervals else ("partial_open" if closed else "full_open")
        status = "closed" if state == "closed" else (
            "missing_open_data" if any(segment.tick_count == 0 for segment in segments) else "observed"
        )
        buckets.append(SessionBucket(
            bucket_start, bucket_end, state, status, _BUCKET_MICROSECONDS - closed_micros, closed_micros,
            count, None if first is None else first.source_row, None if last is None else last.source_row,
            None if first is None else first.timestamp, None if last is None else last.timestamp,
            observed, tuple(segments), tuple(closed),
        ))
        bucket_start = bucket_end
    if tick_index != len(ticks):
        raise SessionBucketError("every source tick must be assigned exactly once")
    return SessionBucketAggregation(parsed, calendar, coverage_start, coverage_end, availability_basis, tuple(buckets))


def validate_session_bucket_aggregation(value: SessionBucketAggregation) -> SessionBucketAggregation:
    """Recompute and compare every observation before treating flags as evidence."""
    if type(value) is not SessionBucketAggregation or type(value.buckets) is not tuple:
        raise SessionBucketError("aggregation must have immutable SessionBucketAggregation metadata")
    expected = aggregate_mt5_session_buckets(
        value.parsed, coverage_start=value.coverage_start, coverage_end=value.coverage_end,
        calendar=value.calendar, availability_basis=value.availability_basis,
    )
    if not _same_typed(value, expected):
        raise SessionBucketError("aggregation metadata does not match its retained ticks and calendar")
    return expected


def _same_typed(left: object, right: object) -> bool:
    # Dataclass equality alone accepts forged True == 1 and list-like metadata.
    if type(left) is not type(right):
        return False
    if is_dataclass(right):
        return all(_same_typed(getattr(left, field.name), getattr(right, field.name)) for field in fields(right))
    if isinstance(right, tuple):
        return len(left) == len(right) and all(_same_typed(a, b) for a, b in zip(left, right))
    return left == right


__all__ = [
    "SCHEMA_NAME", "SCHEMA_VERSION", "MAX_SESSION_BUCKETS", "BUCKET_SECONDS", "SessionBucketError",
    "SessionInterval", "OpenSessionSegment", "ObservedQuoteOhlc", "SessionBucket", "SessionBucketAggregation",
    "aggregate_mt5_session_buckets", "validate_session_bucket_aggregation",
]
