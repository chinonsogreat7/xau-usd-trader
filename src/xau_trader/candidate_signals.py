"""Auditable paper-only XAU/USD candidate decisions.

This module joins three immutable research streams: exact per-observation zone
lifecycle snapshots, confirmed H1 structure regimes, and M15 confirmation
events.  It deliberately stops at a paper decision.  It has no broker, order,
fill, account, sizing, stop, target, or P&L behavior.

One decision is emitted for every sealed M15 lifecycle transition.  BUY and
SELL are proposed only for that completed bar's immediate next open (the bar
end).  A scheduled closure cancels the proposal rather than queuing an entry
for reopening.  The finite calendar's end is an explicit unknown-open gate.  A
NO_TRADE decision has ``proposed_entry_at=None``.  Evidence that was not
available by the next-open deadline can never turn that historical decision
into an action.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import math
from typing import Dict, Iterable, Optional, Sequence, Tuple

from .domain import AvailabilityBasis
from .m15_confirmation import (
    CandleCombination,
    CandlePatternKind,
    ConfirmationEvent,
    ConfirmationPolicy,
    CrossoverTiming,
    TouchBarConfirmation,
    confirmation_time_eligible_for_touch,
)
from .market_regime import (
    H1RegimeEvent,
    MarketRegime,
    RegimePolicy,
    StructureLeg,
)
from .multitimeframe import H1_DURATION, M15_DURATION, H1PivotEvent, PivotKind
from .session_calendar import SessionCalendarArtifact
from .session_timing import (
    SESSION_STRATEGY_SEMANTICS,
    next_open_start,
    open_bar_count,
    validate_calendar,
)
from .supply_demand import ImpulseDirection, OriginSelectionPolicy, ZoneEvent, ZoneKind
from .zone_lifecycle import (
    EqualTimeOrder,
    ObservationTimeframe,
    RetestEpisodeStarted,
    ZoneBookUpdate,
    ZoneLifecyclePolicy,
    ZoneObservationTransition,
    ZoneState,
    select_retest_starts,
)


class CandidateDataError(ValueError):
    """Raised when input streams cannot be joined without guessing."""


class EntryTiming(str, Enum):
    """Only an immediately following M15 open may be proposed."""

    NEXT_M15_OPEN_ONLY = "next_m15_open_only"


class RegimeAlignment(str, Enum):
    """How direction is filtered by the latest point-in-time H1 regime."""

    STRICT_DIRECTIONAL = "strict_directional"


class RetestValidity(str, Enum):
    """How long a started retest may support a confirmation."""

    ZONE_VALID_AT_CONFIRMATION_STEP = "zone_valid_at_confirmation_step"


class CandidateAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NO_TRADE = "no_trade"


class CandidateReason(str, Enum):
    """Stable machine-readable explanations for every paper decision."""

    BUY_CRITERIA_MET = "buy_criteria_met"
    SELL_CRITERIA_MET = "sell_criteria_met"
    M15_TRANSITION_LATE_FOR_NEXT_OPEN = "m15_transition_late_for_next_open"
    NO_TIMELY_CONFIRMATION = "no_timely_confirmation"
    CONFLICTING_TIMELY_CONFIRMATIONS = "conflicting_timely_confirmations"
    NO_VISIBLE_H1_REGIME = "no_visible_h1_regime"
    H1_REGIME_UNKNOWN = "h1_regime_unknown"
    H1_REGIME_RANGE = "h1_regime_range"
    H1_REGIME_DIRECTION_MISMATCH = "h1_regime_direction_mismatch"
    NO_ELIGIBLE_DIRECTIONAL_RETEST = "no_eligible_directional_retest"
    TOUCH_BAR_CONFIRMATION_EXCLUDED = "touch_bar_confirmation_excluded"
    CONFIRMATION_WINDOW_EXPIRED = "confirmation_window_expired"
    ZONE_INVALID_AT_M15_STEP = "zone_invalid_at_m15_step"
    SCHEDULED_CLOSURE_NEXT_OPEN = "scheduled_closure_next_open"
    CALENDAR_COVERAGE_EXHAUSTED = "calendar_coverage_exhausted"


DecisionKey = Tuple[str, datetime, datetime, datetime]


@dataclass(frozen=True)
class CandidatePolicy:
    """Required choices and exact formation provenance for one experiment.

    Semantic choices have no defaults; an optional calendar enables the
    separately fingerprinted session semantics.  A candidate run accepts zones from
    exactly one impulse-policy fingerprint and one origin-policy configuration;
    a mixed or unexpected formation stream is rejected rather than filtered.
    """

    expected_impulse_policy_fingerprint: str
    expected_origin_policy: OriginSelectionPolicy
    expected_origin_lookback_bars: int
    entry_timing: EntryTiming
    regime_alignment: RegimeAlignment
    retest_validity: RetestValidity
    calendar: Optional[SessionCalendarArtifact] = None

    def __post_init__(self) -> None:
        validate_calendar(self.calendar)
        _validate_fingerprint(
            self.expected_impulse_policy_fingerprint,
            "expected_impulse_policy_fingerprint",
        )
        _require_enum(
            self.expected_origin_policy,
            OriginSelectionPolicy,
            "expected_origin_policy",
        )
        if isinstance(self.expected_origin_lookback_bars, bool) or not isinstance(
            self.expected_origin_lookback_bars, int
        ):
            raise TypeError("expected_origin_lookback_bars must be an integer")
        if self.expected_origin_lookback_bars < 1:
            raise ValueError("expected_origin_lookback_bars must be positive")
        _require_enum(self.entry_timing, EntryTiming, "entry_timing")
        _require_enum(self.regime_alignment, RegimeAlignment, "regime_alignment")
        _require_enum(self.retest_validity, RetestValidity, "retest_validity")

    @property
    def canonical_identity(self) -> str:
        identity = ";".join(
            (
                "paper-candidate-policy-v1",
                "expected_impulse_policy_fingerprint={}".format(
                    self.expected_impulse_policy_fingerprint
                ),
                "expected_origin_policy={}".format(
                    self.expected_origin_policy.value
                ),
                "expected_origin_lookback_bars={}".format(
                    self.expected_origin_lookback_bars
                ),
                "entry_timing={}".format(self.entry_timing.value),
                "regime_alignment={}".format(self.regime_alignment.value),
                "retest_validity={}".format(self.retest_validity.value),
            )
        )
        if self.calendar is not None:
            identity += ";calendar={};session_semantics={}".format(
                self.calendar.fingerprint, SESSION_STRATEGY_SEMANTICS
            )
        return identity

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_identity.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class CandidateDecision:
    """One immutable paper decision for one exact M15 lifecycle step."""

    action: CandidateAction
    reasons: Tuple[CandidateReason, ...]
    m15_bar_start: datetime
    m15_bar_end: datetime
    next_m15_open: datetime
    proposed_entry_at: Optional[datetime]
    available_at: datetime
    transition_available_at: datetime
    transition_sealed_through: datetime
    direction: Optional[ImpulseDirection]
    confirmation: Optional[ConfirmationEvent]
    regime: Optional[H1RegimeEvent]
    selected_retests: Tuple[RetestEpisodeStarted, ...]
    policy_fingerprint: str
    candidate_policy_fingerprint: str
    lifecycle_policy_fingerprint: str
    regime_policy_fingerprint: str
    confirmation_policy_fingerprint: str
    calendar: Optional[SessionCalendarArtifact] = None

    def __post_init__(self) -> None:
        validate_calendar(self.calendar)
        _require_enum(self.action, CandidateAction, "action")
        if not isinstance(self.reasons, tuple) or not self.reasons:
            raise ValueError("decision reasons must be a non-empty tuple")
        if any(not isinstance(reason, CandidateReason) for reason in self.reasons):
            raise TypeError("decision reasons must contain CandidateReason values")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("decision reasons must be unique")
        _validate_m15_interval(self.m15_bar_start, self.m15_bar_end, "decision bar")
        expected_next = self.m15_bar_end
        if self.calendar is not None:
            self.calendar.validate_bar(self.m15_bar_start, self.m15_bar_end)
            expected_next = next_open_start(self.calendar, self.m15_bar_end)
        if self.next_m15_open != expected_next:
            raise ValueError("next_m15_open conflicts with the exact next open")
        closure_reason = _next_open_failure(self.calendar, self.m15_bar_end)
        if closure_reason is not None and (
            self.action != CandidateAction.NO_TRADE
            or self.reasons != (closure_reason,)
        ):
            raise ValueError("a closing or uncovered next open requires explicit NO_TRADE")
        if closure_reason is None and any(
            reason in (
                CandidateReason.SCHEDULED_CLOSURE_NEXT_OPEN,
                CandidateReason.CALENDAR_COVERAGE_EXHAUSTED,
            )
            for reason in self.reasons
        ):
            raise ValueError("next-open closure reason conflicts with the calendar")
        for value, name in (
            (self.available_at, "available_at"),
            (self.transition_available_at, "transition_available_at"),
            (self.transition_sealed_through, "transition_sealed_through"),
        ):
            _require_utc(value, name)
        if self.transition_sealed_through < self.transition_available_at:
            raise ValueError("transition watermark cannot precede its availability")
        for value, name in (
            (self.policy_fingerprint, "policy_fingerprint"),
            (self.candidate_policy_fingerprint, "candidate_policy_fingerprint"),
            (self.lifecycle_policy_fingerprint, "lifecycle_policy_fingerprint"),
            (self.regime_policy_fingerprint, "regime_policy_fingerprint"),
            (self.confirmation_policy_fingerprint, "confirmation_policy_fingerprint"),
        ):
            _validate_fingerprint(value, name)
        if self.action == CandidateAction.NO_TRADE:
            if self.proposed_entry_at is not None:
                raise ValueError("NO_TRADE cannot propose an entry timestamp")
        else:
            if self.proposed_entry_at != self.next_m15_open:
                raise ValueError("BUY/SELL must propose exactly the next M15 open")
            if self.available_at > self.proposed_entry_at:
                raise ValueError("an actionable candidate cannot be backdated")
            if self.confirmation is None or self.regime is None:
                raise ValueError("an actionable candidate requires confirmation and regime")
            if not self.selected_retests:
                raise ValueError("an actionable candidate requires a selected retest")

    @property
    def key(self) -> DecisionKey:
        return (
            self.policy_fingerprint,
            self.m15_bar_start,
            self.transition_available_at,
            self.transition_sealed_through,
        )

    @property
    def confirmation_key(self) -> Optional[tuple]:
        return None if self.confirmation is None else self.confirmation.key

    @property
    def regime_key(self) -> Optional[tuple]:
        return None if self.regime is None else self.regime.key

    @property
    def zone_keys(self) -> Tuple[tuple, ...]:
        return tuple(event.zone.key for event in self.selected_retests)


def _require_enum(value: object, enum_type: object, field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError("{} must be a {}".format(field_name, enum_type.__name__))


def _validate_fingerprint(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("{} must be a lowercase SHA-256 digest".format(field_name))


def _require_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("{} must be a datetime".format(field_name))
    if value.tzinfo != timezone.utc:
        raise CandidateDataError("{} must be normalized to UTC".format(field_name))
    return value


def _normalize_as_of(as_of: Optional[datetime]) -> Optional[datetime]:
    if as_of is None:
        return None
    if not isinstance(as_of, datetime):
        raise TypeError("as_of must be a datetime")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return as_of.astimezone(timezone.utc)


def _validate_m15_interval(start: datetime, end: datetime, label: str) -> None:
    _require_utc(start, "{} start".format(label))
    _require_utc(end, "{} end".format(label))
    if end - start != M15_DURATION:
        raise CandidateDataError("{} must span exactly 15 minutes".format(label))
    if start.minute % 15 or start.second or start.microsecond:
        raise CandidateDataError("{} must align to a UTC M15 boundary".format(label))


def _validate_zone_provenance(zone: ZoneEvent, policy: CandidatePolicy) -> None:
    if not isinstance(zone, ZoneEvent):
        raise TypeError("zone state must contain a ZoneEvent")
    if zone.calendar != policy.calendar:
        raise CandidateDataError("zone calendar is mixed or unexpected")
    fingerprint = zone.impulse_key[0] if len(zone.impulse_key) == 4 else None
    if fingerprint != policy.expected_impulse_policy_fingerprint:
        raise CandidateDataError(
            "zone impulse policy fingerprint is mixed or unexpected"
        )
    if zone.origin_policy != policy.expected_origin_policy:
        raise CandidateDataError("zone origin policy is mixed or unexpected")
    if zone.origin_lookback_bars != policy.expected_origin_lookback_bars:
        raise CandidateDataError("zone origin lookback is mixed or unexpected")


def _validate_lifecycle(
    lifecycle: ZoneBookUpdate, policy: CandidatePolicy
) -> Tuple[ZoneObservationTransition, ...]:
    if not isinstance(lifecycle, ZoneBookUpdate):
        raise TypeError("lifecycle must be a ZoneBookUpdate")
    lifecycle_policy = lifecycle.book.policy
    if not isinstance(lifecycle_policy, ZoneLifecyclePolicy):
        raise TypeError("lifecycle book must bind a ZoneLifecyclePolicy")
    if lifecycle_policy.calendar != policy.calendar:
        raise CandidateDataError("lifecycle calendar conflicts with candidate policy")
    if lifecycle.book.policy_fingerprint != lifecycle_policy.fingerprint:
        raise CandidateDataError("lifecycle book policy fingerprint conflicts")
    for transition in lifecycle.transitions:
        if transition.calendar != policy.calendar:
            raise CandidateDataError("transition calendar conflicts with candidate policy")
        if transition.policy_fingerprint != lifecycle_policy.fingerprint:
            raise CandidateDataError("transition policy fingerprint conflicts")
        for state in transition.states_before + transition.states_after:
            if not isinstance(state, ZoneState):
                raise TypeError("transition snapshots must contain ZoneState values")
            if state.policy_fingerprint != lifecycle_policy.fingerprint:
                raise CandidateDataError("zone state policy fingerprint conflicts")
            _validate_zone_provenance(state.zone, policy)
        for event in transition.events:
            if event.policy_fingerprint != lifecycle_policy.fingerprint:
                raise CandidateDataError("lifecycle event policy fingerprint conflicts")
            _validate_zone_provenance(event.zone, policy)
    for state in lifecycle.book.states:
        _validate_zone_provenance(state.zone, policy)
    return lifecycle.transitions


def _validate_pivot(
    pivot: H1PivotEvent,
    expected_kind: PivotKind,
    calendar: Optional[SessionCalendarArtifact] = None,
) -> None:
    if not isinstance(pivot, H1PivotEvent):
        raise TypeError("regime evidence must contain H1PivotEvent values")
    if pivot.kind != expected_kind:
        raise CandidateDataError("regime pivot kind conflicts with evidence tuple")
    start = _require_utc(pivot.pivot_bar_start, "pivot_bar_start")
    end = _require_utc(pivot.pivot_bar_end, "pivot_bar_end")
    confirmed = _require_utc(pivot.confirmed_at, "pivot confirmed_at")
    available = _require_utc(pivot.available_at, "pivot available_at")
    if end - start != H1_DURATION or start.minute or start.second or start.microsecond:
        raise CandidateDataError("regime pivot must identify one aligned H1 bar")
    if (
        type(pivot.left_wing) is not int
        or type(pivot.right_wing) is not int
        or pivot.left_wing != 3
        or pivot.right_wing != 3
    ):
        raise CandidateDataError("regime pivot must bind the exact 3-left/3-right rule")
    if calendar is not None:
        calendar.validate_bar(start, end)
        calendar.validate_bar(confirmed - H1_DURATION, confirmed)
        confirmation_intervals = open_bar_count(calendar, end, confirmed, H1_DURATION)
    else:
        confirmation_intervals = (confirmed - end) / H1_DURATION
    if confirmation_intervals != 3:
        raise CandidateDataError("regime pivot confirmation time conflicts")
    if available < confirmed:
        raise CandidateDataError("regime pivot availability is backdated")
    _require_enum(pivot.availability_basis, AvailabilityBasis, "pivot availability_basis")
    for value in (pivot.bid_price, pivot.ask_price, pivot.mid_price):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("regime pivot prices must be numbers")
        if not math.isfinite(value) or value <= 0:
            raise CandidateDataError("regime pivot prices must be finite and positive")
    if pivot.bid_price > pivot.ask_price:
        raise CandidateDataError("regime pivot bid cannot exceed ask")
    if pivot.mid_price != (pivot.bid_price + pivot.ask_price) / 2.0:
        raise CandidateDataError("regime pivot midpoint conflicts with bid/ask")


def _structure(pivots: Sequence[H1PivotEvent]) -> StructureLeg:
    if len(pivots) < 2:
        return StructureLeg.INSUFFICIENT
    if pivots[-1].mid_price > pivots[-2].mid_price:
        return StructureLeg.RISING
    if pivots[-1].mid_price < pivots[-2].mid_price:
        return StructureLeg.FALLING
    return StructureLeg.EQUAL


def _expected_regime(high: StructureLeg, low: StructureLeg) -> MarketRegime:
    if high == StructureLeg.INSUFFICIENT or low == StructureLeg.INSUFFICIENT:
        return MarketRegime.UNKNOWN
    if high == StructureLeg.RISING and low == StructureLeg.RISING:
        return MarketRegime.BULLISH
    if high == StructureLeg.FALLING and low == StructureLeg.FALLING:
        return MarketRegime.BEARISH
    return MarketRegime.RANGE


def _validate_regimes(
    regimes: Iterable[H1RegimeEvent], policy: RegimePolicy
) -> Tuple[H1RegimeEvent, ...]:
    if not isinstance(policy, RegimePolicy):
        raise TypeError("regime_policy must be a RegimePolicy")
    source = tuple(regimes)
    seen = set()
    previous_structure_start = None  # type: Optional[datetime]
    for event in source:
        if not isinstance(event, H1RegimeEvent):
            raise TypeError("regimes must contain H1RegimeEvent values")
        if event.policy_fingerprint != policy.fingerprint:
            raise CandidateDataError("regime event policy fingerprint conflicts")
        if (
            event.structure_rule != policy.structure_rule
            or event.insufficient_evidence != policy.insufficient_evidence
            or event.mixed_structure != policy.mixed_structure
        ):
            raise CandidateDataError("regime event policy fields conflict")
        _require_enum(event.regime, MarketRegime, "regime")
        _require_enum(event.high_structure, StructureLeg, "high_structure")
        _require_enum(event.low_structure, StructureLeg, "low_structure")
        start = _require_utc(event.structure_bar_start, "structure_bar_start")
        end = _require_utc(event.structure_bar_end, "structure_bar_end")
        confirmed = _require_utc(event.confirmed_at, "regime confirmed_at")
        available = _require_utc(event.available_at, "regime available_at")
        if end - start != H1_DURATION or start.minute or start.second or start.microsecond:
            raise CandidateDataError("regime structure bar must be one aligned H1 bar")
        if available < confirmed:
            raise CandidateDataError("regime availability is backdated")
        _require_enum(
            event.availability_basis,
            AvailabilityBasis,
            "regime availability_basis",
        )
        if not isinstance(event.high_pivots, tuple) or not isinstance(event.low_pivots, tuple):
            raise TypeError("regime pivot evidence must be immutable tuples")
        if not 1 <= len(event.high_pivots) + len(event.low_pivots) <= 4:
            raise CandidateDataError("regime must bind one to four exact pivots")
        if len(event.high_pivots) > 2 or len(event.low_pivots) > 2:
            raise CandidateDataError("regime may bind at most two pivots per kind")
        for pivots, kind in (
            (event.high_pivots, PivotKind.HIGH),
            (event.low_pivots, PivotKind.LOW),
        ):
            for pivot in pivots:
                _validate_pivot(pivot, kind, policy.calendar)
            if any(
                pivots[index].pivot_bar_start >= pivots[index + 1].pivot_bar_start
                for index in range(len(pivots) - 1)
            ):
                raise CandidateDataError("regime pivots must be chronologically ordered")
        high = _structure(event.high_pivots)
        low = _structure(event.low_pivots)
        if event.high_structure != high or event.low_structure != low:
            raise CandidateDataError("regime structure legs conflict with pivot evidence")
        if event.regime != _expected_regime(high, low):
            raise CandidateDataError("regime classification conflicts with evidence")
        evidence = event.high_pivots + event.low_pivots
        latest_structure = max(pivot.pivot_bar_start for pivot in evidence)
        if event.structure_bar_start != latest_structure:
            raise CandidateDataError("regime structure time conflicts with exact evidence")
        trigger = tuple(
            pivot for pivot in evidence if pivot.pivot_bar_start == latest_structure
        )
        if not trigger or event.confirmed_at != trigger[-1].confirmed_at:
            raise CandidateDataError("regime confirmation time conflicts with trigger")
        ordered_evidence = tuple(
            sorted(
                evidence,
                key=lambda pivot: (
                    pivot.pivot_bar_start,
                    0 if pivot.kind == PivotKind.HIGH else 1,
                ),
            )
        )
        gate = max(
            enumerate(ordered_evidence),
            key=lambda item: (item[1].available_at, item[0]),
        )[1]
        if (
            event.available_at != gate.available_at
            or event.availability_basis != gate.availability_basis
        ):
            raise CandidateDataError("regime availability conflicts with exact evidence")
        if event.key in seen:
            raise CandidateDataError("regime events must be unique")
        seen.add(event.key)
        if previous_structure_start is not None and start <= previous_structure_start:
            raise CandidateDataError("regime events must be in structure-time order")
        previous_structure_start = start
    return source


def _validate_confirmation_event(
    event: ConfirmationEvent, policy: ConfirmationPolicy
) -> None:
    if not isinstance(event, ConfirmationEvent):
        raise TypeError("confirmations must contain ConfirmationEvent values")
    if event.policy_fingerprint != policy.fingerprint:
        raise CandidateDataError("confirmation event policy fingerprint conflicts")
    if (
        event.ema_period != policy.ema_period
        or event.ema_initialization != policy.ema_initialization
        or event.crossover_timing != policy.crossover_timing
        or event.candle_combination != policy.candle_combination
        or event.touch_bar_confirmation != policy.touch_bar_confirmation
        or event.confirmation_expiry_bars != policy.confirmation_expiry_bars
    ):
        raise CandidateDataError("confirmation event policy fields conflict")
    _require_enum(event.direction, ImpulseDirection, "confirmation direction")
    _validate_m15_interval(event.bar_start, event.bar_end, "confirmation bar")
    if policy.calendar is not None:
        policy.calendar.validate_bar(event.bar_start, event.bar_end)
    confirmed = _require_utc(event.confirmed_at, "confirmation confirmed_at")
    available = _require_utc(event.available_at, "confirmation available_at")
    if confirmed != event.bar_end or available < confirmed:
        raise CandidateDataError("confirmation timing is backdated or inconsistent")
    _require_enum(
        event.availability_basis,
        AvailabilityBasis,
        "confirmation availability_basis",
    )
    if not isinstance(event.proving_patterns, tuple) or not event.proving_patterns:
        raise CandidateDataError("confirmation must bind proving candle patterns")
    if any(not isinstance(kind, CandlePatternKind) for kind in event.proving_patterns):
        raise TypeError("proving_patterns must contain CandlePatternKind values")
    if len(set(event.proving_patterns)) != len(event.proving_patterns):
        raise CandidateDataError("confirmation proving patterns must be unique")
    pattern_set = set(event.proving_patterns)
    if policy.candle_combination == CandleCombination.ENGULFING_ONLY:
        expected = {CandlePatternKind.ENGULFING}
        if pattern_set != expected:
            raise CandidateDataError("confirmation pattern proof conflicts with policy")
    elif policy.candle_combination == CandleCombination.DISPLACEMENT_ONLY:
        expected = {CandlePatternKind.DISPLACEMENT}
        if pattern_set != expected:
            raise CandidateDataError("confirmation pattern proof conflicts with policy")
    elif policy.candle_combination == CandleCombination.ENGULFING_AND_DISPLACEMENT:
        expected = {
            CandlePatternKind.ENGULFING,
            CandlePatternKind.DISPLACEMENT,
        }
        if pattern_set != expected:
            raise CandidateDataError("confirmation pattern proof conflicts with policy")
    elif policy.candle_combination == CandleCombination.ENGULFING_OR_DISPLACEMENT:
        if not pattern_set.issubset(
            {CandlePatternKind.ENGULFING, CandlePatternKind.DISPLACEMENT}
        ):
            raise CandidateDataError("confirmation pattern proof conflicts with policy")
    else:
        raise ValueError("candle_combination is not implemented")
    numeric_fields = (
        event.prior_close,
        event.prior_ema,
        event.current_close,
        event.current_ema,
        event.current_crossover_reference,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        for value in numeric_fields
    ):
        raise CandidateDataError("confirmation prices must be finite and positive")
    expected_reference = (
        event.current_ema
        if policy.crossover_timing == CrossoverTiming.SAME_BAR_EMA
        else event.prior_ema
    )
    if event.current_crossover_reference != expected_reference:
        raise CandidateDataError("confirmation crossover reference conflicts")
    if event.direction == ImpulseDirection.BULLISH:
        crossed = (
            event.prior_close <= event.prior_ema
            and event.current_close > expected_reference
        )
    else:
        crossed = (
            event.prior_close >= event.prior_ema
            and event.current_close < expected_reference
        )
    if not crossed:
        raise CandidateDataError("confirmation direction conflicts with crossover")


def _validate_confirmations(
    confirmations: Iterable[ConfirmationEvent], policy: ConfirmationPolicy
) -> Tuple[ConfirmationEvent, ...]:
    if not isinstance(policy, ConfirmationPolicy):
        raise TypeError("confirmation_policy must be a ConfirmationPolicy")
    source = tuple(confirmations)
    seen = set()
    previous_order = None  # type: Optional[Tuple[datetime, int]]
    for event in source:
        _validate_confirmation_event(event, policy)
        if event.key in seen:
            raise CandidateDataError("confirmation events must be unique")
        seen.add(event.key)
        direction_order = 0 if event.direction == ImpulseDirection.BULLISH else 1
        order = (event.bar_start, direction_order)
        if previous_order is not None and order <= previous_order:
            raise CandidateDataError("confirmation events must be in canonical bar order")
        previous_order = order
    return source


def _combined_policy_fingerprint(
    candidate: CandidatePolicy,
    lifecycle: ZoneLifecyclePolicy,
    regime: RegimePolicy,
    confirmation: ConfirmationPolicy,
) -> str:
    identity = ";".join(
        (
            "paper-candidate-policy-bundle-v1",
            "candidate={}".format(candidate.fingerprint),
            "lifecycle={}".format(lifecycle.fingerprint),
            "regime={}".format(regime.fingerprint),
            "confirmation={}".format(confirmation.fingerprint),
        )
    )
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


def _visible_regime(
    regimes: Sequence[H1RegimeEvent],
    transition: ZoneObservationTransition,
    lifecycle_policy: ZoneLifecyclePolicy,
) -> Optional[H1RegimeEvent]:
    observation_at = transition.observation.bar.available_at
    if lifecycle_policy.equal_time_order == EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH:
        visible = tuple(event for event in regimes if event.available_at <= observation_at)
    elif lifecycle_policy.equal_time_order == EqualTimeOrder.M15_TOUCH_THEN_H1_INVALIDATION:
        visible = tuple(event for event in regimes if event.available_at < observation_at)
    else:
        raise ValueError("equal_time_order is not implemented")
    if not visible:
        return None
    return max(
        visible,
        key=lambda event: (
            event.structure_bar_start,
            event.available_at,
            event.confirmed_at,
        ),
    )


def _state_by_zone(
    transition: ZoneObservationTransition,
) -> Dict[tuple, ZoneState]:
    return {state.zone.key: state for state in transition.states_after}


def _mid_high(bar: object) -> float:
    return (bar.bid_high + bar.ask_high) / 2.0


def _mid_low(bar: object) -> float:
    return (bar.bid_low + bar.ask_low) / 2.0


def _overlaps(bar: object, zone: ZoneEvent) -> bool:
    return _mid_low(bar) <= zone.upper_price and _mid_high(bar) >= zone.lower_price


def _direction_kind(direction: ImpulseDirection) -> ZoneKind:
    return ZoneKind.DEMAND if direction == ImpulseDirection.BULLISH else ZoneKind.SUPPLY


def _regime_reason(
    regime: Optional[H1RegimeEvent], direction: ImpulseDirection
) -> Optional[CandidateReason]:
    if regime is None:
        return CandidateReason.NO_VISIBLE_H1_REGIME
    if regime.regime == MarketRegime.UNKNOWN:
        return CandidateReason.H1_REGIME_UNKNOWN
    if regime.regime == MarketRegime.RANGE:
        return CandidateReason.H1_REGIME_RANGE
    expected = (
        MarketRegime.BULLISH
        if direction == ImpulseDirection.BULLISH
        else MarketRegime.BEARISH
    )
    if regime.regime != expected:
        return CandidateReason.H1_REGIME_DIRECTION_MISMATCH
    return None


def _retest_selection(
    transitions: Sequence[ZoneObservationTransition],
    target_index: int,
    direction: ImpulseDirection,
    confirmation: ConfirmationEvent,
    confirmation_policy: ConfirmationPolicy,
    lifecycle_policy: ZoneLifecyclePolicy,
) -> Tuple[Tuple[RetestEpisodeStarted, ...], Tuple[CandidateReason, ...]]:
    target = transitions[target_index]
    kind = _direction_kind(direction)
    starts = []
    for transition in transitions[: target_index + 1]:
        if transition.observation.timeframe != ObservationTimeframe.M15:
            continue
        for event in transition.events:
            if (
                isinstance(event, RetestEpisodeStarted)
                and event.eligible
                and event.zone.kind == kind
                and event.m15_bar_end <= confirmation.bar_end
                and event.available_at <= confirmation.bar_end
            ):
                starts.append(event)

    timed = tuple(
        event
        for event in starts
        if confirmation_time_eligible_for_touch(
            confirmation,
            touch_bar_start=event.m15_bar_start,
            touch_bar_end=event.m15_bar_end,
            policy=confirmation_policy,
        )
    )
    states = _state_by_zone(target)
    valid = tuple(
        event
        for event in timed
        if event.zone.key in states and states[event.zone.key].valid
    )
    if valid:
        newest_touch = max(event.m15_bar_start for event in valid)
        latest = tuple(
            event for event in valid if event.m15_bar_start == newest_touch
        )
        return (
            select_retest_starts(latest, kind=kind, policy=lifecycle_policy),
            (),
        )

    if timed:
        return (), (CandidateReason.ZONE_INVALID_AT_M15_STEP,)

    current_bar = target.observation.bar
    invalid_overlap = any(
        state.zone.kind == kind
        and not state.valid
        and _overlaps(current_bar, state.zone)
        for state in target.states_after
    )
    if invalid_overlap:
        return (), (CandidateReason.ZONE_INVALID_AT_M15_STEP,)

    timing_reasons = []
    minimum = (
        0
        if confirmation_policy.touch_bar_confirmation
        == TouchBarConfirmation.TOUCH_BAR_ALLOWED
        else 1
    )
    deltas = tuple(
        (confirmation.bar_end - event.m15_bar_end) / M15_DURATION
        for event in starts
    )
    if any(delta < minimum for delta in deltas):
        timing_reasons.append(CandidateReason.TOUCH_BAR_CONFIRMATION_EXCLUDED)
    if any(delta > confirmation_policy.confirmation_expiry_bars for delta in deltas):
        timing_reasons.append(CandidateReason.CONFIRMATION_WINDOW_EXPIRED)
    if timing_reasons:
        return (), tuple(timing_reasons)
    return (), (CandidateReason.NO_ELIGIBLE_DIRECTIONAL_RETEST,)


def _decision(
    *,
    transition: ZoneObservationTransition,
    action: CandidateAction,
    reasons: Tuple[CandidateReason, ...],
    direction: Optional[ImpulseDirection],
    confirmation: Optional[ConfirmationEvent],
    regime: Optional[H1RegimeEvent],
    retests: Tuple[RetestEpisodeStarted, ...],
    available_at: datetime,
    bundle_fingerprint: str,
    candidate_policy: CandidatePolicy,
    lifecycle_policy: ZoneLifecyclePolicy,
    regime_policy: RegimePolicy,
    confirmation_policy: ConfirmationPolicy,
) -> CandidateDecision:
    bar = transition.observation.bar
    return CandidateDecision(
        action=action,
        reasons=reasons,
        m15_bar_start=bar.start_time,
        m15_bar_end=bar.timestamp,
        next_m15_open=(
            bar.timestamp
            if candidate_policy.calendar is None
            else next_open_start(candidate_policy.calendar, bar.timestamp)
        ),
        proposed_entry_at=(
            bar.timestamp if action in (CandidateAction.BUY, CandidateAction.SELL) else None
        ),
        available_at=available_at,
        transition_available_at=bar.available_at,
        transition_sealed_through=transition.sealed_through,
        direction=direction,
        confirmation=confirmation,
        regime=regime,
        selected_retests=retests,
        policy_fingerprint=bundle_fingerprint,
        candidate_policy_fingerprint=candidate_policy.fingerprint,
        lifecycle_policy_fingerprint=lifecycle_policy.fingerprint,
        regime_policy_fingerprint=regime_policy.fingerprint,
        confirmation_policy_fingerprint=confirmation_policy.fingerprint,
        calendar=candidate_policy.calendar,
    )


def _next_open_failure(
    calendar: Optional[SessionCalendarArtifact], bar_end: datetime
) -> Optional[CandidateReason]:
    """Do not queue a closing-bar signal for a later session or unknown open."""

    if calendar is None:
        return None
    next_start = next_open_start(calendar, bar_end)
    if next_start + M15_DURATION > calendar.coverage_end:
        return CandidateReason.CALENDAR_COVERAGE_EXHAUSTED
    if next_start != bar_end:
        return CandidateReason.SCHEDULED_CLOSURE_NEXT_OPEN
    return None


def paper_candidate_decisions(
    lifecycle: ZoneBookUpdate,
    regimes: Iterable[H1RegimeEvent],
    confirmations: Iterable[ConfirmationEvent],
    *,
    candidate_policy: CandidatePolicy,
    regime_policy: RegimePolicy,
    confirmation_policy: ConfirmationPolicy,
    as_of: Optional[datetime] = None
) -> Tuple[CandidateDecision, ...]:
    """Join immutable evidence into one paper decision per sealed M15 step.

    The ``lifecycle`` argument is a validated :class:`ZoneBookUpdate`, not a
    final ``ZoneBook`` or an arbitrary transition tuple.  Its constructor
    replays every transition and checks snapshot continuity, preserving the
    exact H1-first/M15-first state used here.

    Batch-completeness precondition: ``regimes`` and ``confirmations`` must be
    the complete, sealed positive-event outputs of their detectors through the
    lifecycle ledger's watermark.  A positive-only tuple cannot by itself
    prove that an omitted event did not exist.  Calling this function with a
    partial detector batch will therefore produce an invalid absence-based
    NO_TRADE conclusion; sealing that upstream batch is the caller's explicit
    trust boundary.

    Confirmation absence is evaluated at ``next_m15_open``.  A matching event
    whose ``available_at`` is later is intentionally indistinguishable from no
    timely event, so adding delayed evidence cannot rewrite a historical
    decision.  ``as_of`` controls when sealed decisions are returned; it never
    relaxes the next-open evidence deadline.
    """

    if not isinstance(candidate_policy, CandidatePolicy):
        raise TypeError("candidate_policy must be a CandidatePolicy")
    if not isinstance(regime_policy, RegimePolicy):
        raise TypeError("regime_policy must be a RegimePolicy")
    if not isinstance(confirmation_policy, ConfirmationPolicy):
        raise TypeError("confirmation_policy must be a ConfirmationPolicy")
    if (
        regime_policy.calendar != candidate_policy.calendar
        or confirmation_policy.calendar != candidate_policy.calendar
    ):
        raise CandidateDataError("candidate evidence policies must bind the same calendar")
    transitions = _validate_lifecycle(lifecycle, candidate_policy)
    source_regimes = _validate_regimes(regimes, regime_policy)
    source_confirmations = _validate_confirmations(
        confirmations, confirmation_policy
    )
    normalized_as_of = _normalize_as_of(as_of)
    lifecycle_policy = lifecycle.book.policy
    bundle = _combined_policy_fingerprint(
        candidate_policy,
        lifecycle_policy,
        regime_policy,
        confirmation_policy,
    )

    confirmations_by_bar = {}  # type: Dict[Tuple[datetime, datetime], list]
    for event in source_confirmations:
        confirmations_by_bar.setdefault((event.bar_start, event.bar_end), []).append(event)
    m15_bars = {
        (transition.observation.bar.start_time, transition.observation.bar.timestamp)
        for transition in transitions
        if transition.observation.timeframe == ObservationTimeframe.M15
    }
    unmatched = tuple(key for key in confirmations_by_bar if key not in m15_bars)
    if unmatched:
        raise CandidateDataError(
            "confirmation event has no exact M15 lifecycle transition"
        )

    decisions = []
    for index, transition in enumerate(transitions):
        if transition.observation.timeframe != ObservationTimeframe.M15:
            continue
        if (
            normalized_as_of is not None
            and transition.sealed_through > normalized_as_of
        ):
            continue
        bar = transition.observation.bar
        entry_deadline = bar.timestamp
        base_available = transition.sealed_through

        next_open_failure = _next_open_failure(candidate_policy.calendar, bar.timestamp)
        if next_open_failure is not None:
            decisions.append(
                _decision(
                    transition=transition,
                    action=CandidateAction.NO_TRADE,
                    reasons=(next_open_failure,),
                    direction=None,
                    confirmation=None,
                    regime=None,
                    retests=(),
                    available_at=base_available,
                    bundle_fingerprint=bundle,
                    candidate_policy=candidate_policy,
                    lifecycle_policy=lifecycle_policy,
                    regime_policy=regime_policy,
                    confirmation_policy=confirmation_policy,
                )
            )
            continue

        if base_available > entry_deadline:
            decisions.append(
                _decision(
                    transition=transition,
                    action=CandidateAction.NO_TRADE,
                    reasons=(CandidateReason.M15_TRANSITION_LATE_FOR_NEXT_OPEN,),
                    direction=None,
                    confirmation=None,
                    regime=None,
                    retests=(),
                    available_at=base_available,
                    bundle_fingerprint=bundle,
                    candidate_policy=candidate_policy,
                    lifecycle_policy=lifecycle_policy,
                    regime_policy=regime_policy,
                    confirmation_policy=confirmation_policy,
                )
            )
            continue

        timely = tuple(
            event
            for event in confirmations_by_bar.get((bar.start_time, bar.timestamp), ())
            if event.available_at <= entry_deadline
        )
        if not timely:
            decisions.append(
                _decision(
                    transition=transition,
                    action=CandidateAction.NO_TRADE,
                    reasons=(CandidateReason.NO_TIMELY_CONFIRMATION,),
                    direction=None,
                    confirmation=None,
                    regime=None,
                    retests=(),
                    available_at=base_available,
                    bundle_fingerprint=bundle,
                    candidate_policy=candidate_policy,
                    lifecycle_policy=lifecycle_policy,
                    regime_policy=regime_policy,
                    confirmation_policy=confirmation_policy,
                )
            )
            continue
        if len(timely) != 1:
            decisions.append(
                _decision(
                    transition=transition,
                    action=CandidateAction.NO_TRADE,
                    reasons=(CandidateReason.CONFLICTING_TIMELY_CONFIRMATIONS,),
                    direction=None,
                    confirmation=None,
                    regime=None,
                    retests=(),
                    available_at=max(
                        (base_available,) + tuple(event.available_at for event in timely)
                    ),
                    bundle_fingerprint=bundle,
                    candidate_policy=candidate_policy,
                    lifecycle_policy=lifecycle_policy,
                    regime_policy=regime_policy,
                    confirmation_policy=confirmation_policy,
                )
            )
            continue

        confirmation = timely[0]
        direction = confirmation.direction
        regime = _visible_regime(source_regimes, transition, lifecycle_policy)
        reasons = []
        regime_failure = _regime_reason(regime, direction)
        if regime_failure is not None:
            reasons.append(regime_failure)
        retests, retest_failures = _retest_selection(
            transitions,
            index,
            direction,
            confirmation,
            confirmation_policy,
            lifecycle_policy,
        )
        reasons.extend(retest_failures)
        evidence_times = [base_available, confirmation.available_at]
        if regime is not None:
            evidence_times.append(regime.available_at)
        evidence_times.extend(event.available_at for event in retests)
        available_at = max(evidence_times)

        if reasons:
            action = CandidateAction.NO_TRADE
            reason_tuple = tuple(reasons)
        elif direction == ImpulseDirection.BULLISH:
            action = CandidateAction.BUY
            reason_tuple = (CandidateReason.BUY_CRITERIA_MET,)
        elif direction == ImpulseDirection.BEARISH:
            action = CandidateAction.SELL
            reason_tuple = (CandidateReason.SELL_CRITERIA_MET,)
        else:
            raise ValueError("confirmation direction is not implemented")
        decisions.append(
            _decision(
                transition=transition,
                action=action,
                reasons=reason_tuple,
                direction=direction,
                confirmation=confirmation,
                regime=regime,
                retests=retests,
                available_at=available_at,
                bundle_fingerprint=bundle,
                candidate_policy=candidate_policy,
                lifecycle_policy=lifecycle_policy,
                regime_policy=regime_policy,
                confirmation_policy=confirmation_policy,
            )
        )
    return tuple(decisions)


__all__ = [
    "CandidateAction",
    "CandidateDataError",
    "CandidateDecision",
    "CandidatePolicy",
    "CandidateReason",
    "DecisionKey",
    "EntryTiming",
    "RegimeAlignment",
    "RetestValidity",
    "paper_candidate_decisions",
]
