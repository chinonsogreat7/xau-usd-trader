"""End-to-end, paper-only diagnostic replay for the XAU/USD hypothesis.

The replay is deliberately a composition boundary rather than an execution
engine.  It accepts one M15 bid/ask stream and one fully explicit
research-policy bundle, derives every upstream evidence stream, folds sealed
H1/M15 receipt groups through the zone lifecycle, and finishes at auditable
``BUY``/``SELL``/``NO_TRADE`` candidates.  It has no broker, order, fill,
position, stop, target, account, P&L, or profitability behavior.

The original policy requires strict contiguity. A separately named session
policy binds an explicit finite calendar and provisional closure semantics;
only exactly declared closures may interrupt the actual source bars.

``dataset_fingerprint`` must be the validated sidecar manifest fingerprint at
the CLI boundary. ``input_bars_fingerprint`` independently binds the exact
point-in-time M15 prefix consumed by this run, so a manifest cannot conceal a
change in candle data. Readiness is dataset-relative: early decisions remain
in the audit ledger as pre-roll, while the explicit history boundary records
that lifecycle state was assumed empty at the dataset start.
"""

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .candidate_signals import CandidateDecision, paper_candidate_decisions
from .domain import AvailabilityBasis, QuoteBar
from .m15_confirmation import (
    CandleCombination,
    CandlePatternEvent,
    ConfirmationEvent,
    DisplacementHistory,
    EmaEvent,
    EmaInitialization,
    m15_candle_events,
    m15_confirmation_events,
    m15_ema_events,
)
from .market_regime import H1RegimeEvent, h1_regime_events
from .multitimeframe import (
    M15_DURATION,
    PIVOT_LEFT_WING,
    PIVOT_RIGHT_WING,
    H1PivotEvent,
    aggregate_m15_to_h1,
    confirmed_h1_pivots,
)
from .research_baseline import ResearchPolicyBundle
from .session_calendar import SessionCalendarArtifact
from .session_timing import validate_calendar
from .supply_demand import (
    IMPULSE_WINDOW_BARS,
    AtrEvent,
    AtrTiming,
    ImpulseEvent,
    ZoneEvent,
    h1_atr_events,
    h1_impulse_events,
    supply_demand_zone_events,
)
from .zone_lifecycle import (
    CompletedBarObservation,
    ObservationTimeframe,
    SealedAvailabilityGroup,
    ZoneBookUpdate,
    ZoneLifecycleEvent,
    ZoneObservationTransition,
    advance_zone_book_group,
    new_zone_book,
    seal_availability_group,
)


class DiagnosticReplayDataError(ValueError):
    """Raised when an end-to-end replay would require guessing about data."""


DIAGNOSTIC_REPLAY_ENGINE_VERSION = "xauusd-diagnostic-replay-engine-v2"
DIAGNOSTIC_REPLAY_SCHEMA_VERSION = 2
SESSION_DIAGNOSTIC_REPLAY_ENGINE_VERSION = "xauusd-session-diagnostic-replay-engine-v1"
SESSION_DIAGNOSTIC_REPLAY_SCHEMA_VERSION = 3


class ReplayHistoryBoundary(str, Enum):
    """What, if anything, is assumed about lifecycle state before the feed."""

    EMPTY_STATE_AT_DATASET_START = "empty_state_at_dataset_start"


