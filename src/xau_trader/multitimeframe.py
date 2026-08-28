"""Pure multi-timeframe diagnostics for the supply-and-demand research intake.

This module deliberately has no broker, order, backtest, or P&L dependencies.  It
only validates completed quote bars, derives provisionally UTC-aligned H1 bars
from M15 bars, and identifies strictly confirmed H1 pivots.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
from typing import Iterable, Optional, Sequence, Tuple

from .domain import AvailabilityBasis, QuoteBar


M15_DURATION = timedelta(minutes=15)
H1_DURATION = timedelta(hours=1)
PIVOT_LEFT_WING = 3
PIVOT_RIGHT_WING = 3


class MultiTimeframeDataError(ValueError):
    """Raised when bars cannot be used without guessing about missing data."""


class PivotKind(str, Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class H1PivotEvent:
    """One strict H1 pivot, usable only after all three right-wing bars.

    ``confirmed_at`` is the close time of the third right-hand H1 bar.
    ``available_at`` is the latest availability time anywhere in the seven-bar
    window.  The associated basis comes from that gating bar (the later bar wins
    a tie), so a delayed input cannot become point-in-time visible early.
    """

    kind: PivotKind
    pivot_bar_start: datetime
    pivot_bar_end: datetime
    confirmed_at: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    bid_price: float
    ask_price: float
    mid_price: float
    left_wing: int = PIVOT_LEFT_WING
    right_wing: int = PIVOT_RIGHT_WING


def _is_boundary(value: datetime, minute: int) -> bool:
    return value.minute == minute and value.second == 0 and value.microsecond == 0


def _validate_bars(
    bars: Sequence[QuoteBar],
    *,
    duration: timedelta,
    label: str,
    require_hour_boundary: bool,
) -> None:
    for index, bar in enumerate(bars):
        if not isinstance(bar, QuoteBar):
            raise TypeError("{} input must contain QuoteBar values".format(label))
        if bar.timestamp - bar.start_time != duration:
            raise MultiTimeframeDataError(
                "{} bar {} must span exactly {} minutes".format(
                    label, index, int(duration.total_seconds() // 60)
                )
            )
        if bar.start_time.tzinfo != timezone.utc or bar.timestamp.tzinfo != timezone.utc:
            raise MultiTimeframeDataError("{} bars must be normalized to UTC".format(label))
        expected_minute = 0 if require_hour_boundary else bar.start_time.minute
        if require_hour_boundary:
            aligned = _is_boundary(bar.start_time, expected_minute)
        else:
            aligned = (
                bar.start_time.minute % 15 == 0
                and bar.start_time.second == 0
                and bar.start_time.microsecond == 0
            )
        if not aligned:
            raise MultiTimeframeDataError(
                "{} bar {} is not aligned to a UTC {} boundary".format(
                    label, index, "hour" if require_hour_boundary else "15-minute"
                )
            )
        if index and bar.start_time != bars[index - 1].timestamp:
            raise MultiTimeframeDataError(
                "{} bar {} is not contiguous with the previous bar".format(label, index)
            )


def _availability_gate(bars: Sequence[QuoteBar]) -> QuoteBar:
    # Enumerating makes the later bar the deterministic winner when availability
    # timestamps tie.
    return max(enumerate(bars), key=lambda item: (item[1].available_at, item[0]))[1]


def aggregate_m15_to_h1(bars: Iterable[QuoteBar]) -> Tuple[QuoteBar, ...]:
    """Aggregate complete contiguous UTC M15 bars into immutable H1 bars.

    The function fails closed for partial hours, gaps, overlaps, bad durations, or
    inconsistent UTC boundaries.  H1 availability is the maximum child
    ``available_at``.  Volume is summed only when all four child volumes exist;
    otherwise it remains unknown.
    """

    source = tuple(bars)
    if not source:
        return ()
    _validate_bars(
        source,
        duration=M15_DURATION,
        label="M15",
        require_hour_boundary=False,
    )
    if not _is_boundary(source[0].start_time, 0):
        raise MultiTimeframeDataError("M15 input must begin on a UTC hour boundary")
    if not _is_boundary(source[-1].timestamp, 0):
        raise MultiTimeframeDataError("M15 input must end on a UTC hour boundary")
    if len(source) % 4:
        raise MultiTimeframeDataError("M15 input must contain exactly four bars per hour")

    result = []
    for offset in range(0, len(source), 4):
        children = source[offset : offset + 4]
        if children[-1].timestamp - children[0].start_time != H1_DURATION:
            raise MultiTimeframeDataError(
                "M15 bars {}-{} do not form exactly one UTC hour".format(
                    offset, offset + 3
                )
            )
        gate = _availability_gate(children)
        volume: Optional[float]
        if all(child.volume is not None for child in children):
            volume = math.fsum(child.volume for child in children if child.volume is not None)
        else:
            volume = None
        result.append(
            QuoteBar(
                start_time=children[0].start_time,
                timestamp=children[-1].timestamp,
                available_at=gate.available_at,
                availability_basis=gate.availability_basis,
                bid_open=children[0].bid_open,
                bid_high=max(child.bid_high for child in children),
                bid_low=min(child.bid_low for child in children),
                bid_close=children[-1].bid_close,
                ask_open=children[0].ask_open,
                ask_high=max(child.ask_high for child in children),
                ask_low=min(child.ask_low for child in children),
                ask_close=children[-1].ask_close,
                volume=volume,
            )
        )
    return tuple(result)


def _mid_high(bar: QuoteBar) -> float:
    return (bar.bid_high + bar.ask_high) / 2.0


def _mid_low(bar: QuoteBar) -> float:
    return (bar.bid_low + bar.ask_low) / 2.0


def _pivot_event(
    kind: PivotKind,
    pivot: QuoteBar,
    window: Sequence[QuoteBar],
) -> H1PivotEvent:
    gate = _availability_gate(window)
    bid_price = pivot.bid_high if kind == PivotKind.HIGH else pivot.bid_low
    ask_price = pivot.ask_high if kind == PivotKind.HIGH else pivot.ask_low
    return H1PivotEvent(
        kind=kind,
        pivot_bar_start=pivot.start_time,
        pivot_bar_end=pivot.timestamp,
        confirmed_at=window[-1].timestamp,
        available_at=gate.available_at,
        availability_basis=gate.availability_basis,
        bid_price=bid_price,
        ask_price=ask_price,
        mid_price=(bid_price + ask_price) / 2.0,
    )


def confirmed_h1_pivots(
    bars: Iterable[QuoteBar], *, as_of: Optional[datetime] = None
) -> Tuple[H1PivotEvent, ...]:
    """Return strict 3-left/3-right H1 pivots in deterministic time order.

    Highs and lows are compared on the arithmetic mid of the matching bid/ask
    extremes.  Equality with any wing bar disqualifies the pivot.  Supplying
    ``as_of`` returns only events actually available by that timezone-aware time.
    """

    source = tuple(bars)
    if as_of is not None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        as_of = as_of.astimezone(timezone.utc)
    if not source:
        return ()
    _validate_bars(
        source,
        duration=H1_DURATION,
        label="H1",
        require_hour_boundary=True,
    )
    events = []
    window_size = PIVOT_LEFT_WING + 1 + PIVOT_RIGHT_WING
    for start in range(0, len(source) - window_size + 1):
        window = source[start : start + window_size]
        pivot = window[PIVOT_LEFT_WING]
        neighbors = window[:PIVOT_LEFT_WING] + window[PIVOT_LEFT_WING + 1 :]
        if all(_mid_high(pivot) > _mid_high(bar) for bar in neighbors):
            event = _pivot_event(PivotKind.HIGH, pivot, window)
            if as_of is None or event.available_at <= as_of:
                events.append(event)
        if all(_mid_low(pivot) < _mid_low(bar) for bar in neighbors):
            event = _pivot_event(PivotKind.LOW, pivot, window)
            if as_of is None or event.available_at <= as_of:
                events.append(event)
    return tuple(events)
