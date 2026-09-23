"""Bounded, offline import of complete bid/ask snapshots from MT5 tick exports.

This deliberately supports only six documented MT5 tick columns, optionally
followed by FLAGS. Incremental/partial quote updates are not reconstructed. A
caller supplies a fixed server-to-UTC offset and explicit calendar evidence;
neither broker timezone rules nor missing prices are inferred.
"""

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import math
import re
from typing import List, Optional, Tuple

from .domain import AvailabilityBasis, QuoteBar
from .session_calendar import SessionCalendarArtifact


MAX_RAW_BYTES = 64 * 1024 * 1024
MAX_TICKS = 500000
MT5_COLUMNS = ("<DATE>", "<TIME>", "<BID>", "<ASK>", "<LAST>", "<VOLUME>")
KNOWN_FLAG_MASK = 2 | 4 | 8 | 16 | 32 | 64
_QUARTER_HOUR = timedelta(minutes=15)
_DATE = re.compile(r"[0-9]{4}\.[0-9]{2}\.[0-9]{2}\Z")
_TIME = re.compile(r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,3}))?\Z")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


class Mt5TickError(ValueError):
    """Invalid or unsupported tick evidence; messages never include raw rows."""


def _utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise Mt5TickError("{} must be a datetime".format(field_name))
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise Mt5TickError("{} must use an explicit UTC timezone".format(field_name))
    return value.astimezone(timezone.utc)


def _finite_number(value: object, field_name: str, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Mt5TickError("{} must be a finite number".format(field_name))
    try:
        clean = float(value)
    except (ValueError, OverflowError) as exc:
        raise Mt5TickError("{} must be a finite number".format(field_name)) from exc
    if not math.isfinite(clean) or (clean <= 0 if positive else clean < 0):
        adjective = "positive" if positive else "non-negative"
        raise Mt5TickError("{} must be finite and {}".format(field_name, adjective))
    return clean


@dataclass(frozen=True)
class BidAskTick:
    timestamp: datetime
    bid: float
    ask: float
    source_row: int
    last: Optional[float] = None
    volume: Optional[float] = None
    flags: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "tick.timestamp"))
        for name in ("bid", "ask"):
            object.__setattr__(self, name, _finite_number(getattr(self, name), name, positive=True))
        if self.ask < self.bid:
            raise Mt5TickError("tick ask must not be below bid")
        if isinstance(self.source_row, bool) or not isinstance(self.source_row, int) or self.source_row < 2:
            raise Mt5TickError("source_row must be an integer at least 2")
        for name in ("last", "volume"):
            if getattr(self, name) is not None:
                object.__setattr__(self, name, _finite_number(getattr(self, name), name, positive=False))
        if self.flags is not None and (
            isinstance(self.flags, bool) or not isinstance(self.flags, int)
            or self.flags < 0 or self.flags & ~KNOWN_FLAG_MASK
        ):
            raise Mt5TickError("flags must contain only supported MT5 flag bits 2 through 64")


@dataclass(frozen=True)
class ParsedMt5Ticks:
    ticks: Tuple[BidAskTick, ...]
    encoding: str
    delimiter: str
    columns: Tuple[str, ...]
    raw_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.ticks, tuple) or not self.ticks or len(self.ticks) > MAX_TICKS:
            raise Mt5TickError("ticks must be a nonempty tuple within MAX_TICKS")
        if self.encoding not in ("utf-8", "utf-8-sig", "utf-16-le", "utf-16-be"):
            raise Mt5TickError("unsupported tick encoding")
        if self.delimiter not in (",", "\t"):
            raise Mt5TickError("unsupported tick delimiter")
        if self.columns not in (MT5_COLUMNS, MT5_COLUMNS + ("<FLAGS>",)):
            raise Mt5TickError("unsupported MT5 tick columns")
        if not isinstance(self.raw_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256) is None:
            raise Mt5TickError("raw_sha256 must be a lowercase SHA-256 digest")
        previous = None
        for tick in self.ticks:
            if not isinstance(tick, BidAskTick):
                raise Mt5TickError("ticks must contain BidAskTick values")
            if previous is not None and (
                tick.timestamp < previous.timestamp or tick.source_row <= previous.source_row
            ):
                raise Mt5TickError("ticks must preserve increasing source rows and nondecreasing timestamps")
            if (tick.flags is None) != (len(self.columns) == 6):
                raise Mt5TickError("tick flags must agree with the parsed columns")
            previous = tick


def _number(text: str, field_name: str, row: int, *, positive: bool, optional: bool = False) -> Optional[float]:
    if optional and text == "":
        return None
    if _NUMBER.fullmatch(text) is None:
        raise Mt5TickError("row {} has an invalid {} value".format(row, field_name))
    return _finite_number(float(text), "row {} {}".format(row, field_name), positive=positive)