@dataclass(frozen=True)
class DiagnosticReplayReadiness:
    """History boundary separating pre-roll from post-pre-roll decisions.

    ``post_pre_roll_m15_start`` means all configured fixed-length indicators,
    the finite origin search, one impulse window, and one 3/3 pivot window can
    be evaluated from this dataset.  It does *not* assert that directional
    pivots or zones must exist, nor that zones formed before the dataset are
    known.  The latter limitation is explicit in ``history_boundary``.
    """

    history_boundary: ReplayHistoryBoundary
    pre_roll_m15_bars: int
    minimum_input_m15_bars: int
    post_pre_roll_m15_start: datetime
    pre_roll_decision_count: int
    post_pre_roll_decision_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.history_boundary, ReplayHistoryBoundary):
            raise TypeError("history_boundary must be a ReplayHistoryBoundary")
        for value, field_name in (
            (self.pre_roll_m15_bars, "pre_roll_m15_bars"),
            (self.minimum_input_m15_bars, "minimum_input_m15_bars"),
            (self.pre_roll_decision_count, "pre_roll_decision_count"),
            (self.post_pre_roll_decision_count, "post_pre_roll_decision_count"),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("{} must be an integer".format(field_name))
            if value < 0:
                raise ValueError("{} must be non-negative".format(field_name))
        if self.pre_roll_m15_bars < 1:
            raise ValueError("pre_roll_m15_bars must be positive")
        if self.minimum_input_m15_bars <= self.pre_roll_m15_bars:
            raise ValueError(
                "minimum_input_m15_bars must include a post-pre-roll bar"
            )
        if self.minimum_input_m15_bars % 4:
            raise ValueError("minimum_input_m15_bars must contain complete UTC hours")
        if (
            not isinstance(self.post_pre_roll_m15_start, datetime)
            or self.post_pre_roll_m15_start.tzinfo != timezone.utc
        ):
            raise ValueError("post_pre_roll_m15_start must be normalized to UTC")
        if (
            self.post_pre_roll_m15_start.minute % 15
            or self.post_pre_roll_m15_start.second
            or self.post_pre_roll_m15_start.microsecond
        ):
            raise ValueError("post_pre_roll_m15_start must align to M15")


def _frame(tag: bytes, payload: bytes) -> bytes:
    return tag + str(len(payload)).encode("ascii") + b":" + payload


def _stable_encode(value: object) -> bytes:
    """Encode supported immutable evidence values without repr ambiguity."""

    if value is None:
        return b"n0:"
    if isinstance(value, Enum):
        identity = "{}.{}".format(
            value.__class__.__module__, value.__class__.__qualname__
        ).encode("utf-8")
        return _frame(b"e", _frame(b"t", identity) + _stable_encode(value.value))
    if isinstance(value, bool):
        return b"b1:1" if value else b"b1:0"
    if isinstance(value, int):
        return _frame(b"i", str(value).encode("ascii"))
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("stable evidence encoding rejects non-finite floats")
        return _frame(b"f", value.hex().encode("ascii"))
    if isinstance(value, str):
        return _frame(b"s", value.encode("utf-8"))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stable evidence encoding requires aware datetimes")
        normalized = value.astimezone(timezone.utc)
        return _frame(
            b"d", normalized.isoformat(timespec="microseconds").encode("ascii")
        )
    if isinstance(value, tuple):
        return _frame(b"q", b"".join(_stable_encode(item) for item in value))
    if is_dataclass(value) and not isinstance(value, type):
        identity = "{}.{}".format(
            value.__class__.__module__, value.__class__.__qualname__
        ).encode("utf-8")
        payload = [_frame(b"t", identity)]
        for item in fields(value):
            # Optional calendar support must not rewrite strict-v1 identities.
            if item.name == "calendar" and getattr(value, item.name) is None:
                continue
            payload.append(_frame(b"k", item.name.encode("utf-8")))
            payload.append(_stable_encode(getattr(value, item.name)))
        return _frame(b"c", b"".join(payload))
    raise TypeError(
        "stable evidence encoding does not support {}".format(type(value).__name__)
    )


def _stable_digest(value: object) -> str:
    return hashlib.sha256(_stable_encode(value)).hexdigest()


def _validate_sha256(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("{} must be a lowercase SHA-256 digest".format(field_name))
    return value


def _normalize_as_of(as_of: Optional[datetime]) -> Optional[datetime]:
    if as_of is None:
        return None
    if not isinstance(as_of, datetime):
        raise TypeError("as_of must be a datetime")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return as_of.astimezone(timezone.utc)


def _bar_fingerprint(bars: Sequence[QuoteBar]) -> str:
    """Hash every semantic field using stable UTC and IEEE-754 encodings."""

    digest = hashlib.sha256()
    digest.update(b"xauusd-diagnostic-m15-bars-v1\n")
    for bar in bars:
        values = (
            bar.start_time.isoformat(timespec="microseconds"),
            bar.timestamp.isoformat(timespec="microseconds"),
            bar.available_at.isoformat(timespec="microseconds"),
            bar.availability_basis.value,
            float(bar.bid_open).hex(),
            float(bar.bid_high).hex(),
            float(bar.bid_low).hex(),
            float(bar.bid_close).hex(),
            float(bar.ask_open).hex(),
            float(bar.ask_high).hex(),
            float(bar.ask_low).hex(),
            float(bar.ask_close).hex(),
            "none" if bar.volume is None else float(bar.volume).hex(),
        )
        digest.update("|".join(values).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _visible_prefix(
    bars: Sequence[QuoteBar], as_of: Optional[datetime]
) -> Tuple[QuoteBar, ...]:
    if as_of is None:
        return tuple(bars)
    visible = tuple(bar.available_at <= as_of for bar in bars)
    first_hidden = next(
        (index for index, is_visible in enumerate(visible) if not is_visible),
        len(bars),
    )
    if any(visible[first_hidden + 1 :]):
        raise DiagnosticReplayDataError(
            "bars available by as_of must form one chronological prefix"
        )
    return tuple(bars[:first_hidden])


def _validate_m15_bars(
    bars: Sequence[QuoteBar], calendar: Optional[SessionCalendarArtifact] = None,
) -> AvailabilityBasis:
    validate_calendar(calendar)
    if not bars:
        raise DiagnosticReplayDataError("M15 input is empty at the replay cutoff")
    for index, bar in enumerate(bars):
        if not isinstance(bar, QuoteBar):
            raise TypeError("M15 input must contain QuoteBar values")
        if bar.timestamp - bar.start_time != M15_DURATION:
            raise DiagnosticReplayDataError(
                "M15 bar {} must span exactly 15 minutes".format(index)
            )
        if bar.start_time.tzinfo != timezone.utc or bar.timestamp.tzinfo != timezone.utc:
            raise DiagnosticReplayDataError("M15 bars must be normalized to UTC")
        if (
            bar.start_time.minute % 15
            or bar.start_time.second
            or bar.start_time.microsecond
        ):
            raise DiagnosticReplayDataError(
                "M15 bar {} is not aligned to a UTC 15-minute boundary".format(index)
            )
        if calendar is not None:
            calendar.validate_bar(bar.start_time, bar.timestamp)
            if index:
                calendar.validate_transition(bars[index - 1].timestamp, bar.start_time)
        elif index and bar.start_time != bars[index - 1].timestamp:
            raise DiagnosticReplayDataError(
                "M15 bar {} is not contiguous with the previous bar".format(index)
            )
        if index and bar.available_at < bars[index - 1].available_at:
            raise DiagnosticReplayDataError(
                "M15 receipt times must be non-decreasing in bar order"
            )
        if bar.availability_basis in (
            AvailabilityBasis.SYNTHETIC,
            AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
        ) and bar.available_at != bar.timestamp:
            raise DiagnosticReplayDataError(
                "M15 bar {} available_at must equal its end for {}".format(
                    index, bar.availability_basis.value
                )
            )
    if (
        bars[0].start_time.minute
        or bars[0].start_time.second
        or bars[0].start_time.microsecond
    ):
        raise DiagnosticReplayDataError(
            "M15 replay must begin on a UTC hour boundary"
        )
    if (
        bars[-1].timestamp.minute
        or bars[-1].timestamp.second
        or bars[-1].timestamp.microsecond
        or len(bars) % 4
    ):
        raise DiagnosticReplayDataError(
            "M15 replay must end on a UTC hour boundary with four bars per hour"
        )
    bases = {bar.availability_basis for bar in bars}
    if len(bases) != 1:
        raise DiagnosticReplayDataError(
            "M15 input mixes availability provenance within one replay"
        )
    return next(iter(bases))


def _required_h1_bars(bundle: ResearchPolicyBundle) -> int:
    policy = bundle.impulse_policy
    if policy.atr_timing == AtrTiming.BEFORE_IMPULSE_WINDOW:
        impulse_required = policy.atr_period + IMPULSE_WINDOW_BARS
    elif policy.atr_timing == AtrTiming.BEFORE_IMPULSE_END:
        impulse_required = max(
            IMPULSE_WINDOW_BARS,
            policy.atr_period + 1,
        )
    elif policy.atr_timing == AtrTiming.IMPULSE_END_INCLUSIVE:
        impulse_required = max(IMPULSE_WINDOW_BARS, policy.atr_period)
    else:
        raise ValueError("atr_timing is not implemented")
    pivot_required = PIVOT_LEFT_WING + 1 + PIVOT_RIGHT_WING
    # A finite lookback is semantically part of a zone non-match.  Before this
    # much history exists, "no opposite origin" would mean "not present in the
    # truncated feed", not "absent from the configured search interval".
    origin_required = bundle.origin_lookback_bars + IMPULSE_WINDOW_BARS
    return max(impulse_required, pivot_required, origin_required)


def _required_confirmation_bars(bundle: ResearchPolicyBundle) -> int:
    policy = bundle.confirmation_policy
    if policy.ema_initialization == EmaInitialization.FIRST_CLOSE_SEED:
        ema_required = 2
    elif policy.ema_initialization == EmaInitialization.SMA_PERIOD_SEED:
        ema_required = policy.ema_period + 1
    else:
        raise ValueError("ema_initialization is not implemented")

    pattern_requirements = []  # type: List[int]
    if policy.candle_combination in (
        CandleCombination.ENGULFING_ONLY,
        CandleCombination.ENGULFING_OR_DISPLACEMENT,
        CandleCombination.ENGULFING_AND_DISPLACEMENT,
    ):
        pattern_requirements.append(2)
    if policy.candle_combination in (
        CandleCombination.DISPLACEMENT_ONLY,
        CandleCombination.ENGULFING_OR_DISPLACEMENT,
        CandleCombination.ENGULFING_AND_DISPLACEMENT,
    ):
        if policy.displacement_history == DisplacementHistory.PREVIOUS_BARS_ONLY:
            pattern_requirements.append(policy.displacement_lookback_bars + 1)
        elif policy.displacement_history == DisplacementHistory.CURRENT_BAR_INCLUSIVE:
            pattern_requirements.append(policy.displacement_lookback_bars)
        else:
            raise ValueError("displacement_history is not implemented")
    if not pattern_requirements:
        raise ValueError("candle_combination is not implemented")
    return max((ema_required,) + tuple(pattern_requirements))


def diagnostic_pre_roll_m15_bars(bundle: ResearchPolicyBundle) -> int:
    """Return history preceding the first post-pre-roll M15 decision.

    The count covers every configured confirmation proof path, the complete
    finite origin search before a four-H1-bar impulse, its ATR dependency, and
    one complete 3/3 pivot window.  A positive impulse, pivot, zone, or
    confirmation is never required; a valid non-match remains valid evidence.

    This is a dataset-relative boundary.  The lifecycle still explicitly starts
    with an empty zone book at the first M15/H1 bar and therefore does not claim
    knowledge of formations that predate the supplied dataset.
    """

    if not isinstance(bundle, ResearchPolicyBundle):
        raise TypeError("policy_bundle must be a ResearchPolicyBundle")
    return max(
        4 * _required_h1_bars(bundle),
        _required_confirmation_bars(bundle) - 1,
    )


def diagnostic_warmup_m15_bars(bundle: ResearchPolicyBundle) -> int:
    """Backward-compatible name for :func:`diagnostic_pre_roll_m15_bars`."""

    return diagnostic_pre_roll_m15_bars(bundle)


def diagnostic_minimum_input_m15_bars(bundle: ResearchPolicyBundle) -> int:
    """Return complete-hour input needed for one post-pre-roll decision."""

    pre_roll = diagnostic_pre_roll_m15_bars(bundle)
    return ((pre_roll + 1 + 3) // 4) * 4


def _availability_groups(
    m15_bars: Sequence[QuoteBar],
    h1_bars: Sequence[QuoteBar],
    zones: Sequence[ZoneEvent],
    calendar: Optional[SessionCalendarArtifact] = None,
) -> Tuple[SealedAvailabilityGroup, ...]:
    buckets = {}  # type: Dict[datetime, Tuple[List[CompletedBarObservation], List[ZoneEvent]]]

    def observation_bucket(bar: QuoteBar) -> List[CompletedBarObservation]:
        pair = buckets.setdefault(bar.available_at, ([], []))
        return pair[0]

    for bar in m15_bars:
        observation_bucket(bar).append(
            CompletedBarObservation(
                timeframe=ObservationTimeframe.M15,
                bar=bar,
            )
        )
    for bar in h1_bars:
        observation_bucket(bar).append(
            CompletedBarObservation(
                timeframe=ObservationTimeframe.H1,
                bar=bar,
            )
        )
    for zone in zones:
        pair = buckets.setdefault(zone.available_at, ([], []))
        pair[1].append(zone)

    return tuple(
        seal_availability_group(
            tuple(buckets[available_at][0]),
            tuple(buckets[available_at][1]),
            sealed_through=available_at,
            calendar=calendar,
        )
        for available_at in sorted(buckets)
    )


def _fold_lifecycle(
    groups: Sequence[SealedAvailabilityGroup],
    bundle: ResearchPolicyBundle,
    *,
    h1_start: datetime,
    m15_start: datetime,
) -> ZoneBookUpdate:
    book = new_zone_book(
        (),
        bundle.lifecycle_policy,
        h1_start=h1_start,
        m15_start=m15_start,
    )
    events = []  # type: List[ZoneLifecycleEvent]
    transitions = []  # type: List[ZoneObservationTransition]
    for group in groups:
        update = advance_zone_book_group(book, group)
        book = update.book
        events.extend(update.events)
        transitions.extend(update.transitions)
    return ZoneBookUpdate(
        book=book,
        events=tuple(events),
        transitions=tuple(transitions),
    )


@dataclass(frozen=True)
class DiagnosticReplayResult:
    """Immutable evidence ledger for one point-in-time diagnostic replay."""

    dataset_fingerprint: str
    input_bars_fingerprint: str
    policy_bundle: ResearchPolicyBundle
    as_of: Optional[datetime]
    availability_basis: AvailabilityBasis
    readiness: DiagnosticReplayReadiness
    m15_bars: Tuple[QuoteBar, ...]
    h1_bars: Tuple[QuoteBar, ...]
    h1_atr: Tuple[AtrEvent, ...]
    h1_pivots: Tuple[H1PivotEvent, ...]
    h1_regimes: Tuple[H1RegimeEvent, ...]
    h1_impulses: Tuple[ImpulseEvent, ...]
    zones: Tuple[ZoneEvent, ...]
    m15_ema: Tuple[EmaEvent, ...]
    m15_candles: Tuple[CandlePatternEvent, ...]
    m15_confirmations: Tuple[ConfirmationEvent, ...]
    availability_groups: Tuple[SealedAvailabilityGroup, ...]
    lifecycle: ZoneBookUpdate
    decisions: Tuple[CandidateDecision, ...]

    def __post_init__(self) -> None:
        _validate_sha256(self.dataset_fingerprint, "dataset_fingerprint")
        _validate_sha256(self.input_bars_fingerprint, "input_bars_fingerprint")
        if not isinstance(self.policy_bundle, ResearchPolicyBundle):
            raise TypeError("policy_bundle must be a ResearchPolicyBundle")
        if self.as_of is not None and (
            not isinstance(self.as_of, datetime) or self.as_of.tzinfo != timezone.utc
        ):
            raise ValueError("result as_of must be normalized to UTC")
        if not isinstance(self.availability_basis, AvailabilityBasis):
            raise TypeError("availability_basis must be an AvailabilityBasis")
        if not isinstance(self.readiness, DiagnosticReplayReadiness):
            raise TypeError("readiness must be a DiagnosticReplayReadiness")
        tuple_fields = (
            "m15_bars",
            "h1_bars",
            "h1_atr",
            "h1_pivots",
            "h1_regimes",
            "h1_impulses",
            "zones",
            "m15_ema",
            "m15_candles",
            "m15_confirmations",
            "availability_groups",
            "decisions",
        )
        for field_name in tuple_fields:
            if not isinstance(getattr(self, field_name), tuple):
                raise TypeError("{} must be an immutable tuple".format(field_name))
        typed_streams = (
            (self.m15_bars, QuoteBar, "m15_bars"),
            (self.h1_bars, QuoteBar, "h1_bars"),
            (self.h1_atr, AtrEvent, "h1_atr"),
            (self.h1_pivots, H1PivotEvent, "h1_pivots"),
            (self.h1_regimes, H1RegimeEvent, "h1_regimes"),
            (self.h1_impulses, ImpulseEvent, "h1_impulses"),
            (self.zones, ZoneEvent, "zones"),
            (self.m15_ema, EmaEvent, "m15_ema"),
            (self.m15_candles, CandlePatternEvent, "m15_candles"),
            (self.m15_confirmations, ConfirmationEvent, "m15_confirmations"),
            (
                self.availability_groups,
                SealedAvailabilityGroup,
                "availability_groups",
            ),
            (self.decisions, CandidateDecision, "decisions"),
        )
        for stream, expected_type, field_name in typed_streams:
            if any(not isinstance(item, expected_type) for item in stream):
                raise TypeError(
                    "{} must contain {} values".format(
                        field_name, expected_type.__name__
                    )
                )
        if not isinstance(self.lifecycle, ZoneBookUpdate):
            raise TypeError("lifecycle must be a ZoneBookUpdate")
        if self.input_bars_fingerprint != _bar_fingerprint(self.m15_bars):
            raise ValueError("input_bars_fingerprint conflicts with M15 bars")
        if not self.m15_bars or not self.h1_bars:
            raise DiagnosticReplayDataError("replay result cannot contain empty bar streams")
        expected_pre_roll = diagnostic_pre_roll_m15_bars(self.policy_bundle)
        expected_minimum = diagnostic_minimum_input_m15_bars(self.policy_bundle)
        if len(self.m15_bars) < expected_minimum:
            raise DiagnosticReplayDataError(
                "replay result has no complete-hour post-pre-roll decision"
            )
        expected_boundary = self.m15_bars[expected_pre_roll].start_time
        pre_roll_decisions = tuple(
            decision
            for decision in self.decisions
            if decision.m15_bar_start < expected_boundary
        )
        post_pre_roll_decisions = tuple(
            decision
            for decision in self.decisions
            if decision.m15_bar_start >= expected_boundary
        )
        if self.readiness != DiagnosticReplayReadiness(
            history_boundary=ReplayHistoryBoundary.EMPTY_STATE_AT_DATASET_START,
            pre_roll_m15_bars=expected_pre_roll,
            minimum_input_m15_bars=expected_minimum,
            post_pre_roll_m15_start=expected_boundary,
            pre_roll_decision_count=len(pre_roll_decisions),
            post_pre_roll_decision_count=len(post_pre_roll_decisions),
        ):
            raise ValueError("readiness metadata conflicts with replay history boundary")
        _validate_m15_bars(self.m15_bars, self.calendar)
        if tuple(aggregate_m15_to_h1(self.m15_bars, calendar=self.calendar)) != self.h1_bars:
            raise ValueError("H1 bars conflict with the exact M15 aggregation")
        if self.lifecycle.book.policy.calendar != self.calendar:
            raise ValueError("lifecycle calendar conflicts with replay policy")
        if any(group.calendar != self.calendar for group in self.availability_groups):
            raise ValueError("availability group calendar conflicts with replay policy")
        if any(zone.calendar != self.calendar for zone in self.zones):
            raise ValueError("zone calendar conflicts with replay policy")
        if any(decision.calendar != self.calendar for decision in self.decisions):
            raise ValueError("decision calendar conflicts with replay policy")
        provenance_events = (
            self.m15_bars
            + self.h1_bars
            + self.h1_atr
            + self.h1_pivots
            + self.h1_regimes
            + self.h1_impulses
            + self.zones
            + self.m15_ema
            + self.m15_candles
            + self.m15_confirmations
        )
        if any(
            event.availability_basis != self.availability_basis
            for event in provenance_events
        ):
            raise DiagnosticReplayDataError("result mixes M15 availability provenance")
        if any(
            event.policy_fingerprint != self.policy_bundle.impulse_policy.fingerprint
            for event in self.h1_impulses
        ):
            raise ValueError("impulse policy provenance conflicts with bundle")
        if any(
            event.impulse_key[0] != self.policy_bundle.impulse_policy.fingerprint
            or event.origin_policy != self.policy_bundle.origin_policy
            or event.origin_lookback_bars != self.policy_bundle.origin_lookback_bars
            for event in self.zones
        ):
            raise ValueError("zone policy provenance conflicts with bundle")
        if any(
            event.policy_fingerprint != self.policy_bundle.regime_policy.fingerprint
            for event in self.h1_regimes
        ):
            raise ValueError("regime policy provenance conflicts with bundle")
        confirmation_stream = self.m15_ema + self.m15_candles + self.m15_confirmations
        if any(
            event.policy_fingerprint
            != self.policy_bundle.confirmation_policy.fingerprint
            for event in confirmation_stream
        ):
            raise ValueError("confirmation policy provenance conflicts with bundle")
        if (
            self.lifecycle.book.policy_fingerprint
            != self.policy_bundle.lifecycle_policy.fingerprint
        ):
            raise ValueError("lifecycle policy conflicts with policy bundle")

        group_observations = tuple(
            observation
            for group in self.availability_groups
            for observation in group.observations
        )
        expected_observations = {
            (ObservationTimeframe.M15, bar.start_time, bar.timestamp): bar
            for bar in self.m15_bars
        }
        expected_observations.update(
            {
                (ObservationTimeframe.H1, bar.start_time, bar.timestamp): bar
                for bar in self.h1_bars
            }
        )
        expected_observation_keys = set(expected_observations)
        group_observation_keys = tuple(
            (
                observation.timeframe,
                observation.bar.start_time,
                observation.bar.timestamp,
            )
            for observation in group_observations
        )
        if (
            len(group_observation_keys) != len(expected_observation_keys)
            or set(group_observation_keys) != expected_observation_keys
        ):
            raise ValueError("availability groups do not exactly cover input observations")
        if any(
            observation.bar
            != expected_observations[
                (
                    observation.timeframe,
                    observation.bar.start_time,
                    observation.bar.timestamp,
                )
            ]
            for observation in group_observations
        ):
            raise ValueError("availability group observation content conflicts")
        grouped_zones = tuple(
            zone for group in self.availability_groups for zone in group.formations
        )
        zones_by_key = {zone.key: zone for zone in self.zones}
        if (
            len(grouped_zones) != len(self.zones)
            or set(zone.key for zone in grouped_zones) != set(zones_by_key)
            or any(zone != zones_by_key[zone.key] for zone in grouped_zones)
        ):
            raise ValueError("availability groups do not exactly cover zone formations")
        transition_observation_keys = tuple(
            (
                transition.observation.timeframe,
                transition.observation.bar.start_time,
                transition.observation.bar.timestamp,
            )
            for transition in self.lifecycle.transitions
        )
        if (
            len(transition_observation_keys) != len(expected_observation_keys)
            or set(transition_observation_keys) != expected_observation_keys
        ):
            raise ValueError("lifecycle does not exactly cover sealed observations")
        if any(
            transition.observation.bar
            != expected_observations[
                (
                    transition.observation.timeframe,
                    transition.observation.bar.start_time,
                    transition.observation.bar.timestamp,
                )
            ]
            for transition in self.lifecycle.transitions
        ):
            raise ValueError("lifecycle observation content conflicts")
        if (
            set(state.zone.key for state in self.lifecycle.book.states)
            != set(zones_by_key)
            or any(
                state.zone != zones_by_key[state.zone.key]
                for state in self.lifecycle.book.states
            )
        ):
            raise ValueError("lifecycle final book does not exactly cover formations")
        expected_m15 = tuple(
            transition
            for transition in self.lifecycle.transitions
            if transition.observation.timeframe == ObservationTimeframe.M15
        )
        if len(self.decisions) != len(expected_m15):
            raise ValueError("replay must contain one decision per M15 transition")
        confirmation_keys = {event.key for event in self.m15_confirmations}
        regime_keys = {event.key for event in self.h1_regimes}
        for decision, transition in zip(self.decisions, expected_m15):
            bar = transition.observation.bar
            if (
                decision.m15_bar_start != bar.start_time
                or decision.m15_bar_end != bar.timestamp
                or decision.transition_available_at != bar.available_at
                or decision.transition_sealed_through != transition.sealed_through
            ):
                raise ValueError("decision does not bind its exact M15 transition")
            if (
                decision.candidate_policy_fingerprint
                != self.policy_bundle.candidate_policy.fingerprint
                or decision.lifecycle_policy_fingerprint
                != self.policy_bundle.lifecycle_policy.fingerprint
                or decision.regime_policy_fingerprint
                != self.policy_bundle.regime_policy.fingerprint
                or decision.confirmation_policy_fingerprint
                != self.policy_bundle.confirmation_policy.fingerprint
            ):
                raise ValueError("decision policy provenance conflicts with bundle")
            if (
                decision.confirmation is not None
                and decision.confirmation.key not in confirmation_keys
            ):
                raise ValueError("decision confirmation is absent from replay evidence")
            if decision.regime is not None and decision.regime.key not in regime_keys:
                raise ValueError("decision regime is absent from replay evidence")
            if any(
                retest not in self.lifecycle.events
                for retest in decision.selected_retests
            ):
                raise ValueError("decision retest is absent from lifecycle evidence")
            nested_provenance = tuple(
                evidence
                for evidence in (
                    decision.confirmation,
                    decision.regime,
                )
                if evidence is not None
            ) + tuple(retest.zone for retest in decision.selected_retests)
            if any(
                evidence.availability_basis != self.availability_basis
                for evidence in nested_provenance
            ):
                raise DiagnosticReplayDataError(
                    "decision evidence mixes availability provenance"
                )
        if self.as_of is not None:
            availability_times = [
                event.available_at for event in provenance_events
            ]  # type: List[datetime]
            for group in self.availability_groups:
                availability_times.extend((group.available_at, group.sealed_through))
                availability_times.extend(
                    observation.bar.available_at
                    for observation in group.observations
                )
                availability_times.extend(zone.available_at for zone in group.formations)
            if self.lifecycle.book.last_group_available_at is not None:
                availability_times.append(self.lifecycle.book.last_group_available_at)
            if self.lifecycle.book.sealed_through is not None:
                availability_times.append(self.lifecycle.book.sealed_through)
            for transition in self.lifecycle.transitions:
                availability_times.extend(
                    (
                        transition.observation.bar.available_at,
                        transition.sealed_through,
                    )
                )
                availability_times.extend(event.available_at for event in transition.events)
                for state in transition.states_before + transition.states_after:
                    availability_times.append(state.zone.available_at)
                    if state.invalidated_at is not None:
                        availability_times.append(state.invalidated_at)
                    if state.active_episode is not None:
                        availability_times.extend(
                            (
                                state.active_episode.first_touch_available_at,
                                state.active_episode.last_touch_available_at,
                            )
                        )
            for decision in self.decisions:
                availability_times.extend(
                    (
                        decision.available_at,
                        decision.transition_available_at,
                        decision.transition_sealed_through,
                    )
                )
            if any(value > self.as_of for value in availability_times):
                raise ValueError("result contains evidence after as_of")

    @property
    def calendar(self) -> Optional[SessionCalendarArtifact]:
        return self.policy_bundle.calendar

    @property
    def engine_version(self) -> str:
        return (DIAGNOSTIC_REPLAY_ENGINE_VERSION if self.calendar is None
                else SESSION_DIAGNOSTIC_REPLAY_ENGINE_VERSION)

    @property
    def schema_version(self) -> int:
        return (DIAGNOSTIC_REPLAY_SCHEMA_VERSION if self.calendar is None
                else SESSION_DIAGNOSTIC_REPLAY_SCHEMA_VERSION)

    @property
    def policy_bundle_fingerprint(self) -> str:
        return self.policy_bundle.fingerprint

    @property
    def pre_roll_decisions(self) -> Tuple[CandidateDecision, ...]:
        """Decisions retained for audit but not labeled post-pre-roll."""

        boundary = self.readiness.post_pre_roll_m15_start
        return tuple(
            decision
            for decision in self.decisions
            if decision.m15_bar_start < boundary
        )

    @property
    def post_pre_roll_decisions(self) -> Tuple[CandidateDecision, ...]:
        """Dataset-relative decisions at or after the declared history boundary."""

        boundary = self.readiness.post_pre_roll_m15_start
        return tuple(
            decision
            for decision in self.decisions
            if decision.m15_bar_start >= boundary
        )

    @property
    def evidence_fingerprint(self) -> str:
        """Bind every stored bar, event, lifecycle snapshot, and decision."""

        return _stable_digest(
            (
                "diagnostic-replay-evidence-v2",
                self.readiness,
                self.m15_bars,
                self.h1_bars,
                self.h1_atr,
                self.h1_pivots,
                self.h1_regimes,
                self.h1_impulses,
                self.zones,
                self.m15_ema,
                self.m15_candles,
                self.m15_confirmations,
                self.availability_groups,
                self.lifecycle,
                self.decisions,
            )
        )

    @property
    def canonical_identity(self) -> str:
        return ";".join(
            (
                "diagnostic_replay_engine={}".format(
                    self.engine_version
                ),
                "diagnostic_replay_schema={}".format(
                    self.schema_version
                ),
                "dataset={}".format(self.dataset_fingerprint),
                "input_bars={}".format(self.input_bars_fingerprint),
                "policy_bundle={}".format(self.policy_bundle.fingerprint),
                "availability_basis={}".format(self.availability_basis.value),
                "as_of={}".format(
                    "complete-input"
                    if self.as_of is None
                    else self.as_of.isoformat(timespec="microseconds")
                ),
                "evidence={}".format(self.evidence_fingerprint),
            )
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_identity.encode("ascii")).hexdigest()


def run_diagnostic_replay(
    m15_bars: Iterable[QuoteBar],
    *,
    dataset_fingerprint: str,
    policy_bundle: ResearchPolicyBundle,
    as_of: Optional[datetime] = None,
) -> DiagnosticReplayResult:
    """Compose all diagnostic detectors for one immutable M15 data prefix.

    When ``as_of`` is supplied, only the chronological receipt prefix actually
    available by that time is consumed.  A later visible bar behind an earlier
    unavailable bar is rejected, because accepting it would skip the lifecycle
    cadence.  Valid suffix bars not yet available at ``as_of`` are ignored, so
    they cannot rewrite any historical event or decision.
    """

    _validate_sha256(dataset_fingerprint, "dataset_fingerprint")
    if not isinstance(policy_bundle, ResearchPolicyBundle):
        raise TypeError("policy_bundle must be a ResearchPolicyBundle")
    normalized_as_of = _normalize_as_of(as_of)
    supplied = tuple(m15_bars)
    if any(not isinstance(bar, QuoteBar) for bar in supplied):
        raise TypeError("M15 input must contain QuoteBar values")
    source = _visible_prefix(supplied, normalized_as_of)
    calendar = policy_bundle.calendar
    basis = _validate_m15_bars(source, calendar)
    pre_roll = diagnostic_pre_roll_m15_bars(policy_bundle)
    minimum_input = diagnostic_minimum_input_m15_bars(policy_bundle)
    if len(source) < minimum_input:
        raise DiagnosticReplayDataError(
            "insufficient history: replay requires at least {} valid M15 bars "
            "({} pre-roll plus a complete-hour post-pre-roll bar); got {}".format(
                minimum_input,
                pre_roll,
                len(source),
            )
        )

    # The H1 builder is intentionally strict: no trailing M15 bars are silently
    # discarded.  Point-in-time callers choose an ``as_of`` hour boundary.
    h1_bars = aggregate_m15_to_h1(source, calendar=calendar)
    pivots = confirmed_h1_pivots(h1_bars, as_of=normalized_as_of, calendar=calendar)
    regimes = h1_regime_events(
        pivots,
        policy=policy_bundle.regime_policy,
        as_of=normalized_as_of,
    )
    atr = h1_atr_events(
        h1_bars,
        period=policy_bundle.impulse_policy.atr_period,
        method=policy_bundle.impulse_policy.atr_method,
        as_of=normalized_as_of,
        calendar=calendar,
    )
    impulses = h1_impulse_events(
        h1_bars,
        policy=policy_bundle.impulse_policy,
        as_of=normalized_as_of,
    )
    zones = supply_demand_zone_events(
        h1_bars,
        impulse_policy=policy_bundle.impulse_policy,
        origin_policy=policy_bundle.origin_policy,
        origin_lookback_bars=policy_bundle.origin_lookback_bars,
        as_of=normalized_as_of,
    )
    ema = m15_ema_events(
        source,
        policy=policy_bundle.confirmation_policy,
        as_of=normalized_as_of,
    )
    candles = m15_candle_events(
        source,
        policy=policy_bundle.confirmation_policy,
        as_of=normalized_as_of,
    )
    confirmations = m15_confirmation_events(
        source,
        policy=policy_bundle.confirmation_policy,
        as_of=normalized_as_of,
    )

    groups = _availability_groups(source, h1_bars, zones, calendar)
    lifecycle = _fold_lifecycle(
        groups,
        policy_bundle,
        h1_start=h1_bars[0].start_time,
        m15_start=source[0].start_time,
    )
    decisions = paper_candidate_decisions(
        lifecycle,
        regimes,
        confirmations,
        candidate_policy=policy_bundle.candidate_policy,
        regime_policy=policy_bundle.regime_policy,
        confirmation_policy=policy_bundle.confirmation_policy,
        as_of=normalized_as_of,
    )
    post_pre_roll_start = source[pre_roll].start_time
    pre_roll_count = sum(
        decision.m15_bar_start < post_pre_roll_start for decision in decisions
    )
    readiness = DiagnosticReplayReadiness(
        history_boundary=ReplayHistoryBoundary.EMPTY_STATE_AT_DATASET_START,
        pre_roll_m15_bars=pre_roll,
        minimum_input_m15_bars=minimum_input,
        post_pre_roll_m15_start=post_pre_roll_start,
        pre_roll_decision_count=pre_roll_count,
        post_pre_roll_decision_count=len(decisions) - pre_roll_count,
    )
    return DiagnosticReplayResult(
        dataset_fingerprint=dataset_fingerprint,
        input_bars_fingerprint=_bar_fingerprint(source),
        policy_bundle=policy_bundle,
        as_of=normalized_as_of,
        availability_basis=basis,
        readiness=readiness,
        m15_bars=source,
        h1_bars=h1_bars,
        h1_atr=atr,
        h1_pivots=pivots,
        h1_regimes=regimes,
        h1_impulses=impulses,
        zones=zones,
        m15_ema=ema,
        m15_candles=candles,
        m15_confirmations=confirmations,
        availability_groups=groups,
        lifecycle=lifecycle,
        decisions=decisions,
    )


def validate_diagnostic_replay_result(result: DiagnosticReplayResult) -> None:
    """Recompute and equality-check every stream at an export trust boundary.

    Frozen dataclasses prevent in-place mutation and the content fingerprint
    makes any replacement evident.  Persistence/export code should additionally
    call this validator: it rebuilds aggregation, detectors, receipt groups,
    lifecycle snapshots, readiness metadata, and candidate decisions from the
    stored M15 bars and policy bundle, then requires exact dataclass equality.
    """

    if not isinstance(result, DiagnosticReplayResult):
        raise TypeError("result must be a DiagnosticReplayResult")
    expected = run_diagnostic_replay(
        result.m15_bars,
        dataset_fingerprint=result.dataset_fingerprint,
        policy_bundle=result.policy_bundle,
        as_of=result.as_of,
    )
    if result != expected or result.fingerprint != expected.fingerprint:
        raise DiagnosticReplayDataError(
            "diagnostic replay result conflicts with exact recomputation"
        )


__all__ = [
    "DIAGNOSTIC_REPLAY_ENGINE_VERSION",
    "DIAGNOSTIC_REPLAY_SCHEMA_VERSION",
    "SESSION_DIAGNOSTIC_REPLAY_ENGINE_VERSION",
    "SESSION_DIAGNOSTIC_REPLAY_SCHEMA_VERSION",
    "DiagnosticReplayDataError",
    "DiagnosticReplayReadiness",
    "DiagnosticReplayResult",
    "ReplayHistoryBoundary",
    "diagnostic_minimum_input_m15_bars",
    "diagnostic_pre_roll_m15_bars",
    "diagnostic_warmup_m15_bars",
    "run_diagnostic_replay",
    "validate_diagnostic_replay_result",
]
