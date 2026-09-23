"""Pure point-in-time lifecycle for H1 supply and demand zones.

The module turns immutable :class:`~xau_trader.supply_demand.ZoneEvent`
formations into auditable H1 invalidation and M15 retest-episode events.  It
contains no order, broker, account, backtest, sizing, or P&L behavior.

Every unresolved interpretation is selected in ``ZoneLifecyclePolicy``.  The
strategy policy intentionally has no defaults, so a caller cannot silently choose zone
freshness, timestamp precedence, overlap selection, or a price basis.
Streaming transitions accept only watermark-sealed availability groups; the
receipt collector is the explicit completeness trust boundary.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import math
from typing import Iterable, Optional, Tuple, Union

from .domain import AvailabilityBasis, QuoteBar
from .multitimeframe import H1_DURATION, M15_DURATION, MultiTimeframeDataError
from .session_calendar import SessionCalendarArtifact
from .session_timing import (
    SESSION_STRATEGY_SEMANTICS,
    next_open_start,
    open_bar_count,
    validate_calendar,
)
from .supply_demand import (
    IMPULSE_WINDOW_BARS,
    ImpulseDirection,
    OriginSelectionPolicy,
    ZoneEvent,
    ZoneKind,
)


class RetestPolicy(str, Enum):
    """Which retest-episode ordinals remain eligible for confirmation."""

    FIRST_TOUCH_ONLY = "first_touch_only"
    ALLOW_ONE_PRIOR = "allow_one_prior"


class TouchEpisodePolicy(str, Enum):
    """How individual M15 overlaps are grouped into retest episodes."""

    CONTIGUOUS_INCLUSIVE_OVERLAP = "contiguous_inclusive_overlap"


class EqualTimeOrder(str, Enum):
    """Precedence when H1 and M15 observations share availability and bar end."""

    H1_INVALIDATION_THEN_M15_TOUCH = "h1_then_m15"
    M15_TOUCH_THEN_H1_INVALIDATION = "m15_then_h1"


class OverlapSelection(str, Enum):
    """How eligible starts from several overlapping zones are returned."""

    ALL_ELIGIBLE = "all_eligible"
    NEWEST_ORIGIN_THEN_ZONE_KEY = "newest_origin_then_zone_key"


class ZonePriceBasis(str, Enum):
    """Price representation used for diagnostic zone comparisons."""

    MID = "mid"


class ObservationTimeframe(str, Enum):
    H1 = "h1"
    M15 = "m15"


class EpisodeEndReason(str, Enum):
    PRICE_LEFT_ZONE = "price_left_zone"
    ZONE_INVALIDATED = "zone_invalidated"


@dataclass(frozen=True)
class ZoneLifecyclePolicy:
    """All material choices for one immutable lifecycle run.

    Strategy choices have no defaults. ``calendar=None`` preserves strict
    contiguous history. Passing enum values, rather than their strings, makes
    accidental or misspelled policy choices fail closed.
    """

    retest_policy: RetestPolicy
    touch_episode: TouchEpisodePolicy
    equal_time_order: EqualTimeOrder
    overlap_selection: OverlapSelection
    price_basis: ZonePriceBasis
    calendar: Optional[SessionCalendarArtifact] = None

    def __post_init__(self) -> None:
        _require_enum(self.retest_policy, RetestPolicy, "retest_policy")
        _require_enum(self.touch_episode, TouchEpisodePolicy, "touch_episode")
        _require_enum(self.equal_time_order, EqualTimeOrder, "equal_time_order")
        _require_enum(self.overlap_selection, OverlapSelection, "overlap_selection")
        _require_enum(self.price_basis, ZonePriceBasis, "price_basis")
        validate_calendar(self.calendar)

    @property
    def canonical_identity(self) -> str:
        """Complete versioned identity for every lifecycle-policy choice."""

        identity = ";".join(
            (
                "zone-lifecycle-policy-v1",
                "retest_policy={}".format(self.retest_policy.value),
                "touch_episode={}".format(self.touch_episode.value),
                "equal_time_order={}".format(self.equal_time_order.value),
                "overlap_selection={}".format(self.overlap_selection.value),
                "price_basis={}".format(self.price_basis.value),
            )
        )
        if self.calendar is not None:
            identity += ";session_semantics={};calendar={}".format(
                SESSION_STRATEGY_SEMANTICS, self.calendar.fingerprint
            )
        return identity

    @property
    def fingerprint(self) -> str:
        """SHA-256 fingerprint binding every lifecycle-policy field."""

        return hashlib.sha256(self.canonical_identity.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class CompletedBarObservation:
    timeframe: ObservationTimeframe
    bar: QuoteBar

    def __post_init__(self) -> None:
        _require_enum(self.timeframe, ObservationTimeframe, "timeframe")
        if not isinstance(self.bar, QuoteBar):
            raise TypeError("bar must be a QuoteBar")
        _validate_bar(self.timeframe, self.bar)


@dataclass(frozen=True)
class SealedAvailabilityGroup:
    """Complete receipt group closed by an explicit availability watermark.

    The ingestion boundary, not the lifecycle engine, is responsible for
    collecting every observation and formation with the same ``available_at``.
    Once sealed, a later transition cannot add another member at or before
    ``sealed_through``.
    """

    available_at: datetime
    sealed_through: datetime
    observations: Tuple[CompletedBarObservation, ...]
    formations: Tuple[ZoneEvent, ...]
    calendar: Optional[SessionCalendarArtifact] = None

    def __post_init__(self) -> None:
        validate_calendar(self.calendar)
        for value, name in (
            (self.available_at, "available_at"),
            (self.sealed_through, "sealed_through"),
        ):
            if not isinstance(value, datetime):
                raise TypeError("availability group {} must be a datetime".format(name))
            if value.tzinfo != timezone.utc:
                raise ValueError(
                    "availability group {} must be normalized to UTC".format(name)
                )
        if self.sealed_through < self.available_at:
            raise ValueError("sealed_through cannot precede available_at")
        if not isinstance(self.observations, tuple):
            raise TypeError("availability group observations must be a tuple")
        if not isinstance(self.formations, tuple):
            raise TypeError("availability group formations must be a tuple")
        if not self.observations and not self.formations:
            raise ValueError("an availability group cannot be empty")
        for observation in self.observations:
            if not isinstance(observation, CompletedBarObservation):
                raise TypeError(
                    "availability group observations must be completed bars"
                )
            if observation.bar.available_at != self.available_at:
                raise ValueError(
                    "availability group observations must share available_at"
                )
            _validate_calendar_bar(self.calendar, observation.bar)
        for zone in self.formations:
            _validate_zone(zone)
            _require_same_calendar(zone.calendar, self.calendar)
            if zone.available_at != self.available_at:
                raise ValueError(
                    "availability group formations must share available_at"
                )


@dataclass(frozen=True)
class RetestEpisode:
    ordinal: int
    eligible: bool
    policy_fingerprint: str
    first_touch_bar_start: datetime
    first_touch_bar_end: datetime
    first_touch_available_at: datetime
    last_touch_bar_end: datetime
    last_touch_available_at: datetime


@dataclass(frozen=True)
class ZoneState:
    zone: ZoneEvent
    policy_fingerprint: str
    valid: bool
    retest_episode_count: int
    active_episode: Optional[RetestEpisode]
    invalidating_h1_start: Optional[datetime]
    invalidating_h1_end: Optional[datetime]
    invalidated_at: Optional[datetime]
    next_h1_start: datetime
    next_m15_start: datetime

    def __post_init__(self) -> None:
        _validate_zone(self.zone)
        _validate_policy_fingerprint(
            self.policy_fingerprint, "zone state policy_fingerprint"
        )
        if isinstance(self.retest_episode_count, bool) or not isinstance(
            self.retest_episode_count, int
        ):
            raise TypeError("retest_episode_count must be an integer")
        if self.retest_episode_count < 0:
            raise ValueError("retest_episode_count must be non-negative")
        if self.active_episode is not None:
            if not isinstance(self.active_episode, RetestEpisode):
                raise TypeError("active_episode must be a RetestEpisode or None")
            if self.active_episode.ordinal != self.retest_episode_count:
                raise ValueError("active episode ordinal must equal episode count")
            if self.active_episode.policy_fingerprint != self.policy_fingerprint:
                raise ValueError("active episode policy fingerprint conflicts with state")
        _validate_anchor(self.next_h1_start, ObservationTimeframe.H1, "next_h1_start")
        _validate_anchor(
            self.next_m15_start, ObservationTimeframe.M15, "next_m15_start"
        )
        _validate_calendar_anchor(self.zone.calendar, self.next_h1_start)
        _validate_calendar_anchor(self.zone.calendar, self.next_m15_start)
        invalidation_values = (
            self.invalidating_h1_start,
            self.invalidating_h1_end,
            self.invalidated_at,
        )
        if self.valid:
            if any(value is not None for value in invalidation_values):
                raise ValueError("a valid zone cannot have invalidation timestamps")
        else:
            if any(value is None for value in invalidation_values):
                raise ValueError("an invalid zone requires all invalidation timestamps")
            if self.active_episode is not None:
                raise ValueError("an invalid zone cannot have an active episode")


@dataclass(frozen=True)
class RetestEpisodeStarted:
    zone: ZoneEvent
    policy_fingerprint: str
    ordinal: int
    eligible: bool
    m15_bar_start: datetime
    m15_bar_end: datetime
    available_at: datetime

    @property
    def zone_key(self) -> tuple:
        return self.zone.key


@dataclass(frozen=True)
class RetestEpisodeEnded:
    zone: ZoneEvent
    policy_fingerprint: str
    ordinal: int
    reason: EpisodeEndReason
    last_touch_bar_end: datetime
    ended_by_timeframe: ObservationTimeframe
    ended_by_bar_start: datetime
    ended_by_bar_end: datetime
    available_at: datetime

    @property
    def zone_key(self) -> tuple:
        return self.zone.key


@dataclass(frozen=True)
class ZoneInvalidated:
    zone: ZoneEvent
    policy_fingerprint: str
    h1_bar_start: datetime
    h1_bar_end: datetime
    available_at: datetime
    mid_close: float
    invalidation_boundary: float

    @property
    def zone_key(self) -> tuple:
        return self.zone.key


ZoneLifecycleEvent = Union[
    RetestEpisodeStarted, RetestEpisodeEnded, ZoneInvalidated
]


@dataclass(frozen=True)
class ZoneUpdate:
    state: ZoneState
    events: Tuple[ZoneLifecycleEvent, ...]


ObservationOrderKey = Tuple[datetime, datetime, int, datetime]


@dataclass(frozen=True)
class ZoneBook:
    policy: ZoneLifecyclePolicy
    policy_fingerprint: str
    states: Tuple[ZoneState, ...]
    next_h1_start: datetime
    next_m15_start: datetime
    last_group_available_at: Optional[datetime]
    sealed_through: Optional[datetime]


@dataclass(frozen=True)
class ZoneBookUpdate:
    book: ZoneBook
    events: Tuple[ZoneLifecycleEvent, ...]
    transitions: Tuple["ZoneObservationTransition", ...]

    def __post_init__(self) -> None:
        _validate_zone_book_update(self)


@dataclass(frozen=True)
class ZoneObservationTransition:
    """One canonical H1 or M15 lifecycle step inside a sealed receipt group."""

    observation: CompletedBarObservation
    policy_fingerprint: str
    sealed_through: datetime
    states_before: Tuple[ZoneState, ...]
    states_after: Tuple[ZoneState, ...]
    events: Tuple[ZoneLifecycleEvent, ...]
    calendar: Optional[SessionCalendarArtifact] = None

    def __post_init__(self) -> None:
        validate_calendar(self.calendar)
        if not isinstance(self.observation, CompletedBarObservation):
            raise TypeError("transition observation must be a CompletedBarObservation")
        _validate_calendar_bar(self.calendar, self.observation.bar)
        _validate_policy_fingerprint(
            self.policy_fingerprint, "transition policy_fingerprint"
        )
        if not isinstance(self.sealed_through, datetime):
            raise TypeError("transition sealed_through must be a datetime")
        if self.sealed_through.tzinfo != timezone.utc:
            raise ValueError("transition sealed_through must be normalized to UTC")
        if self.sealed_through < self.observation.bar.available_at:
            raise ValueError(
                "transition watermark cannot precede observation availability"
            )
        if not isinstance(self.states_before, tuple) or not isinstance(
            self.states_after, tuple
        ):
            raise TypeError("transition states must be immutable tuples")
        if not isinstance(self.events, tuple):
            raise TypeError("transition events must be an immutable tuple")
        for states, label in (
            (self.states_before, "states_before"),
            (self.states_after, "states_after"),
        ):
            for state in states:
                if not isinstance(state, ZoneState):
                    raise TypeError(
                        "transition {} must contain ZoneState values".format(label)
                    )
                if state.policy_fingerprint != self.policy_fingerprint:
                    raise ValueError(
                        "transition state policy fingerprint conflicts with transition"
                    )
                _require_same_calendar(state.zone.calendar, self.calendar)
            if tuple(
                sorted(states, key=lambda item: _zone_sort_key(item.zone))
            ) != states:
                raise ValueError(
                    "transition {} must be in canonical zone order".format(label)
                )
            keys = tuple(state.zone.key for state in states)
            if len(set(keys)) != len(keys):
                raise ValueError(
                    "transition {} must have unique zone keys".format(label)
                )
        before_keys = tuple(state.zone.key for state in self.states_before)
        after_keys = tuple(state.zone.key for state in self.states_after)
        if before_keys != after_keys:
            raise ValueError(
                "transition snapshots must contain the same canonical zone keys"
            )
        zones_by_key = {
            state.zone.key: state.zone for state in self.states_before
        }
        for event in self.events:
            if not isinstance(
                event,
                (RetestEpisodeStarted, RetestEpisodeEnded, ZoneInvalidated),
            ):
                raise TypeError(
                    "transition events must contain ZoneLifecycleEvent values"
                )
            if event.policy_fingerprint != self.policy_fingerprint:
                raise ValueError(
                    "transition event policy fingerprint conflicts with transition"
                )
            if event.zone.key not in zones_by_key or (
                zones_by_key[event.zone.key] != event.zone
            ):
                raise ValueError(
                    "transition event zone is absent from its state snapshots"
                )
            if event.available_at != self.observation.bar.available_at:
                raise ValueError(
                    "transition event availability conflicts with observation"
                )
            if isinstance(event, RetestEpisodeStarted):
                if self.observation.timeframe != ObservationTimeframe.M15:
                    raise ValueError("retest starts require an M15 transition")
                if (
                    event.m15_bar_start != self.observation.bar.start_time
                    or event.m15_bar_end != self.observation.bar.timestamp
                ):
                    raise ValueError(
                        "retest start source conflicts with transition observation"
                    )
            elif isinstance(event, RetestEpisodeEnded):
                if event.ended_by_timeframe != self.observation.timeframe:
                    raise ValueError(
                        "retest end timeframe conflicts with transition observation"
                    )
                if (
                    event.ended_by_bar_start != self.observation.bar.start_time
                    or event.ended_by_bar_end != self.observation.bar.timestamp
                ):
                    raise ValueError(
                        "retest end source conflicts with transition observation"
                    )
            elif isinstance(event, ZoneInvalidated):
                if self.observation.timeframe != ObservationTimeframe.H1:
                    raise ValueError("zone invalidation requires an H1 transition")
                if (
                    event.h1_bar_start != self.observation.bar.start_time
                    or event.h1_bar_end != self.observation.bar.timestamp
                ):
                    raise ValueError(
                        "zone invalidation source conflicts with transition observation"
                    )


def _require_enum(value: object, enum_type: object, field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError("{} must be a {}".format(field_name, enum_type.__name__))


def _validate_policy_fingerprint(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("{} must be a lowercase SHA-256 digest".format(field_name))


def _require_same_calendar(
    left: Optional[SessionCalendarArtifact],
    right: Optional[SessionCalendarArtifact],
) -> None:
    validate_calendar(left)
    validate_calendar(right)
    if left != right:
        raise ValueError("lifecycle calendar conflicts with its bound policy or group")


def _validate_calendar_bar(
    calendar: Optional[SessionCalendarArtifact], bar: QuoteBar
) -> None:
    if calendar is not None:
        calendar.validate_bar(bar.start_time, bar.timestamp)


def _validate_calendar_anchor(
    calendar: Optional[SessionCalendarArtifact], value: datetime
) -> None:
    if calendar is not None and next_open_start(calendar, value) != value:
        raise ValueError("lifecycle cadence anchor cannot fall inside a closure")


def _next_start(
    calendar: Optional[SessionCalendarArtifact], value: datetime
) -> datetime:
    return value if calendar is None else next_open_start(calendar, value)


def _validate_zone(zone: ZoneEvent) -> None:
    if not isinstance(zone, ZoneEvent):
        raise TypeError("zone must be a ZoneEvent")
    _require_enum(zone.kind, ZoneKind, "zone kind")
    _require_enum(zone.origin_policy, OriginSelectionPolicy, "origin_policy")
    _require_enum(zone.availability_basis, AvailabilityBasis, "availability_basis")
    validate_calendar(zone.calendar)
    if isinstance(zone.origin_lookback_bars, bool) or not isinstance(
        zone.origin_lookback_bars, int
    ):
        raise TypeError("origin_lookback_bars must be an integer")
    if zone.origin_lookback_bars < 1:
        raise ValueError("origin_lookback_bars must be positive")
    for name in (
        "origin_bar_start",
        "origin_bar_end",
        "impulse_window_start",
        "impulse_window_end",
        "formed_at",
        "available_at",
    ):
        value = getattr(zone, name)
        if value.tzinfo != timezone.utc:
            raise ValueError("zone timestamps must be normalized to UTC")
    if zone.available_at < zone.formed_at:
        raise ValueError("zone available_at cannot precede formed_at")
    if zone.origin_bar_end - zone.origin_bar_start != H1_DURATION:
        raise ValueError("zone origin must span exactly one H1 bar")
    if (
        zone.origin_bar_start.minute
        or zone.origin_bar_start.second
        or zone.origin_bar_start.microsecond
    ):
        raise ValueError("zone origin must be aligned to a UTC hour")
    impulse_duration_valid = (
        zone.impulse_window_end - zone.impulse_window_start
        == H1_DURATION * IMPULSE_WINDOW_BARS
        if zone.calendar is None else open_bar_count(
            zone.calendar, zone.impulse_window_start, zone.impulse_window_end,
            H1_DURATION,
        ) == IMPULSE_WINDOW_BARS
    )
    if not impulse_duration_valid:
        raise ValueError("zone impulse window must span exactly four H1 bars")
    for value in (zone.impulse_window_start, zone.impulse_window_end):
        if value.minute or value.second or value.microsecond:
            raise ValueError("zone impulse window must be aligned to UTC hours")
    if zone.impulse_window_end != zone.formed_at:
        raise ValueError("zone impulse_window_end must equal formed_at")
    if zone.origin_bar_end > zone.impulse_window_end:
        raise ValueError("zone origin cannot end after the impulse window")
    if zone.calendar is not None:
        zone.calendar.validate_bar(zone.origin_bar_start, zone.origin_bar_end)
        zone.calendar.validate_bar(
            zone.impulse_window_start, zone.impulse_window_start + H1_DURATION
        )
        zone.calendar.validate_bar(
            zone.impulse_window_end - H1_DURATION, zone.impulse_window_end
        )
    if zone.calendar is None:
        lookback_exceeded = zone.origin_bar_start < zone.impulse_window_start - (
            H1_DURATION * zone.origin_lookback_bars
        )
    else:
        lookback_exceeded = (
            zone.origin_bar_start < zone.impulse_window_start and open_bar_count(
                zone.calendar, zone.origin_bar_start, zone.impulse_window_start,
                H1_DURATION,
            ) > zone.origin_lookback_bars
        )
    if lookback_exceeded:
        raise ValueError("zone origin exceeds its declared finite lookback")
    if (
        zone.origin_policy == OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW
        and zone.origin_bar_end > zone.impulse_window_start
    ):
        raise ValueError("before-window origin cannot overlap the impulse window")
    if not isinstance(zone.impulse_key, tuple) or len(zone.impulse_key) != 4:
        raise TypeError("zone impulse_key must be a four-field tuple")
    fingerprint, direction, window_start, window_end = zone.impulse_key
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise ValueError("zone impulse policy fingerprint must be lowercase SHA-256")
    _require_enum(direction, ImpulseDirection, "zone impulse direction")
    if (
        window_start != zone.impulse_window_start
        or window_end != zone.impulse_window_end
    ):
        raise ValueError("zone impulse_key window must match the zone window")
    expected_direction = (
        ImpulseDirection.BULLISH
        if zone.kind == ZoneKind.DEMAND
        else ImpulseDirection.BEARISH
    )
    if direction != expected_direction:
        raise ValueError("zone kind must match its impulse direction")
    for value in (zone.lower_price, zone.upper_price):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("zone prices must be numbers")
        if not math.isfinite(value) or value <= 0:
            raise ValueError("zone prices must be finite positive numbers")
    if zone.lower_price >= zone.upper_price:
        raise ValueError("zone lower_price must be below upper_price")


def _validate_bar(timeframe: ObservationTimeframe, bar: QuoteBar) -> None:
    duration = H1_DURATION if timeframe == ObservationTimeframe.H1 else M15_DURATION
    if bar.timestamp - bar.start_time != duration:
        raise MultiTimeframeDataError(
            "{} bar must span exactly {} minutes".format(
                timeframe.value.upper(), int(duration.total_seconds() // 60)
            )
        )
    if bar.start_time.tzinfo != timezone.utc or bar.timestamp.tzinfo != timezone.utc:
        raise MultiTimeframeDataError(
            "{} bars must be normalized to UTC".format(timeframe.value.upper())
        )
    minute_multiple = 60 if timeframe == ObservationTimeframe.H1 else 15
    if (
        bar.start_time.minute % minute_multiple
        or bar.start_time.second
        or bar.start_time.microsecond
    ):
        raise MultiTimeframeDataError(
            "{} bar is not aligned to its UTC boundary".format(timeframe.value.upper())
        )


def _validate_anchor(
    value: datetime, timeframe: ObservationTimeframe, field_name: str
) -> None:
    if not isinstance(value, datetime):
        raise TypeError("{} must be a datetime".format(field_name))
    if value.tzinfo != timezone.utc:
        raise ValueError("{} must be normalized to UTC".format(field_name))
    minute_multiple = 60 if timeframe == ObservationTimeframe.H1 else 15
    if value.minute % minute_multiple or value.second or value.microsecond:
        raise ValueError(
            "{} must be aligned to a UTC {} boundary".format(
                field_name, timeframe.value.upper()
            )
        )


def _first_full_bar_start(
    available_at: datetime,
    timeframe: ObservationTimeframe,
    calendar: Optional[SessionCalendarArtifact] = None,
) -> datetime:
    """Earliest aligned bar start at or after an event became visible.

    Equality is inclusive: a zone visible exactly at a boundary may use the bar
    beginning at that boundary.  A zone visible inside a bar waits for the next
    boundary, so no partial pre-visibility OHLC can affect its lifecycle.
    When receipts outlive the finite calendar, its timeframe-floored end is an
    exhausted sentinel, not an inferred future open. A complete bar beginning
    there cannot fit in coverage (including a calendar with a partial tail).
    Actual availability is never changed: the source-bar visibility guard and
    calendar interval validation still apply before any lifecycle effect.
    """

    if timeframe == ObservationTimeframe.H1:
        floor = available_at.replace(minute=0, second=0, microsecond=0)
        duration = H1_DURATION
    else:
        floor = available_at.replace(
            minute=(available_at.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        duration = M15_DURATION
    aligned = floor if floor == available_at else floor + duration
    if calendar is not None and aligned > calendar.coverage_end:
        end = calendar.coverage_end
        minute = 0 if timeframe == ObservationTimeframe.H1 else (end.minute // 15) * 15
        exhausted = end.replace(minute=minute, second=0, microsecond=0)
        _validate_calendar_anchor(calendar, exhausted)
        return exhausted
    return _next_start(calendar, aligned)


def _zone_sort_key(zone: ZoneEvent) -> tuple:
    fingerprint, direction, window_start, window_end = zone.impulse_key
    return (
        zone.kind.value,
        zone.origin_policy.value,
        zone.origin_lookback_bars,
        zone.origin_bar_start,
        fingerprint,
        direction.value,
        window_start,
        window_end,
    )


def _mid_high(bar: QuoteBar) -> float:
    return (bar.bid_high + bar.ask_high) / 2.0


def _mid_low(bar: QuoteBar) -> float:
    return (bar.bid_low + bar.ask_low) / 2.0


def _mid_close(bar: QuoteBar) -> float:
    return (bar.bid_close + bar.ask_close) / 2.0


def _episode_is_eligible(ordinal: int, policy: RetestPolicy) -> bool:
    if policy == RetestPolicy.FIRST_TOUCH_ONLY:
        return ordinal == 1
    if policy == RetestPolicy.ALLOW_ONE_PRIOR:
        return ordinal <= 2
    raise ValueError("retest_policy is not implemented")


def initial_zone_state(
    zone: ZoneEvent,
    policy: ZoneLifecyclePolicy,
    *,
    h1_start: datetime,
    m15_start: datetime,
) -> ZoneState:
    """Return a valid, untouched lifecycle state for one formation event."""

    _validate_zone(zone)
    if not isinstance(policy, ZoneLifecyclePolicy):
        raise TypeError("policy must be a ZoneLifecyclePolicy")
    _require_same_calendar(zone.calendar, policy.calendar)
    _validate_anchor(h1_start, ObservationTimeframe.H1, "h1_start")
    _validate_anchor(m15_start, ObservationTimeframe.M15, "m15_start")
    return ZoneState(
        zone=zone,
        policy_fingerprint=policy.fingerprint,
        valid=True,
        retest_episode_count=0,
        active_episode=None,
        invalidating_h1_start=None,
        invalidating_h1_end=None,
        invalidated_at=None,
        next_h1_start=_first_full_bar_start(
            max(zone.available_at, h1_start), ObservationTimeframe.H1, policy.calendar
        ),
        next_m15_start=_first_full_bar_start(
            max(zone.available_at, m15_start), ObservationTimeframe.M15, policy.calendar
        ),
    )


def _apply_h1_close(
    state: ZoneState, bar: QuoteBar, policy: ZoneLifecyclePolicy
) -> ZoneUpdate:
    """Apply H1 after the owning ``ZoneBook`` has checked cadence."""

    if not isinstance(state, ZoneState):
        raise TypeError("state must be a ZoneState")
    if not isinstance(policy, ZoneLifecyclePolicy):
        raise TypeError("policy must be a ZoneLifecyclePolicy")
    _validate_bar(ObservationTimeframe.H1, bar)
    _require_same_calendar(state.zone.calendar, policy.calendar)
    _validate_calendar_bar(policy.calendar, bar)
    zone = state.zone
    if bar.start_time < state.next_h1_start:
        return ZoneUpdate(state=state, events=())
    if bar.start_time > state.next_h1_start:
        raise MultiTimeframeDataError("zone H1 cadence omitted a full bar")
    advanced = replace(state, next_h1_start=_next_start(policy.calendar, bar.timestamp))
    if not state.valid or bar.start_time < zone.available_at:
        return ZoneUpdate(state=advanced, events=())

    close = _mid_close(bar)
    if zone.kind == ZoneKind.DEMAND:
        boundary = zone.lower_price
        invalidated = close < boundary
    elif zone.kind == ZoneKind.SUPPLY:
        boundary = zone.upper_price
        invalidated = close > boundary
    else:
        raise ValueError("zone kind is not implemented")
    if not invalidated:
        return ZoneUpdate(state=advanced, events=())

    invalidation = ZoneInvalidated(
        zone=zone,
        policy_fingerprint=policy.fingerprint,
        h1_bar_start=bar.start_time,
        h1_bar_end=bar.timestamp,
        available_at=bar.available_at,
        mid_close=close,
        invalidation_boundary=boundary,
    )
    events = [invalidation]  # type: list
    if advanced.active_episode is not None:
        episode = advanced.active_episode
        events.append(
            RetestEpisodeEnded(
                zone=zone,
                policy_fingerprint=policy.fingerprint,
                ordinal=episode.ordinal,
                reason=EpisodeEndReason.ZONE_INVALIDATED,
                last_touch_bar_end=episode.last_touch_bar_end,
                ended_by_timeframe=ObservationTimeframe.H1,
                ended_by_bar_start=bar.start_time,
                ended_by_bar_end=bar.timestamp,
                available_at=bar.available_at,
            )
        )
    return ZoneUpdate(
        state=ZoneState(
            zone=zone,
            policy_fingerprint=state.policy_fingerprint,
            valid=False,
            retest_episode_count=state.retest_episode_count,
            active_episode=None,
            invalidating_h1_start=bar.start_time,
            invalidating_h1_end=bar.timestamp,
            invalidated_at=bar.available_at,
            next_h1_start=advanced.next_h1_start,
            next_m15_start=advanced.next_m15_start,
        ),
        events=tuple(events),
    )


def _apply_m15_close(
    state: ZoneState, bar: QuoteBar, policy: ZoneLifecyclePolicy
) -> ZoneUpdate:
    """Apply M15 after the owning ``ZoneBook`` has checked cadence."""

    if not isinstance(state, ZoneState):
        raise TypeError("state must be a ZoneState")
    if not isinstance(policy, ZoneLifecyclePolicy):
        raise TypeError("policy must be a ZoneLifecyclePolicy")
    _validate_bar(ObservationTimeframe.M15, bar)
    _require_same_calendar(state.zone.calendar, policy.calendar)
    _validate_calendar_bar(policy.calendar, bar)
    zone = state.zone
    if bar.start_time < state.next_m15_start:
        return ZoneUpdate(state=state, events=())
    if bar.start_time > state.next_m15_start:
        raise MultiTimeframeDataError("zone M15 cadence omitted a full bar")
    advanced = replace(state, next_m15_start=_next_start(policy.calendar, bar.timestamp))
    if not state.valid or bar.start_time < zone.available_at:
        return ZoneUpdate(state=advanced, events=())

    overlaps = _mid_low(bar) <= zone.upper_price and _mid_high(bar) >= zone.lower_price
    active = advanced.active_episode
    if overlaps and active is None:
        ordinal = advanced.retest_episode_count + 1
        eligible = _episode_is_eligible(ordinal, policy.retest_policy)
        episode = RetestEpisode(
            ordinal=ordinal,
            eligible=eligible,
            policy_fingerprint=policy.fingerprint,
            first_touch_bar_start=bar.start_time,
            first_touch_bar_end=bar.timestamp,
            first_touch_available_at=bar.available_at,
            last_touch_bar_end=bar.timestamp,
            last_touch_available_at=bar.available_at,
        )
        return ZoneUpdate(
            state=replace(
                advanced,
                retest_episode_count=ordinal,
                active_episode=episode,
            ),
            events=(
                RetestEpisodeStarted(
                    zone=zone,
                    policy_fingerprint=policy.fingerprint,
                    ordinal=ordinal,
                    eligible=eligible,
                    m15_bar_start=bar.start_time,
                    m15_bar_end=bar.timestamp,
                    available_at=bar.available_at,
                ),
            ),
        )
    if overlaps and active is not None:
        return ZoneUpdate(
            state=replace(
                advanced,
                active_episode=replace(
                    active,
                    last_touch_bar_end=bar.timestamp,
                    last_touch_available_at=bar.available_at,
                ),
            ),
            events=(),
        )
    if not overlaps and active is not None:
        return ZoneUpdate(
            state=replace(advanced, active_episode=None),
            events=(
                RetestEpisodeEnded(
                    zone=zone,
                    policy_fingerprint=policy.fingerprint,
                    ordinal=active.ordinal,
                    reason=EpisodeEndReason.PRICE_LEFT_ZONE,
                    last_touch_bar_end=active.last_touch_bar_end,
                    ended_by_timeframe=ObservationTimeframe.M15,
                    ended_by_bar_start=bar.start_time,
                    ended_by_bar_end=bar.timestamp,
                    available_at=bar.available_at,
                ),
            ),
        )
    return ZoneUpdate(state=advanced, events=())


def observation_order_key(
    observation: CompletedBarObservation, policy: ZoneLifecyclePolicy
) -> ObservationOrderKey:
    """Return the explicit point-in-time ordering key for an observation."""

    if not isinstance(observation, CompletedBarObservation):
        raise TypeError("observation must be a CompletedBarObservation")
    if not isinstance(policy, ZoneLifecyclePolicy):
        raise TypeError("policy must be a ZoneLifecyclePolicy")
    if policy.equal_time_order == EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH:
        priority = 0 if observation.timeframe == ObservationTimeframe.H1 else 1
    elif policy.equal_time_order == EqualTimeOrder.M15_TOUCH_THEN_H1_INVALIDATION:
        priority = 0 if observation.timeframe == ObservationTimeframe.M15 else 1
    else:
        raise ValueError("equal_time_order is not implemented")
    return (
        observation.bar.available_at,
        observation.bar.timestamp,
        priority,
        observation.bar.start_time,
    )


def order_observations(
    observations: Iterable[CompletedBarObservation], policy: ZoneLifecyclePolicy
) -> Tuple[CompletedBarObservation, ...]:
    """Explicitly canonicalize an offline observation batch.

    Streaming callers must collect a complete availability-time group and seal
    it with :func:`seal_availability_group` before advancing the book.
    """

    source = tuple(observations)
    return tuple(sorted(source, key=lambda item: observation_order_key(item, policy)))


def seal_availability_group(
    observations: Iterable[CompletedBarObservation],
    formations: Iterable[ZoneEvent],
    *,
    sealed_through: datetime,
    calendar: Optional[SessionCalendarArtifact] = None,
) -> SealedAvailabilityGroup:
    """Close one complete same-availability receipt group at a watermark."""

    source_observations = tuple(observations)
    source_formations = tuple(formations)
    if any(
        not isinstance(observation, CompletedBarObservation)
        for observation in source_observations
    ):
        raise TypeError("observations must contain CompletedBarObservation values")
    for zone in source_formations:
        _validate_zone(zone)
    availability_values = tuple(
        observation.bar.available_at for observation in source_observations
    ) + tuple(zone.available_at for zone in source_formations)
    if not availability_values:
        raise ValueError("an availability group cannot be empty")
    available_at = availability_values[0]
    if any(value != available_at for value in availability_values[1:]):
        raise MultiTimeframeDataError(
            "availability group members must share one available_at"
        )
    return SealedAvailabilityGroup(
        available_at=available_at,
        sealed_through=sealed_through,
        observations=source_observations,
        formations=source_formations,
        calendar=calendar,
    )


def _validate_state(state: ZoneState, policy: ZoneLifecyclePolicy) -> None:
    if not isinstance(state, ZoneState):
        raise TypeError("book states must contain ZoneState values")
    _validate_zone(state.zone)
    _require_same_calendar(state.zone.calendar, policy.calendar)
    _validate_policy_fingerprint(
        state.policy_fingerprint, "zone state policy_fingerprint"
    )
    if state.policy_fingerprint != policy.fingerprint:
        raise ValueError("zone state policy fingerprint conflicts with book policy")
    if type(state.valid) is not bool:
        raise TypeError("zone state valid must be a bool")
    if isinstance(state.retest_episode_count, bool) or not isinstance(
        state.retest_episode_count, int
    ):
        raise TypeError("retest_episode_count must be an integer")
    if state.retest_episode_count < 0:
        raise ValueError("retest_episode_count must be non-negative")
    _validate_anchor(state.next_h1_start, ObservationTimeframe.H1, "next_h1_start")
    _validate_anchor(
        state.next_m15_start, ObservationTimeframe.M15, "next_m15_start"
    )
    _validate_calendar_anchor(policy.calendar, state.next_h1_start)
    _validate_calendar_anchor(policy.calendar, state.next_m15_start)
    if state.next_h1_start < _first_full_bar_start(
        state.zone.available_at, ObservationTimeframe.H1, policy.calendar
    ):
        raise ValueError("zone H1 anchor precedes zone visibility")
    if state.next_m15_start < _first_full_bar_start(
        state.zone.available_at, ObservationTimeframe.M15, policy.calendar
    ):
        raise ValueError("zone M15 anchor precedes zone visibility")
    if state.active_episode is not None:
        episode = state.active_episode
        if not isinstance(episode, RetestEpisode):
            raise TypeError("active_episode must be a RetestEpisode")
        if episode.ordinal != state.retest_episode_count or episode.ordinal < 1:
            raise ValueError("active episode ordinal must equal the positive count")
        if type(episode.eligible) is not bool:
            raise TypeError("episode eligible must be a bool")
        _validate_policy_fingerprint(
            episode.policy_fingerprint, "active episode policy_fingerprint"
        )
        if episode.policy_fingerprint != policy.fingerprint:
            raise ValueError("active episode policy fingerprint conflicts with policy")
        if episode.eligible != _episode_is_eligible(
            episode.ordinal, policy.retest_policy
        ):
            raise ValueError("active episode eligibility conflicts with policy")
        _validate_anchor(
            episode.first_touch_bar_start,
            ObservationTimeframe.M15,
            "first_touch_bar_start",
        )
        if episode.first_touch_bar_end - episode.first_touch_bar_start != M15_DURATION:
            raise ValueError("active episode first touch must span one M15 bar")
        if episode.first_touch_available_at < episode.first_touch_bar_end:
            raise ValueError("active episode cannot be visible before its touch bar ends")
        if episode.last_touch_bar_end < episode.first_touch_bar_end:
            raise ValueError("active episode last touch cannot precede its first touch")
        if episode.last_touch_available_at < episode.last_touch_bar_end:
            raise ValueError("active episode last touch availability is backdated")
        if policy.calendar is not None:
            policy.calendar.validate_bar(
                episode.first_touch_bar_start, episode.first_touch_bar_end
            )
            policy.calendar.validate_bar(
                episode.last_touch_bar_end - M15_DURATION, episode.last_touch_bar_end
            )
    invalidation = (
        state.invalidating_h1_start,
        state.invalidating_h1_end,
        state.invalidated_at,
    )
    if state.valid:
        if any(value is not None for value in invalidation):
            raise ValueError("valid state cannot contain invalidation timestamps")
    else:
        if any(value is None for value in invalidation):
            raise ValueError("invalid state requires all invalidation timestamps")
        if state.active_episode is not None:
            raise ValueError("invalid state cannot contain an active episode")
        if state.invalidating_h1_end - state.invalidating_h1_start != H1_DURATION:
            raise ValueError("invalidation source must span one H1 bar")
        if state.invalidated_at < state.invalidating_h1_end:
            raise ValueError("zone invalidation availability is backdated")
        if policy.calendar is not None:
            policy.calendar.validate_bar(
                state.invalidating_h1_start, state.invalidating_h1_end
            )


def _validate_book(book: ZoneBook) -> None:
    if not isinstance(book, ZoneBook):
        raise TypeError("book must be a ZoneBook")
    if not isinstance(book.policy, ZoneLifecyclePolicy):
        raise TypeError("book policy must be a ZoneLifecyclePolicy")
    _validate_policy_fingerprint(
        book.policy_fingerprint, "book policy_fingerprint"
    )
    if book.policy_fingerprint != book.policy.fingerprint:
        raise ValueError("book policy fingerprint conflicts with policy")
    if not isinstance(book.states, tuple):
        raise TypeError("book states must be an immutable tuple")
    _validate_anchor(book.next_h1_start, ObservationTimeframe.H1, "next_h1_start")
    _validate_anchor(
        book.next_m15_start, ObservationTimeframe.M15, "next_m15_start"
    )
    _validate_calendar_anchor(book.policy.calendar, book.next_h1_start)
    _validate_calendar_anchor(book.policy.calendar, book.next_m15_start)
    if book.last_group_available_at is not None:
        if (
            not isinstance(book.last_group_available_at, datetime)
            or book.last_group_available_at.tzinfo != timezone.utc
        ):
            raise ValueError("last_group_available_at must be normalized to UTC")
    if book.sealed_through is not None:
        if (
            not isinstance(book.sealed_through, datetime)
            or book.sealed_through.tzinfo != timezone.utc
        ):
            raise ValueError("sealed_through must be normalized to UTC")
    if (book.last_group_available_at is None) != (book.sealed_through is None):
        raise ValueError("book group time and watermark must be present together")
    if (
        book.last_group_available_at is not None
        and book.sealed_through < book.last_group_available_at
    ):
        raise ValueError("book watermark cannot precede its last availability group")
    for state in book.states:
        _validate_state(state, book.policy)
        expected_h1 = max(
            book.next_h1_start,
            _first_full_bar_start(
                state.zone.available_at, ObservationTimeframe.H1, book.policy.calendar
            ),
        )
        expected_m15 = max(
            book.next_m15_start,
            _first_full_bar_start(
                state.zone.available_at, ObservationTimeframe.M15, book.policy.calendar
            ),
        )
        if state.next_h1_start != expected_h1:
            raise ValueError("zone H1 anchor conflicts with the book cadence")
        if state.next_m15_start != expected_m15:
            raise ValueError("zone M15 anchor conflicts with the book cadence")
    if tuple(sorted(book.states, key=lambda item: _zone_sort_key(item.zone))) != book.states:
        raise ValueError("book states must be in canonical zone order")
    keys = tuple(state.zone.key for state in book.states)
    if len(set(keys)) != len(keys):
        raise ValueError("book states must have unique zone keys")


def _validate_transition_against_policy(
    transition: ZoneObservationTransition,
    policy: ZoneLifecyclePolicy,
) -> None:
    """Replay one published snapshot and require an exact policy result."""

    if not isinstance(transition, ZoneObservationTransition):
        raise TypeError(
            "book update transitions must contain ZoneObservationTransition values"
        )
    if transition.policy_fingerprint != policy.fingerprint:
        raise ValueError(
            "transition policy fingerprint conflicts with the book policy"
        )
    _require_same_calendar(transition.calendar, policy.calendar)
    for state in transition.states_before:
        _validate_state(state, policy)
    for state in transition.states_after:
        _validate_state(state, policy)

    updater = (
        _apply_h1_close
        if transition.observation.timeframe == ObservationTimeframe.H1
        else _apply_m15_close
    )
    expected_states = []  # type: list
    expected_events = []  # type: list
    for state in transition.states_before:
        update = updater(state, transition.observation.bar, policy)
        expected_states.append(update.state)
        expected_events.extend(update.events)
    if transition.states_after != tuple(expected_states):
        raise ValueError(
            "transition states_after is not the exact policy replay result"
        )
    if transition.events != tuple(expected_events):
        raise ValueError("transition events are not the exact policy replay result")


def _validate_snapshot_continuity(
    previous: Tuple[ZoneState, ...],
    current: Tuple[ZoneState, ...],
    *,
    field_name: str,
) -> None:
    """Require prior zone snapshots to persist exactly across formation-only gaps."""

    current_by_key = {state.zone.key: state for state in current}
    for state in previous:
        if state.zone.key not in current_by_key:
            raise ValueError("{} cannot remove an existing zone".format(field_name))
        if current_by_key[state.zone.key] != state:
            raise ValueError(
                "{} changed without an observation transition".format(field_name)
            )


def _validate_zone_book_update(update: ZoneBookUpdate) -> None:
    """Validate an immutable transition ledger against its final book."""

    if not isinstance(update.book, ZoneBook):
        raise TypeError("book update book must be a ZoneBook")
    _validate_book(update.book)
    if not isinstance(update.events, tuple):
        raise TypeError("book update events must be an immutable tuple")
    if not isinstance(update.transitions, tuple):
        raise TypeError("book update transitions must be an immutable tuple")

    flattened_events = []  # type: list
    previous_transition = None  # type: Optional[ZoneObservationTransition]
    previous_order_key = None  # type: Optional[ObservationOrderKey]
    calendar_next_starts = {}
    for transition in update.transitions:
        _validate_transition_against_policy(transition, update.book.policy)
        if update.book.policy.calendar is not None:
            timeframe = transition.observation.timeframe
            bar = transition.observation.bar
            if (
                timeframe in calendar_next_starts
                and bar.start_time != calendar_next_starts[timeframe]
            ):
                raise ValueError("transition ledger omitted an open bar or repeated a bar")
            calendar_next_starts[timeframe] = _next_start(
                update.book.policy.calendar, bar.timestamp
            )
        flattened_events.extend(transition.events)
        current_order_key = observation_order_key(
            transition.observation, update.book.policy
        )
        if previous_order_key is not None and current_order_key <= previous_order_key:
            raise ValueError(
                "book update transitions must follow canonical observation order"
            )
        if previous_transition is not None:
            if transition.sealed_through < previous_transition.sealed_through:
                raise ValueError("transition watermarks must be non-decreasing")
            if transition.sealed_through == previous_transition.sealed_through:
                if (
                    transition.observation.bar.available_at
                    != previous_transition.observation.bar.available_at
                ):
                    raise ValueError(
                        "one transition watermark cannot seal different "
                        "availability groups"
                    )
            elif (
                transition.observation.bar.available_at
                <= previous_transition.sealed_through
            ):
                raise ValueError(
                    "a later transition is at or before the prior receipt watermark"
                )
            _validate_snapshot_continuity(
                previous_transition.states_after,
                transition.states_before,
                field_name="transition snapshot continuity",
            )
        previous_transition = transition
        previous_order_key = current_order_key

    for timeframe, expected_start in calendar_next_starts.items():
        actual_start = (
            update.book.next_h1_start
            if timeframe == ObservationTimeframe.H1 else update.book.next_m15_start
        )
        if actual_start != expected_start:
            raise ValueError("final book cadence conflicts with its transition ledger")

    if update.events != tuple(flattened_events):
        raise ValueError(
            "book update events must exactly aggregate transition events in order"
        )
    if previous_transition is not None:
        if update.book.sealed_through is None:
            raise ValueError("a transition ledger requires a final book watermark")
        if previous_transition.sealed_through > update.book.sealed_through:
            raise ValueError("transition watermark exceeds the final book watermark")
        if (
            update.book.last_group_available_at is None
            or previous_transition.observation.bar.available_at
            > update.book.last_group_available_at
        ):
            raise ValueError(
                "transition availability exceeds the final book group time"
            )
        _validate_snapshot_continuity(
            previous_transition.states_after,
            update.book.states,
            field_name="final book snapshot continuity",
        )


def new_zone_book(
    zones: Iterable[ZoneEvent],
    policy: ZoneLifecyclePolicy,
    *,
    h1_start: datetime,
    m15_start: datetime,
) -> ZoneBook:
    """Create a book at explicit stream anchors.

    Initial zones must already be visible at both anchors.  Later formations are
    admitted only through :func:`advance_zone_book_group` at their actual
    ``available_at`` time.
    """

    if not isinstance(policy, ZoneLifecyclePolicy):
        raise TypeError("policy must be a ZoneLifecyclePolicy")
    _validate_anchor(h1_start, ObservationTimeframe.H1, "h1_start")
    _validate_anchor(m15_start, ObservationTimeframe.M15, "m15_start")
    source = tuple(zones)
    h1_start = _next_start(policy.calendar, h1_start)
    m15_start = _next_start(policy.calendar, m15_start)
    admission_cutoff = min(h1_start, m15_start)
    for zone in source:
        _validate_zone(zone)
        _require_same_calendar(zone.calendar, policy.calendar)
        if zone.available_at > admission_cutoff:
            raise ValueError(
                "future zone must be admitted at its availability group"
            )
    ordered = tuple(sorted(source, key=_zone_sort_key))
    if len({zone.key for zone in ordered}) != len(ordered):
        raise ValueError("zones must have unique formation keys")
    book = ZoneBook(
        policy=policy,
        policy_fingerprint=policy.fingerprint,
        states=tuple(
            initial_zone_state(
                zone,
                policy,
                h1_start=h1_start,
                m15_start=m15_start,
            )
            for zone in ordered
        ),
        next_h1_start=h1_start,
        next_m15_start=m15_start,
        last_group_available_at=None,
        sealed_through=None,
    )
    _validate_book(book)
    return book


def advance_zone_book_group(
    book: ZoneBook,
    group: SealedAvailabilityGroup,
) -> ZoneBookUpdate:
    """Atomically apply one complete availability-time receipt group.

    The sealed group is the receipt collector's watermark assertion that no
    omitted peer can arrive at or before ``sealed_through``.  The function
    dynamically admits formations, canonicalizes H1/M15 ties, and only then
    returns events.  Caller iteration order therefore cannot change equal-time
    precedence or expose a partial transition.
    """

    _validate_book(book)
    if not isinstance(group, SealedAvailabilityGroup):
        raise TypeError("group must be a SealedAvailabilityGroup")
    _require_same_calendar(group.calendar, book.policy.calendar)
    source_observations = group.observations
    source_formations = group.formations
    for observation in source_observations:
        if not isinstance(observation, CompletedBarObservation):
            raise TypeError("observations must contain CompletedBarObservation values")
        _require_enum(observation.timeframe, ObservationTimeframe, "timeframe")
        if not isinstance(observation.bar, QuoteBar):
            raise TypeError("observation bar must be a QuoteBar")
        _validate_bar(observation.timeframe, observation.bar)
        _validate_calendar_bar(book.policy.calendar, observation.bar)
    for zone in source_formations:
        _validate_zone(zone)
        _require_same_calendar(zone.calendar, book.policy.calendar)
    group_at = group.available_at
    if (
        book.sealed_through is not None
        and group_at <= book.sealed_through
    ):
        raise MultiTimeframeDataError(
            "availability group is at or before the prior receipt watermark"
        )

    existing_keys = {state.zone.key for state in book.states}
    incoming_keys = tuple(zone.key for zone in source_formations)
    if len(set(incoming_keys)) != len(incoming_keys) or any(
        key in existing_keys for key in incoming_keys
    ):
        raise ValueError("zone formation keys must be unique across the book")

    ordered_observations = order_observations(source_observations, book.policy)
    next_h1 = book.next_h1_start
    next_m15 = book.next_m15_start
    seen_bars = set()
    for observation in ordered_observations:
        identity = (
            observation.timeframe,
            observation.bar.start_time,
            observation.bar.timestamp,
        )
        if identity in seen_bars:
            raise MultiTimeframeDataError("availability group contains a duplicate bar")
        seen_bars.add(identity)
        if observation.timeframe == ObservationTimeframe.H1:
            if observation.bar.start_time != next_h1:
                raise MultiTimeframeDataError(
                    "H1 observation does not match the explicit cadence anchor"
                )
            next_h1 = _next_start(book.policy.calendar, observation.bar.timestamp)
        else:
            if observation.bar.start_time != next_m15:
                raise MultiTimeframeDataError(
                    "M15 observation does not match the explicit cadence anchor"
                )
            next_m15 = _next_start(book.policy.calendar, observation.bar.timestamp)

    states = list(book.states)
    for zone in sorted(source_formations, key=_zone_sort_key):
        states.append(
            initial_zone_state(
                zone,
                book.policy,
                h1_start=book.next_h1_start,
                m15_start=book.next_m15_start,
            )
        )
    states.sort(key=lambda item: _zone_sort_key(item.zone))
    events = []  # type: list
    transitions = []  # type: list
    for observation in ordered_observations:
        updater = (
            _apply_h1_close
            if observation.timeframe == ObservationTimeframe.H1
            else _apply_m15_close
        )
        states_before = tuple(states)
        updated_states = []
        step_events = []  # type: list
        for state in states:
            update = updater(state, observation.bar, book.policy)
            updated_states.append(update.state)
            step_events.extend(update.events)
        states = updated_states
        events.extend(step_events)
        transitions.append(
            ZoneObservationTransition(
                observation=observation,
                policy_fingerprint=book.policy_fingerprint,
                sealed_through=group.sealed_through,
                states_before=states_before,
                states_after=tuple(states),
                events=tuple(step_events),
                calendar=book.policy.calendar,
            )
        )
    result = ZoneBook(
        policy=book.policy,
        policy_fingerprint=book.policy_fingerprint,
        states=tuple(states),
        next_h1_start=next_h1,
        next_m15_start=next_m15,
        last_group_available_at=group_at,
        sealed_through=group.sealed_through,
    )
    _validate_book(result)
    return ZoneBookUpdate(
        book=result,
        events=tuple(events),
        transitions=tuple(transitions),
    )

def evolve_zone_book(
    zones: Iterable[ZoneEvent],
    observations: Iterable[CompletedBarObservation],
    policy: ZoneLifecyclePolicy,
    *,
    h1_start: datetime,
    m15_start: datetime,
) -> ZoneBookUpdate:
    """Dynamically admit formations and atomically fold a complete batch."""

    source_zones = tuple(zones)
    source_observations = tuple(observations)
    book = new_zone_book((), policy, h1_start=h1_start, m15_start=m15_start)
    groups = {}  # type: ignore
    for zone in source_zones:
        _validate_zone(zone)
        group = groups.setdefault(zone.available_at, [[], []])
        group[1].append(zone)
    for observation in source_observations:
        if not isinstance(observation, CompletedBarObservation):
            raise TypeError("observations must contain CompletedBarObservation values")
        group = groups.setdefault(observation.bar.available_at, [[], []])
        group[0].append(observation)
    events = []  # type: list
    transitions = []  # type: list
    for available_at in sorted(groups):
        group_observations, group_formations = groups[available_at]
        update = advance_zone_book_group(
            book,
            seal_availability_group(
                tuple(group_observations),
                tuple(group_formations),
                sealed_through=available_at,
                calendar=policy.calendar,
            ),
        )
        book = update.book
        events.extend(update.events)
        transitions.extend(update.transitions)
    return ZoneBookUpdate(
        book=book,
        events=tuple(events),
        transitions=tuple(transitions),
    )


def select_retest_starts(
    starts: Iterable[RetestEpisodeStarted],
    *,
    kind: ZoneKind,
    policy: ZoneLifecyclePolicy,
) -> Tuple[RetestEpisodeStarted, ...]:
    """Select eligible same-bar starts using the lifecycle's bound policy."""

    _require_enum(kind, ZoneKind, "kind")
    if not isinstance(policy, ZoneLifecyclePolicy):
        raise TypeError("policy must be a ZoneLifecyclePolicy")
    selection = policy.overlap_selection
    source = tuple(starts)
    if any(not isinstance(event, RetestEpisodeStarted) for event in source):
        raise TypeError("starts must contain RetestEpisodeStarted values")
    for event in source:
        _validate_zone(event.zone)
        _require_same_calendar(event.zone.calendar, policy.calendar)
        _validate_policy_fingerprint(
            event.policy_fingerprint, "retest start policy_fingerprint"
        )
        if event.policy_fingerprint != policy.fingerprint:
            raise ValueError("retest start policy fingerprint conflicts with policy")
        if isinstance(event.ordinal, bool) or not isinstance(event.ordinal, int):
            raise TypeError("retest start ordinal must be an integer")
        if event.ordinal < 1:
            raise ValueError("retest start ordinal must be positive")
        if type(event.eligible) is not bool:
            raise TypeError("retest start eligible must be a bool")
        expected_eligible = _episode_is_eligible(
            event.ordinal, policy.retest_policy
        )
        if event.eligible != expected_eligible:
            raise ValueError("retest start eligibility conflicts with policy")
        _validate_anchor(
            event.m15_bar_start,
            ObservationTimeframe.M15,
            "retest start m15_bar_start",
        )
        if event.m15_bar_end - event.m15_bar_start != M15_DURATION:
            raise ValueError("retest start must span one M15 bar")
        if event.available_at < event.m15_bar_end:
            raise ValueError("retest start availability cannot be backdated")
        if policy.calendar is not None:
            policy.calendar.validate_bar(event.m15_bar_start, event.m15_bar_end)
    if source:
        source_bar = (
            source[0].m15_bar_start,
            source[0].m15_bar_end,
            source[0].available_at,
        )
        if any(
            (event.m15_bar_start, event.m15_bar_end, event.available_at) != source_bar
            for event in source[1:]
        ):
            raise ValueError("retest starts must come from one M15 observation")
    identities = tuple(
        (
            event.zone.key,
            event.policy_fingerprint,
            event.ordinal,
            event.m15_bar_start,
            event.m15_bar_end,
            event.available_at,
        )
        for event in source
    )
    if len(set(identities)) != len(identities):
        raise ValueError("retest starts must be unique")
    eligible = tuple(
        event for event in source if event.zone.kind == kind and event.eligible
    )
    if selection == OverlapSelection.ALL_ELIGIBLE:
        return tuple(sorted(eligible, key=lambda event: _zone_sort_key(event.zone)))
    if selection == OverlapSelection.NEWEST_ORIGIN_THEN_ZONE_KEY:
        if not eligible:
            return ()
        newest = max(event.zone.origin_bar_end for event in eligible)
        tied = tuple(event for event in eligible if event.zone.origin_bar_end == newest)
        return (min(tied, key=lambda event: _zone_sort_key(event.zone)),)
    raise ValueError("overlap_selection is not implemented")


__all__ = [
    "CompletedBarObservation",
    "EpisodeEndReason",
    "EqualTimeOrder",
    "ObservationTimeframe",
    "OverlapSelection",
    "RetestEpisode",
    "RetestEpisodeEnded",
    "RetestEpisodeStarted",
    "RetestPolicy",
    "SealedAvailabilityGroup",
    "TouchEpisodePolicy",
    "ZoneBook",
    "ZoneBookUpdate",
    "ZoneInvalidated",
    "ZoneLifecycleEvent",
    "ZoneLifecyclePolicy",
    "ZoneObservationTransition",
    "ZonePriceBasis",
    "ZoneState",
    "advance_zone_book_group",
    "evolve_zone_book",
    "new_zone_book",
    "observation_order_key",
    "order_observations",
    "seal_availability_group",
    "select_retest_starts",
]
