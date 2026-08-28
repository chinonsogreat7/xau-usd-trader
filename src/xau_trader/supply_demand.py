"""Point-in-time H1 impulse and supply/demand-zone diagnostics.

This module implements research events only.  It has no order, broker, account,
backtest, or P&L dependencies, and a :class:`ZoneEvent` is only evidence that a
configured formation rule matched.  The separate ``zone_lifecycle`` module
handles invalidation, retest episodes, and selection; trade decisions remain a
later slice.

All prices used by these diagnostics are arithmetic bid/ask mid prices.  The
policy enums make the unresolved interpretations in the strategy intake
explicit; callers cannot obtain impulse or zone events without selecting them.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import math
from typing import Iterable, Optional, Sequence, Tuple

from .domain import AvailabilityBasis, QuoteBar
from .multitimeframe import H1_DURATION, MultiTimeframeDataError


IMPULSE_WINDOW_BARS = 4


class ImpulseDirection(str, Enum):
    """Directional side of a qualifying four-bar H1 impulse."""

    BULLISH = "bullish"
    BEARISH = "bearish"


class ImpulseMovementPolicy(str, Enum):
    """How directional movement is measured inside an impulse window.

    ``NET_OPEN_TO_CLOSE`` measures the first open to the final close.
    ``FULL_WINDOW_RANGE`` uses the high/low span after candle direction has
    independently selected the side. ``SUM_DIRECTIONAL_BODIES`` adds only
    bodies pointing in the candidate direction.
    """

    NET_OPEN_TO_CLOSE = "net_open_to_close"
    FULL_WINDOW_RANGE = "full_window_range"
    SUM_DIRECTIONAL_BODIES = "sum_directional_bodies"


class AtrMethod(str, Enum):
    """Deterministic ATR algorithm and seed convention."""

    WILDER_SMA_SEED = "wilder_sma_seed"
    SIMPLE_MOVING_AVERAGE = "simple_moving_average"


class AtrTiming(str, Enum):
    """Bar at which the ATR threshold is snapshotted for an impulse.

    ``BEFORE_IMPULSE_WINDOW`` avoids using any of the four candidate bars.
    ``BEFORE_IMPULSE_END`` includes the first three candidate bars but excludes
    the confirming fourth bar. ``IMPULSE_END_INCLUSIVE`` includes all four.
    """

    BEFORE_IMPULSE_WINDOW = "before_impulse_window"
    BEFORE_IMPULSE_END = "before_impulse_end"
    IMPULSE_END_INCLUSIVE = "impulse_end_inclusive"


class OriginSelectionPolicy(str, Enum):
    """Where the final opposite-colour origin candle may be selected."""

    LAST_OPPOSITE_BEFORE_WINDOW = "last_opposite_before_window"
    LAST_OPPOSITE_AT_OR_BEFORE_WINDOW_END = (
        "last_opposite_at_or_before_window_end"
    )


class ZoneKind(str, Enum):
    DEMAND = "demand"
    SUPPLY = "supply"


ImpulseKey = Tuple[str, ImpulseDirection, datetime, datetime]
ZoneKey = Tuple[
    ZoneKind,
    OriginSelectionPolicy,
    int,
    datetime,
    ImpulseKey,
]


@dataclass(frozen=True)
class ImpulsePolicy:
    """Fully selected rules for the four-bar H1 impulse experiment."""

    movement: ImpulseMovementPolicy
    atr_method: AtrMethod
    atr_timing: AtrTiming
    atr_period: int
    minimum_directional_bars: int
    movement_atr_multiple: float
    body_atr_multiple: float

    def __post_init__(self) -> None:
        _require_enum(self.movement, ImpulseMovementPolicy, "movement")
        _require_enum(self.atr_method, AtrMethod, "atr_method")
        _require_enum(self.atr_timing, AtrTiming, "atr_timing")
        if isinstance(self.atr_period, bool) or not isinstance(self.atr_period, int):
            raise TypeError("atr_period must be an integer")
        if self.atr_period < 1:
            raise ValueError("atr_period must be at least 1")
        if (
            isinstance(self.minimum_directional_bars, bool)
            or not isinstance(self.minimum_directional_bars, int)
        ):
            raise TypeError("minimum_directional_bars must be an integer")
        if not 1 <= self.minimum_directional_bars <= IMPULSE_WINDOW_BARS:
            raise ValueError("minimum_directional_bars must be between 1 and 4")
        _require_non_negative_finite(
            self.movement_atr_multiple, "movement_atr_multiple"
        )
        _require_non_negative_finite(self.body_atr_multiple, "body_atr_multiple")

    @property
    def canonical_identity(self) -> str:
        """Complete versioned policy identity with canonical numeric strings."""

        return ";".join(
            (
                "impulse-policy-v1",
                "movement={}".format(self.movement.value),
                "atr_method={}".format(self.atr_method.value),
                "atr_timing={}".format(self.atr_timing.value),
                "atr_period={}".format(self.atr_period),
                "minimum_directional_bars={}".format(
                    self.minimum_directional_bars
                ),
                "movement_atr_multiple={}".format(
                    _canonical_number(self.movement_atr_multiple)
                ),
                "body_atr_multiple={}".format(
                    _canonical_number(self.body_atr_multiple)
                ),
            )
        )

    @property
    def fingerprint(self) -> str:
        """SHA-256 fingerprint of every selected impulse-policy field."""

        return hashlib.sha256(self.canonical_identity.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class AtrEvent:
    """One completed H1 ATR snapshot and its point-in-time availability."""

    bar_start: datetime
    bar_end: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    method: AtrMethod
    period: int
    true_range: float
    value: float


@dataclass(frozen=True)
class ImpulseEvent:
    """One qualifying four-bar H1 window under an explicit policy."""

    direction: ImpulseDirection
    window_start: datetime
    window_end: datetime
    confirmed_at: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    movement_policy: ImpulseMovementPolicy
    atr_method: AtrMethod
    atr_timing: AtrTiming
    atr_period: int
    minimum_directional_bars: int
    policy_fingerprint: str
    atr_snapshot_bar_end: datetime
    atr_value: float
    directional_movement: float
    directional_bar_count: int
    largest_body: float
    movement_atr_multiple: float
    body_atr_multiple: float
    window_bars: int = IMPULSE_WINDOW_BARS

    @property
    def key(self) -> ImpulseKey:
        """Stable identity binding the window, side, and complete policy."""

        return (
            self.policy_fingerprint,
            self.direction,
            self.window_start,
            self.window_end,
        )


@dataclass(frozen=True)
class ZoneEvent:
    """Immutable formation of a supply or demand zone from one impulse.

    Events are deliberately not coalesced when overlapping impulse windows use
    the same origin.  Coalescing would be lifecycle policy, and keeping one
    formation event per impulse preserves point-in-time auditability.
    """

    kind: ZoneKind
    origin_policy: OriginSelectionPolicy
    origin_lookback_bars: int
    origin_bar_start: datetime
    origin_bar_end: datetime
    impulse_window_start: datetime
    impulse_window_end: datetime
    formed_at: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    lower_price: float
    upper_price: float
    impulse_key: ImpulseKey

    @property
    def key(self) -> ZoneKey:
        """Stable, float-independent identity for a formation event."""

        return (
            self.kind,
            self.origin_policy,
            self.origin_lookback_bars,
            self.origin_bar_start,
            self.impulse_key,
        )


@dataclass(frozen=True)
class _AtrCalculation:
    event: AtrEvent
    dependency_start_index: int


@dataclass(frozen=True)
class _OriginSelection:
    origin_index: int
    examined_end_index: int


def _require_enum(value: object, enum_type: object, field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError("{} must be a {}".format(field_name, enum_type.__name__))


def _require_non_negative_finite(value: float, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("{} must be a number".format(field_name))
    if not math.isfinite(value) or value < 0:
        raise ValueError("{} must be a finite non-negative number".format(field_name))


def _canonical_number(value: float) -> str:
    """Return a context-independent decimal identity for an accepted number."""

    sign, digits, exponent = Decimal(str(value)).as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    if not coefficient or all(digit == "0" for digit in coefficient):
        return "0e0"
    prefix = "-" if sign else ""
    return "{}{}e{}".format(prefix, coefficient, exponent)


def _normalize_as_of(as_of: Optional[datetime]) -> Optional[datetime]:
    if as_of is None:
        return None
    if not isinstance(as_of, datetime):
        raise TypeError("as_of must be a datetime")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return as_of.astimezone(timezone.utc)


def _validate_h1_bars(bars: Sequence[QuoteBar]) -> None:
    for index, bar in enumerate(bars):
        if not isinstance(bar, QuoteBar):
            raise TypeError("H1 input must contain QuoteBar values")
        if bar.timestamp - bar.start_time != H1_DURATION:
            raise MultiTimeframeDataError(
                "H1 bar {} must span exactly 60 minutes".format(index)
            )
        if bar.start_time.tzinfo != timezone.utc or bar.timestamp.tzinfo != timezone.utc:
            raise MultiTimeframeDataError("H1 bars must be normalized to UTC")
        if (
            bar.start_time.minute != 0
            or bar.start_time.second != 0
            or bar.start_time.microsecond != 0
        ):
            raise MultiTimeframeDataError(
                "H1 bar {} is not aligned to a UTC hour boundary".format(index)
            )
        if index and bar.start_time != bars[index - 1].timestamp:
            raise MultiTimeframeDataError(
                "H1 bar {} is not contiguous with the previous bar".format(index)
            )


def _mid_open(bar: QuoteBar) -> float:
    return (bar.bid_open + bar.ask_open) / 2.0


def _mid_high(bar: QuoteBar) -> float:
    return (bar.bid_high + bar.ask_high) / 2.0


def _mid_low(bar: QuoteBar) -> float:
    return (bar.bid_low + bar.ask_low) / 2.0


def _mid_close(bar: QuoteBar) -> float:
    return (bar.bid_close + bar.ask_close) / 2.0


def _availability_gate(bars: Sequence[QuoteBar]) -> QuoteBar:
    # A later input bar deterministically wins equal availability timestamps.
    return max(enumerate(bars), key=lambda item: (item[1].available_at, item[0]))[1]


def _true_ranges(bars: Sequence[QuoteBar]) -> Tuple[float, ...]:
    values = []
    for index, bar in enumerate(bars):
        high = _mid_high(bar)
        low = _mid_low(bar)
        if index == 0:
            values.append(high - low)
            continue
        previous_close = _mid_close(bars[index - 1])
        values.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    return tuple(values)


def _atr_calculations(
    bars: Sequence[QuoteBar], *, period: int, method: AtrMethod
) -> Tuple[Optional[_AtrCalculation], ...]:
    true_ranges = _true_ranges(bars)
    calculations = [None] * len(bars)  # type: list
    previous_wilder: Optional[float] = None

    for index in range(len(bars)):
        if index < period - 1:
            continue
        if method == AtrMethod.SIMPLE_MOVING_AVERAGE:
            first_tr_index = index - period + 1
            value = math.fsum(true_ranges[first_tr_index : index + 1]) / period
            # TR at first_tr_index needs the prior close unless it is TR[0].
            dependency_start = max(0, first_tr_index - 1)
        elif method == AtrMethod.WILDER_SMA_SEED:
            if previous_wilder is None:
                value = math.fsum(true_ranges[:period]) / period
            else:
                value = (
                    previous_wilder * (period - 1) + true_ranges[index]
                ) / period
            previous_wilder = value
            # Recursive Wilder values retain every dependency from the seed.
            # Period one has a zero-weight prior ATR; only the prior close used
            # by the current true range remains a dependency.
            dependency_start = 0 if period > 1 else max(0, index - 1)
        else:  # Guard against future enum additions without implementation.
            raise ValueError("atr_method is not implemented")

        dependencies = bars[dependency_start : index + 1]
        gate = _availability_gate(dependencies)
        calculations[index] = _AtrCalculation(
            event=AtrEvent(
                bar_start=bars[index].start_time,
                bar_end=bars[index].timestamp,
                available_at=gate.available_at,
                availability_basis=gate.availability_basis,
                method=method,
                period=period,
                true_range=true_ranges[index],
                value=value,
            ),
            dependency_start_index=dependency_start,
        )
    return tuple(calculations)


def h1_atr_events(
    bars: Iterable[QuoteBar],
    *,
    period: int,
    method: AtrMethod,
    as_of: Optional[datetime] = None
) -> Tuple[AtrEvent, ...]:
    """Return deterministic H1 ATR snapshots after a complete warm-up.

    Wilder ATR uses the arithmetic mean of the first ``period`` true ranges as
    its seed.  SMA ATR is a rolling mean.  The first data bar's true range is its
    high-low range; subsequent true ranges use the prior close.  Availability is
    the latest availability among the exact bars on which each value depends.
    """

    source = tuple(bars)
    _validate_h1_bars(source)
    if isinstance(period, bool) or not isinstance(period, int):
        raise TypeError("period must be an integer")
    if period < 1:
        raise ValueError("period must be at least 1")
    _require_enum(method, AtrMethod, "method")
    normalized_as_of = _normalize_as_of(as_of)
    calculations = _atr_calculations(source, period=period, method=method)
    return tuple(
        calculation.event
        for calculation in calculations
        if calculation is not None
        and (
            normalized_as_of is None
            or calculation.event.available_at <= normalized_as_of
        )
    )


def _atr_snapshot_index(window_start: int, timing: AtrTiming) -> int:
    if timing == AtrTiming.BEFORE_IMPULSE_WINDOW:
        return window_start - 1
    if timing == AtrTiming.BEFORE_IMPULSE_END:
        return window_start + IMPULSE_WINDOW_BARS - 2
    if timing == AtrTiming.IMPULSE_END_INCLUSIVE:
        return window_start + IMPULSE_WINDOW_BARS - 1
    raise ValueError("atr_timing is not implemented")


def _directional_movement(
    bars: Sequence[QuoteBar],
    *,
    direction: ImpulseDirection,
    policy: ImpulseMovementPolicy
) -> float:
    if policy == ImpulseMovementPolicy.NET_OPEN_TO_CLOSE:
        if direction == ImpulseDirection.BULLISH:
            return _mid_close(bars[-1]) - _mid_open(bars[0])
        return _mid_open(bars[0]) - _mid_close(bars[-1])
    if policy == ImpulseMovementPolicy.FULL_WINDOW_RANGE:
        return max(_mid_high(bar) for bar in bars) - min(
            _mid_low(bar) for bar in bars
        )
    if policy == ImpulseMovementPolicy.SUM_DIRECTIONAL_BODIES:
        if direction == ImpulseDirection.BULLISH:
            return math.fsum(
                max(0.0, _mid_close(bar) - _mid_open(bar)) for bar in bars
            )
        return math.fsum(
            max(0.0, _mid_open(bar) - _mid_close(bar)) for bar in bars
        )
    raise ValueError("movement policy is not implemented")


def _directional_count(
    bars: Sequence[QuoteBar], direction: ImpulseDirection
) -> int:
    if direction == ImpulseDirection.BULLISH:
        return sum(_mid_close(bar) > _mid_open(bar) for bar in bars)
    return sum(_mid_close(bar) < _mid_open(bar) for bar in bars)


def _impulse_event(
    source: Sequence[QuoteBar],
    *,
    window_start_index: int,
    direction: ImpulseDirection,
    policy: ImpulsePolicy,
    atr: _AtrCalculation
) -> Optional[ImpulseEvent]:
    window_end_index = window_start_index + IMPULSE_WINDOW_BARS - 1
    window = source[window_start_index : window_end_index + 1]
    directional_count = _directional_count(window, direction)
    if directional_count < policy.minimum_directional_bars:
        return None

    movement = _directional_movement(
        window, direction=direction, policy=policy.movement
    )
    bodies = tuple(abs(_mid_close(bar) - _mid_open(bar)) for bar in window)
    largest_body = max(bodies)
    atr_value = atr.event.value
    if movement < policy.movement_atr_multiple * atr_value:
        return None
    if largest_body < policy.body_atr_multiple * atr_value:
        return None

    dependency_start = min(atr.dependency_start_index, window_start_index)
    gate = _availability_gate(source[dependency_start : window_end_index + 1])
    return ImpulseEvent(
        direction=direction,
        window_start=window[0].start_time,
        window_end=window[-1].timestamp,
        confirmed_at=window[-1].timestamp,
        available_at=gate.available_at,
        availability_basis=gate.availability_basis,
        movement_policy=policy.movement,
        atr_method=policy.atr_method,
        atr_timing=policy.atr_timing,
        atr_period=policy.atr_period,
        minimum_directional_bars=policy.minimum_directional_bars,
        policy_fingerprint=policy.fingerprint,
        atr_snapshot_bar_end=atr.event.bar_end,
        atr_value=atr_value,
        directional_movement=movement,
        directional_bar_count=directional_count,
        largest_body=largest_body,
        movement_atr_multiple=policy.movement_atr_multiple,
        body_atr_multiple=policy.body_atr_multiple,
    )


def h1_impulse_events(
    bars: Iterable[QuoteBar],
    *,
    policy: ImpulsePolicy,
    as_of: Optional[datetime] = None
) -> Tuple[ImpulseEvent, ...]:
    """Return qualifying four-bar H1 impulse events in chronological order.

    Bullish is emitted before bearish if a future custom directional threshold
    ever allows both sides to qualify for one window.  ``as_of`` filters on the
    latest availability of the window and its exact ATR dependencies, never just
    on candle close time.
    """

    if not isinstance(policy, ImpulsePolicy):
        raise TypeError("policy must be an ImpulsePolicy")
    normalized_as_of = _normalize_as_of(as_of)
    source = tuple(bars)
    _validate_h1_bars(source)
    calculations = _atr_calculations(
        source, period=policy.atr_period, method=policy.atr_method
    )

    events = []
    last_window_start = len(source) - IMPULSE_WINDOW_BARS
    for window_start_index in range(last_window_start + 1):
        snapshot_index = _atr_snapshot_index(window_start_index, policy.atr_timing)
        if snapshot_index < 0 or snapshot_index >= len(calculations):
            continue
        atr = calculations[snapshot_index]
        if atr is None:
            continue
        for direction in (ImpulseDirection.BULLISH, ImpulseDirection.BEARISH):
            event = _impulse_event(
                source,
                window_start_index=window_start_index,
                direction=direction,
                policy=policy,
                atr=atr,
            )
            if event is not None and (
                normalized_as_of is None or event.available_at <= normalized_as_of
            ):
                events.append(event)
    return tuple(events)


def _is_opposite_origin(bar: QuoteBar, direction: ImpulseDirection) -> bool:
    if direction == ImpulseDirection.BULLISH:
        return _mid_close(bar) < _mid_open(bar)
    return _mid_close(bar) > _mid_open(bar)


def _origin_selection(
    source: Sequence[QuoteBar],
    *,
    window_start_index: int,
    direction: ImpulseDirection,
    policy: OriginSelectionPolicy,
    lookback_bars: int,
) -> Optional[_OriginSelection]:
    if policy == OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW:
        search_start = window_start_index - 1
    elif policy == OriginSelectionPolicy.LAST_OPPOSITE_AT_OR_BEFORE_WINDOW_END:
        search_start = window_start_index + IMPULSE_WINDOW_BARS - 1
    else:
        raise ValueError("origin policy is not implemented")
    earliest_index = max(0, window_start_index - lookback_bars)
    for index in range(search_start, earliest_index - 1, -1):
        if _is_opposite_origin(source[index], direction):
            return _OriginSelection(
                origin_index=index,
                examined_end_index=search_start,
            )
    return None


def _zone_event(
    impulse: ImpulseEvent,
    origin: QuoteBar,
    origin_proof_bars: Sequence[QuoteBar],
    origin_policy: OriginSelectionPolicy,
    origin_lookback_bars: int,
) -> ZoneEvent:
    if impulse.direction == ImpulseDirection.BULLISH:
        kind = ZoneKind.DEMAND
        lower_price = _mid_low(origin)
        upper_price = _mid_open(origin)
    else:
        kind = ZoneKind.SUPPLY
        lower_price = _mid_open(origin)
        upper_price = _mid_high(origin)

    # Proving that ``origin`` is the latest opposite candle also depends on
    # every more-recent candle examined by the reverse search.  The proof gate
    # wins an exact availability tie.  This only affects provenance basis; the
    # visibility timestamp remains unchanged.
    proof_gate = _availability_gate(origin_proof_bars)
    if proof_gate.available_at >= impulse.available_at:
        available_at = proof_gate.available_at
        availability_basis = proof_gate.availability_basis
    else:
        available_at = impulse.available_at
        availability_basis = impulse.availability_basis
    return ZoneEvent(
        kind=kind,
        origin_policy=origin_policy,
        origin_lookback_bars=origin_lookback_bars,
        origin_bar_start=origin.start_time,
        origin_bar_end=origin.timestamp,
        impulse_window_start=impulse.window_start,
        impulse_window_end=impulse.window_end,
        formed_at=impulse.confirmed_at,
        available_at=available_at,
        availability_basis=availability_basis,
        lower_price=lower_price,
        upper_price=upper_price,
        impulse_key=impulse.key,
    )


def supply_demand_zone_events(
    bars: Iterable[QuoteBar],
    *,
    impulse_policy: ImpulsePolicy,
    origin_policy: OriginSelectionPolicy,
    origin_lookback_bars: int,
    as_of: Optional[datetime] = None
) -> Tuple[ZoneEvent, ...]:
    """Create deterministic formation events from H1 impulses and origins.

    Demand uses the selected bearish origin's mid low-to-open interval; supply
    uses the selected bullish origin's mid open-to-high interval.  The required
    finite ``origin_lookback_bars`` is the number of bars strictly before the
    impulse window that may be searched.  The inclusive policy additionally
    searches the four impulse bars.  A missing opposite-colour origin produces
    no zone.  No retest, invalidation, freshness, overlap selection, or execution
    meaning is attached to these events.
    """

    if not isinstance(impulse_policy, ImpulsePolicy):
        raise TypeError("impulse_policy must be an ImpulsePolicy")
    _require_enum(origin_policy, OriginSelectionPolicy, "origin_policy")
    if isinstance(origin_lookback_bars, bool) or not isinstance(
        origin_lookback_bars, int
    ):
        raise TypeError("origin_lookback_bars must be an integer")
    if origin_lookback_bars < 1:
        raise ValueError("origin_lookback_bars must be at least 1")
    normalized_as_of = _normalize_as_of(as_of)
    source = tuple(bars)
    _validate_h1_bars(source)
    impulses = h1_impulse_events(source, policy=impulse_policy)
    start_indexes = {bar.start_time: index for index, bar in enumerate(source)}

    zones = []
    for impulse in impulses:
        window_start_index = start_indexes[impulse.window_start]
        origin_selection = _origin_selection(
            source,
            window_start_index=window_start_index,
            direction=impulse.direction,
            policy=origin_policy,
            lookback_bars=origin_lookback_bars,
        )
        if origin_selection is None:
            continue
        origin_index = origin_selection.origin_index
        origin_proof_bars = source[
            origin_index : origin_selection.examined_end_index + 1
        ]
        zone = _zone_event(
            impulse,
            source[origin_index],
            origin_proof_bars,
            origin_policy,
            origin_lookback_bars,
        )
        if normalized_as_of is None or zone.available_at <= normalized_as_of:
            zones.append(zone)
    return tuple(zones)


__all__ = [
    "AtrEvent",
    "AtrMethod",
    "AtrTiming",
    "IMPULSE_WINDOW_BARS",
    "ImpulseDirection",
    "ImpulseEvent",
    "ImpulseKey",
    "ImpulseMovementPolicy",
    "ImpulsePolicy",
    "OriginSelectionPolicy",
    "ZoneEvent",
    "ZoneKey",
    "ZoneKind",
    "h1_atr_events",
    "h1_impulse_events",
    "supply_demand_zone_events",
]