def _timestamp(date: str, time: str, row: int, offset: timedelta) -> datetime:
    matched = _TIME.fullmatch(time)
    if _DATE.fullmatch(date) is None or matched is None:
        raise Mt5TickError("row {} has an unsupported date/time format".format(row))
    try:
        year, month, day = (int(part) for part in date.split("."))
        hour, minute, second = (int(matched.group(index)) for index in (1, 2, 3))
        fraction = matched.group(4) or "0"
        local = datetime(year, month, day, hour, minute, second, int(fraction.ljust(6, "0")), tzinfo=timezone.utc)
        return local - offset
    except (ValueError, OverflowError) as exc:
        raise Mt5TickError("row {} has an invalid or out-of-range timestamp".format(row)) from exc


def parse_mt5_tick_bytes(raw: bytes, *, utc_offset_minutes: int) -> ParsedMt5Ticks:
    """Parse at most 64 MiB / 500,000 complete snapshots without carrying quotes.

    ``utc_offset_minutes`` is server time minus UTC, e.g. +120 means a server
    timestamp of 02:00 converts to 00:00 UTC. A changing offset needs separate
    source files and separately declared offsets; this parser never applies DST.
    """
    if not isinstance(raw, bytes):
        raise Mt5TickError("raw tick input must be bytes")
    if not raw or len(raw) > MAX_RAW_BYTES:
        raise Mt5TickError("raw tick input must be nonempty and at most MAX_RAW_BYTES (64 MiB)")
    if isinstance(utc_offset_minutes, bool) or not isinstance(utc_offset_minutes, int) or not -840 <= utc_offset_minutes <= 840:
        raise Mt5TickError("utc_offset_minutes must be an integer between -840 and 840")
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        raise Mt5TickError("UTF-32 tick exports are not supported")
    if raw.startswith(b"\xff\xfe"):
        encoding, decoder = "utf-16-le", "utf-16"
    elif raw.startswith(b"\xfe\xff"):
        encoding, decoder = "utf-16-be", "utf-16"
    elif raw.startswith(b"\xef\xbb\xbf"):
        encoding, decoder = "utf-8-sig", "utf-8-sig"
    else:
        encoding, decoder = "utf-8", "utf-8"
    try:
        content = raw.decode(decoder, errors="strict")
    except UnicodeError as exc:
        raise Mt5TickError("tick input is not valid UTF-8 or BOM-marked UTF-16") from exc
    if any(ord(character) < 32 and character not in "\t\r\n" for character in content):
        raise Mt5TickError("tick input contains unsupported control characters")
    first_line = content.split("\n", 1)[0]
    if ("\t" in first_line) == ("," in first_line):
        raise Mt5TickError("tick header must use exactly one supported delimiter")
    delimiter = "\t" if "\t" in first_line else ","
    reader = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter, strict=True)
    ticks: List[BidAskTick] = []
    offset = timedelta(minutes=utc_offset_minutes)
    try:
        columns = tuple(next(reader))
        if columns not in (MT5_COLUMNS, MT5_COLUMNS + ("<FLAGS>",)):
            raise Mt5TickError("tick header must match the supported MT5 columns in order")
        for values in reader:
            row = reader.line_num
            if len(ticks) >= MAX_TICKS:
                raise Mt5TickError("tick input exceeds MAX_TICKS (500000)")
            if len(values) != len(columns):
                raise Mt5TickError("row {} has an incorrect field count".format(row))
            timestamp = _timestamp(values[0], values[1], row, offset)
            flags = None
            if len(columns) == 7:
                if re.fullmatch(r"[0-9]+", values[6]) is None or len(values[6]) > 3:
                    raise Mt5TickError("row {} has an invalid FLAGS value".format(row))
                flags = int(values[6])
            tick = BidAskTick(
                timestamp=timestamp,
                bid=_number(values[2], "BID", row, positive=True),
                ask=_number(values[3], "ASK", row, positive=True),
                source_row=row,
                last=_number(values[4], "LAST", row, positive=False, optional=True),
                volume=_number(values[5], "VOLUME", row, positive=False, optional=True),
                flags=flags,
            )
            if ticks and tick.timestamp < ticks[-1].timestamp:
                raise Mt5TickError("row {} moves backwards in UTC time".format(row))
            ticks.append(tick)
    except (csv.Error, StopIteration) as exc:
        raise Mt5TickError("tick input contains malformed CSV or an empty header") from exc
    return ParsedMt5Ticks(tuple(ticks), encoding, delimiter, columns, hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class TickBucketDiagnostic:
    start_time: datetime
    end_time: datetime
    tick_count: int
    first_tick_at: datetime
    last_tick_at: datetime
    maximum_intertick_seconds: float
    start_boundary_silence_seconds: float
    end_boundary_silence_seconds: float


@dataclass(frozen=True)
class TickAggregation:
    bars: Tuple[QuoteBar, ...]
    bucket_diagnostics: Tuple[TickBucketDiagnostic, ...]
    tick_count: int
    maximum_intertick_seconds: float
    maximum_boundary_silence_seconds: float
    warnings: Tuple[str, ...]


def aggregate_mt5_ticks_m15(
    parsed: ParsedMt5Ticks,
    *,
    coverage_start: datetime,
    coverage_end: datetime,
    calendar: SessionCalendarArtifact,
    availability_basis: AvailabilityBasis,
) -> TickAggregation:
    """Produce observed quote extrema, never fills or bar-boundary executable prices.

    Coverage is an explicit, half-open, whole-UTC-hour interval. Every open M15
    bucket must contain an actual complete snapshot; closed buckets must contain
    none. A bucket with one sparse quote is accepted but is not evidence of a
    complete feed, exact boundary prices, or executable historical fills.
    """
    if not isinstance(parsed, ParsedMt5Ticks):
        raise Mt5TickError("parsed must be ParsedMt5Ticks")
    if not isinstance(calendar, SessionCalendarArtifact):
        raise Mt5TickError("calendar must be SessionCalendarArtifact")
    if availability_basis not in (AvailabilityBasis.SYNTHETIC, AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION):
        raise Mt5TickError("tick import requires synthetic or historical_close_assumption availability")
    availability_basis = AvailabilityBasis(availability_basis)
    coverage_start = _utc(coverage_start, "coverage_start")
    coverage_end = _utc(coverage_end, "coverage_end")
    if coverage_start >= coverage_end:
        raise Mt5TickError("coverage_end must be after coverage_start")
    if any(value.minute or value.second or value.microsecond for value in (coverage_start, coverage_end)):
        raise Mt5TickError("tick import requires whole UTC hour coverage boundaries")
    calendar.require_hour_aligned_closures()
    calendar.validate_bar(coverage_start, coverage_start + _QUARTER_HOUR)
    calendar.validate_bar(coverage_end - _QUARTER_HOUR, coverage_end)
    for tick in parsed.ticks:
        if not coverage_start <= tick.timestamp < coverage_end:
            raise Mt5TickError("all source ticks must lie inside the half-open declared coverage; trimming is not supported")
    closed_seconds = sum(
        (closure.end - closure.start).total_seconds()
        for closure in calendar.closures
        if coverage_start <= closure.start and closure.end <= coverage_end
    )
    expected_buckets = int(((coverage_end - coverage_start).total_seconds() - closed_seconds) / 900)
    if expected_buckets > len(parsed.ticks):
        raise Mt5TickError("each open M15 bucket requires an actual tick; source contains too few ticks")
    bars: List[QuoteBar] = []
    diagnostics: List[TickBucketDiagnostic] = []
    maximum_intertick = 0.0
    group: List[BidAskTick] = []
    group_start = None

    def finish_bucket() -> None:
        end = group_start + _QUARTER_HOUR
        calendar.validate_bar(group_start, end)
        if bars:
            calendar.validate_transition(bars[-1].timestamp, group_start)
        bars.append(QuoteBar(
            start_time=group_start, timestamp=end, available_at=end,
            availability_basis=availability_basis,
            bid_open=group[0].bid, bid_high=max(item.bid for item in group),
            bid_low=min(item.bid for item in group), bid_close=group[-1].bid,
            ask_open=group[0].ask, ask_high=max(item.ask for item in group),
            ask_low=min(item.ask for item in group), ask_close=group[-1].ask,
            volume=None,
        ))
        diagnostics.append(TickBucketDiagnostic(
            start_time=group_start, end_time=end, tick_count=len(group),
            first_tick_at=group[0].timestamp, last_tick_at=group[-1].timestamp,
            maximum_intertick_seconds=max(
                ((right.timestamp - left.timestamp).total_seconds() for left, right in zip(group, group[1:])),
                default=0.0,
            ),
            start_boundary_silence_seconds=(group[0].timestamp - group_start).total_seconds(),
            end_boundary_silence_seconds=(end - group[-1].timestamp).total_seconds(),
        ))

    previous = None
    for tick in parsed.ticks:
        start = tick.timestamp.replace(minute=(tick.timestamp.minute // 15) * 15, second=0, microsecond=0)
        if previous is not None:
            maximum_intertick = max(maximum_intertick, (tick.timestamp - previous.timestamp).total_seconds())
        if group and start != group_start:
            finish_bucket()
            group = []
        group_start = start
        group.append(tick)
        previous = tick
    finish_bucket()
    if len(bars) != expected_buckets or bars[0].start_time != coverage_start or bars[-1].timestamp != coverage_end:
        raise Mt5TickError("every open M15 bucket in declared coverage must contain an actual tick")
    maximum_boundary = max(
        max(item.start_boundary_silence_seconds, item.end_boundary_silence_seconds)
        for item in diagnostics
    )
    return TickAggregation(
        bars=tuple(bars), bucket_diagnostics=tuple(diagnostics), tick_count=len(parsed.ticks),
        maximum_intertick_seconds=maximum_intertick,
        maximum_boundary_silence_seconds=maximum_boundary,
        warnings=(
            "Historical tick snapshots do not establish observed receipt times; availability is assumed at bar close.",
            "OHLC values are observed quotes inside each bucket, not proven exact boundary prices or executable fills.",
            "One or more ticks per bucket does not prove feed completeness; inspect tick counts and silence diagnostics.",
            "The maximum intertick interval includes documented closures; no quote was filled, seeded, or carried forward.",
            "The UTC offset and calendar are caller-supplied evidence, not independently verified broker schedule rules.",
        ),
    )
