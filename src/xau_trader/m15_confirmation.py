"""Point-in-time M15 EMA and reversal-candle confirmation diagnostics.

This module implements research events only.  It has no broker, order, account,
backtest, or P&L dependencies.  Every interpretation left open by the strategy
intake is a required :class:`ConfirmationPolicy` value; there are no executable
defaults hidden in the API.

All candle prices are arithmetic bid/ask mid prices.  Input bars define the EMA
seed anchor, are required to be contiguous UTC-aligned M15 bars, and are never
silently bridged across a gap.  Event availability is the latest availability
of every completed bar needed to prove the event.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import math
from typing import Dict, Iterable, Optional, Sequence, Tuple

from .domain import AvailabilityBasis, QuoteBar
from .multitimeframe import M15_DURATION, MultiTimeframeDataError
from .supply_demand import ImpulseDirection


class EmaInitialization(str, Enum):
    """How the first EMA value in the supplied, anchored series is formed."""

    FIRST_CLOSE_SEED = "first_close_seed"
    SMA_PERIOD_SEED = "sma_period_seed"


class CrossoverTiming(str, Enum):
    """Which EMA snapshot the current close must cross.

    ``SAME_BAR_EMA`` is the intake's stated rule: compare the prior close with
    the prior EMA and the current close with the current EMA.
    ``CURRENT_CLOSE_VS_PRIOR_EMA`` keeps the prior comparison unchanged but
    compares the current close with the already-completed prior EMA.
    """

    SAME_BAR_EMA = "same_bar_ema"
    CURRENT_CLOSE_VS_PRIOR_EMA = "current_close_vs_prior_ema"


class EngulfingEquality(str, Enum):
    """Whether equal body boundaries count as body engulfment."""

    INCLUSIVE = "inclusive"
    STRICT_BOTH_BOUNDARIES = "strict_both_boundaries"


class MedianConvention(str, Enum):
    """Deterministic choice for an even-sized displacement history."""

    MEAN_OF_MIDDLE_TWO = "mean_of_middle_two"
    LOWER_MIDDLE = "lower_middle"
    UPPER_MIDDLE = "upper_middle"


class DisplacementHistory(str, Enum):
    """Whether the candidate candle is part of its median reference window."""

    PREVIOUS_BARS_ONLY = "previous_bars_only"
    CURRENT_BAR_INCLUSIVE = "current_bar_inclusive"


class DojiSemantics(str, Enum):
    """Symmetric treatment of an exact open/close equality."""

    NEITHER_DIRECTION = "neither_direction"
    BOTH_DIRECTIONS = "both_directions"


class CandleCombination(str, Enum):
    """Which reversal-candle evidence may prove a confirmation."""

    ENGULFING_ONLY = "engulfing_only"
    DISPLACEMENT_ONLY = "displacement_only"
    ENGULFING_OR_DISPLACEMENT = "engulfing_or_displacement"
    ENGULFING_AND_DISPLACEMENT = "engulfing_and_displacement"


class TouchBarConfirmation(str, Enum):
    """Whether a confirmation completed on the first touch bar is eligible."""

    TOUCH_BAR_ALLOWED = "touch_bar_allowed"
    AFTER_TOUCH_BAR_ONLY = "after_touch_bar_only"


class CandlePatternKind(str, Enum):
    ENGULFING = "engulfing"
    DISPLACEMENT = "displacement"


EmaKey = Tuple[
    str,
    datetime,
    datetime,
    datetime,
    AvailabilityBasis,
    int,
    EmaInitialization,
    datetime,
    float,
    float,
]
CandlePatternKey = Tuple[
    str,
    ImpulseDirection,
    CandlePatternKind,
    datetime,
    datetime,
    datetime,
    datetime,
    AvailabilityBasis,
    float,
    float,
    float,
    datetime,
    datetime,
]
ConfirmationKey = Tuple[
    str,
    ImpulseDirection,
    datetime,
    datetime,
    datetime,
    datetime,
    AvailabilityBasis,
    int,
    EmaInitialization,
    CrossoverTiming,
    float,
    float,
    float,
    float,
    float,
    Tuple[CandlePatternKind, ...],
    CandleCombination,
    TouchBarConfirmation,
    int,
]


_MAX_POLICY_BARS = 10_000


@dataclass(frozen=True)
class ConfirmationPolicy:
    """Complete, required policy surface for the M15 confirmation experiment."""

    ema_period: int
    ema_initialization: EmaInitialization
    crossover_timing: CrossoverTiming
    engulfing_equality: EngulfingEquality
    displacement_lookback_bars: int
    displacement_median: MedianConvention
    displacement_history: DisplacementHistory
    displacement_body_multiple: float
    doji_semantics: DojiSemantics
    candle_combination: CandleCombination
    touch_bar_confirmation: TouchBarConfirmation
    confirmation_expiry_bars: int

    def __post_init__(self) -> None:
        _require_positive_integer(self.ema_period, "ema_period")
        _require_enum(
            self.ema_initialization, EmaInitialization, "ema_initialization"
        )
        _require_enum(self.crossover_timing, CrossoverTiming, "crossover_timing")
        _require_enum(
            self.engulfing_equality, EngulfingEquality, "engulfing_equality"
        )
        _require_positive_integer(
            self.displacement_lookback_bars, "displacement_lookback_bars"
        )
        _require_enum(
            self.displacement_median, MedianConvention, "displacement_median"
        )
        _require_enum(
            self.displacement_history, DisplacementHistory, "displacement_history"
        )
        _require_non_negative_finite(
            self.displacement_body_multiple, "displacement_body_multiple"
        )
        _require_enum(self.doji_semantics, DojiSemantics, "doji_semantics")
        _require_enum(
            self.candle_combination, CandleCombination, "candle_combination"
        )
        _require_enum(
            self.touch_bar_confirmation,
            TouchBarConfirmation,
            "touch_bar_confirmation",
        )
        _require_non_negative_integer(
            self.confirmation_expiry_bars, "confirmation_expiry_bars"
        )
        if (
            self.touch_bar_confirmation
            == TouchBarConfirmation.AFTER_TOUCH_BAR_ONLY
            and self.confirmation_expiry_bars < 1
        ):
            raise ValueError(
                "confirmation_expiry_bars must be at least 1 when the touch bar "
                "is excluded"
            )

    @property
    def canonical_identity(self) -> str:
        """Versioned identity containing every selected policy field."""

        return ";".join(
            (
                "m15-confirmation-policy-v1",
                "ema_period={}".format(self.ema_period),
                "ema_initialization={}".format(self.ema_initialization.value),
                "crossover_timing={}".format(self.crossover_timing.value),
                "engulfing_equality={}".format(self.engulfing_equality.value),
                "displacement_lookback_bars={}".format(
                    self.displacement_lookback_bars
                ),
                "displacement_median={}".format(self.displacement_median.value),
                "displacement_history={}".format(self.displacement_history.value),
                "displacement_body_multiple={}".format(
                    _canonical_number(self.displacement_body_multiple)
                ),
                "doji_semantics={}".format(self.doji_semantics.value),
                "candle_combination={}".format(self.candle_combination.value),
                "touch_bar_confirmation={}".format(
                    self.touch_bar_confirmation.value
                ),
                "confirmation_expiry_bars={}".format(
                    self.confirmation_expiry_bars
                ),
            )
        )

    @property
    def fingerprint(self) -> str:
        """SHA-256 fingerprint of the complete confirmation policy."""

        return hashlib.sha256(self.canonical_identity.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class EmaEvent:
    """One completed M15 EMA value and its exact point-in-time gate."""

    bar_start: datetime
    bar_end: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    policy_fingerprint: str
    period: int
    initialization: EmaInitialization
    seed_bar_start: datetime
    close: float
    value: float

    @property
    def key(self) -> EmaKey:
        return (
            self.policy_fingerprint,
            self.bar_start,
            self.bar_end,
            self.available_at,
            self.availability_basis,
            self.period,
            self.initialization,
            self.seed_bar_start,
            self.close,
            self.value,
        )


@dataclass(frozen=True)
class CandlePatternEvent:
    """One positive engulfing or displacement observation on an M15 bar."""

    direction: ImpulseDirection
    kind: CandlePatternKind
    bar_start: datetime
    bar_end: datetime
    confirmed_at: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    policy_fingerprint: str
    body_size: float
    reference_value: float
    required_body_size: float
    reference_window_start: datetime
    reference_window_end: datetime

    @property
    def key(self) -> CandlePatternKey:
        return (
            self.policy_fingerprint,
            self.direction,
            self.kind,
            self.bar_start,
            self.bar_end,
            self.confirmed_at,
            self.available_at,
            self.availability_basis,
            self.body_size,
            self.reference_value,
            self.required_body_size,
            self.reference_window_start,
            self.reference_window_end,
        )


@dataclass(frozen=True)
class ConfirmationEvent:
    """EMA crossover plus configured reversal-candle evidence on one M15 bar."""

    direction: ImpulseDirection
    bar_start: datetime
    bar_end: datetime
    confirmed_at: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    policy_fingerprint: str
    ema_period: int
    ema_initialization: EmaInitialization
    crossover_timing: CrossoverTiming
    prior_close: float
    prior_ema: float
    current_close: float
    current_ema: float
    current_crossover_reference: float
    proving_patterns: Tuple[CandlePatternKind, ...]
    candle_combination: CandleCombination
    touch_bar_confirmation: TouchBarConfirmation
    confirmation_expiry_bars: int

    @property
    def key(self) -> ConfirmationKey:
        return (
            self.policy_fingerprint,
            self.direction,
            self.bar_start,
            self.bar_end,
            self.confirmed_at,
            self.available_at,
            self.availability_basis,
            self.ema_period,
            self.ema_initialization,
            self.crossover_timing,
            self.prior_close,
            self.prior_ema,
            self.current_close,
            self.current_ema,
            self.current_crossover_reference,
            self.proving_patterns,
            self.candle_combination,
            self.touch_bar_confirmation,
            self.confirmation_expiry_bars,
        )


@dataclass(frozen=True)
class _EmaCalculation:
    event: EmaEvent
    dependency_start_index: int


@dataclass(frozen=True)
class _PatternCalculation:
    event: CandlePatternEvent
    dependency_start_index: int


def _require_enum(value: object, enum_type: object, field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError("{} must be a {}".format(field_name, enum_type.__name__))


def _require_positive_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("{} must be an integer".format(field_name))
    if value < 1:
        raise ValueError("{} must be at least 1".format(field_name))
    if value > _MAX_POLICY_BARS:
        raise ValueError(
            "{} must not exceed {}".format(field_name, _MAX_POLICY_BARS)
        )


def _require_non_negative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("{} must be an integer".format(field_name))
    if value < 0:
        raise ValueError("{} must be non-negative".format(field_name))
    if value > _MAX_POLICY_BARS:
        raise ValueError(
            "{} must not exceed {}".format(field_name, _MAX_POLICY_BARS)
        )


def _require_non_negative_finite(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("{} must be a number".format(field_name))
    try:
        finite = math.isfinite(value)
    except OverflowError as exc:
        raise ValueError(
            "{} must be a finite non-negative number".format(field_name)
        ) from exc
    if not finite or value < 0:
        raise ValueError("{} must be a finite non-negative number".format(field_name))


def _canonical_number(value: float) -> str:
    sign, digits, exponent = Decimal(str(value)).as_tuple()
    values = list(digits)
    while len(values) > 1 and values[-1] == 0:
        values.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in values)
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


def _validate_m15_bars(bars: Sequence[QuoteBar]) -> None:
    for index, bar in enumerate(bars):
        if not isinstance(bar, QuoteBar):
            raise TypeError("M15 input must contain QuoteBar values")
        for field_name in ("start_time", "timestamp", "available_at"):
            timestamp = getattr(bar, field_name)
            if not isinstance(timestamp, datetime):
                raise TypeError(
                    "M15 bar {} {} must be a datetime".format(index, field_name)
                )
            if timestamp.tzinfo != timezone.utc:
                raise MultiTimeframeDataError(
                    "M15 bar {} {} must be normalized to UTC".format(
                        index, field_name
                    )
                )
        if not isinstance(bar.availability_basis, AvailabilityBasis):
            raise TypeError(
                "M15 bar {} availability_basis must be an AvailabilityBasis".format(
                    index
                )
            )
        if bar.available_at < bar.timestamp:
            raise MultiTimeframeDataError(
                "M15 bar {} availability cannot precede its completed timestamp".format(
                    index
                )
            )
        if bar.timestamp - bar.start_time != M15_DURATION:
            raise MultiTimeframeDataError(
                "M15 bar {} must span exactly 15 minutes".format(index)
            )
        if (
            bar.start_time.minute % 15
            or bar.start_time.second
            or bar.start_time.microsecond
        ):
            raise MultiTimeframeDataError(
                "M15 bar {} is not aligned to a UTC 15-minute boundary".format(index)
            )
        if index and bar.start_time != bars[index - 1].timestamp:
            raise MultiTimeframeDataError(
                "M15 bar {} is not contiguous with the previous bar".format(index)
            )

        price_fields = (
            "bid_open",
            "bid_high",
            "bid_low",
            "bid_close",
            "ask_open",
            "ask_high",
            "ask_low",
            "ask_close",
        )
        for field_name in price_fields:
            value = getattr(bar, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(
                    "M15 bar {} {} must be a number".format(index, field_name)
                )
            if not math.isfinite(value) or value <= 0:
                raise MultiTimeframeDataError(
                    "M15 bar {} {} must be finite and positive".format(
                        index, field_name
                    )
                )

        if bar.bid_high < max(bar.bid_open, bar.bid_close):
            raise MultiTimeframeDataError(
                "M15 bar {} bid_high is below bid open/close".format(index)
            )
        if bar.bid_low > min(bar.bid_open, bar.bid_close):
            raise MultiTimeframeDataError(
                "M15 bar {} bid_low is above bid open/close".format(index)
            )
        if bar.ask_high < max(bar.ask_open, bar.ask_close):
            raise MultiTimeframeDataError(
                "M15 bar {} ask_high is below ask open/close".format(index)
            )
        if bar.ask_low > min(bar.ask_open, bar.ask_close):
            raise MultiTimeframeDataError(
                "M15 bar {} ask_low is above ask open/close".format(index)
            )
        for suffix in ("open", "high", "low", "close"):
            bid = getattr(bar, "bid_{}".format(suffix))
            ask = getattr(bar, "ask_{}".format(suffix))
            if bid > ask:
                raise MultiTimeframeDataError(
                    "M15 bar {} bid_{} cannot exceed ask_{}".format(
                        index, suffix, suffix
                    )
                )
            midpoint = (bid + ask) / 2.0
            if not math.isfinite(midpoint) or midpoint <= 0:
                raise MultiTimeframeDataError(
                    "M15 bar {} {} midpoint must be finite and positive".format(
                        index, suffix
                    )
                )

        if bar.volume is not None:
            if isinstance(bar.volume, bool) or not isinstance(
                bar.volume, (int, float)
            ):
                raise TypeError(
                    "M15 bar {} volume must be a number or None".format(index)
                )
            if not math.isfinite(bar.volume) or bar.volume < 0:
                raise MultiTimeframeDataError(
                    "M15 bar {} volume must be finite and non-negative".format(index)
                )


def _mid_open(bar: QuoteBar) -> float:
    return (bar.bid_open + bar.ask_open) / 2.0


def _mid_close(bar: QuoteBar) -> float:
    return (bar.bid_close + bar.ask_close) / 2.0


def _body_size(bar: QuoteBar) -> float:
    return abs(_mid_close(bar) - _mid_open(bar))


def _availability_gate(bars: Sequence[QuoteBar]) -> QuoteBar:
    # The later source bar wins a timestamp tie, preserving deterministic basis.
    return max(enumerate(bars), key=lambda item: (item[1].available_at, item[0]))[1]


def _ema_calculations(
    bars: Sequence[QuoteBar], policy: ConfirmationPolicy
) -> Tuple[Optional[_EmaCalculation], ...]:
    calculations = [None] * len(bars)  # type: list
    if not bars:
        return tuple(calculations)

    period = policy.ema_period
    alpha = 2.0 / (period + 1.0)
    if policy.ema_initialization == EmaInitialization.FIRST_CLOSE_SEED:
        seed_index = 0
        previous = _mid_close(bars[0])
    elif policy.ema_initialization == EmaInitialization.SMA_PERIOD_SEED:
        if len(bars) < period:
            return tuple(calculations)
        seed_index = period - 1
        previous = math.fsum(_mid_close(bar) for bar in bars[:period]) / period
    else:
        raise ValueError("ema_initialization is not implemented")

    for index in range(seed_index, len(bars)):
        close = _mid_close(bars[index])
        if index != seed_index:
            previous = alpha * close + (1.0 - alpha) * previous
        gate = _availability_gate(bars[: index + 1])
        event = EmaEvent(
            bar_start=bars[index].start_time,
            bar_end=bars[index].timestamp,
            available_at=gate.available_at,
            availability_basis=gate.availability_basis,
            policy_fingerprint=policy.fingerprint,
            period=period,
            initialization=policy.ema_initialization,
            seed_bar_start=bars[0].start_time,
            close=close,
            value=previous,
        )
        calculations[index] = _EmaCalculation(
            event=event,
            dependency_start_index=0,
        )
    return tuple(calculations)


def m15_ema_events(
    bars: Iterable[QuoteBar],
    *,
    policy: ConfirmationPolicy,
    as_of: Optional[datetime] = None
) -> Tuple[EmaEvent, ...]:
    """Return EMA snapshots under the selected seed convention.

    A first-close seed emits from the first supplied bar.  An SMA-period seed
    emits first on bar ``period``.  Recursive EMA availability retains the full
    seed-to-current dependency chain.
    """

    if not isinstance(policy, ConfirmationPolicy):
        raise TypeError("policy must be a ConfirmationPolicy")
    normalized_as_of = _normalize_as_of(as_of)
    source = tuple(bars)
    _validate_m15_bars(source)
    calculations = _ema_calculations(source, policy)
    return tuple(
        calculation.event
        for calculation in calculations
        if calculation is not None
        and (
            normalized_as_of is None
            or calculation.event.available_at <= normalized_as_of
        )
    )


def _directions(
    bar: QuoteBar, semantics: DojiSemantics
) -> Tuple[ImpulseDirection, ...]:
    open_price = _mid_open(bar)
    close = _mid_close(bar)
    if close > open_price:
        return (ImpulseDirection.BULLISH,)
    if close < open_price:
        return (ImpulseDirection.BEARISH,)
    if semantics == DojiSemantics.NEITHER_DIRECTION:
        return ()
    if semantics == DojiSemantics.BOTH_DIRECTIONS:
        return (ImpulseDirection.BULLISH, ImpulseDirection.BEARISH)
    raise ValueError("doji_semantics is not implemented")


def _median(values: Sequence[float], convention: MedianConvention) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    if convention == MedianConvention.MEAN_OF_MIDDLE_TWO:
        return (ordered[middle - 1] + ordered[middle]) / 2.0
    if convention == MedianConvention.LOWER_MIDDLE:
        return ordered[middle - 1]
    if convention == MedianConvention.UPPER_MIDDLE:
        return ordered[middle]
    raise ValueError("displacement_median is not implemented")


def _engulfs(
    previous: QuoteBar, current: QuoteBar, equality: EngulfingEquality
) -> bool:
    previous_lower = min(_mid_open(previous), _mid_close(previous))
    previous_upper = max(_mid_open(previous), _mid_close(previous))
    current_lower = min(_mid_open(current), _mid_close(current))
    current_upper = max(_mid_open(current), _mid_close(current))
    if equality == EngulfingEquality.INCLUSIVE:
        return current_lower <= previous_lower and current_upper >= previous_upper
    if equality == EngulfingEquality.STRICT_BOTH_BOUNDARIES:
        return current_lower < previous_lower and current_upper > previous_upper
    raise ValueError("engulfing_equality is not implemented")


def _pattern_calculations(
    bars: Sequence[QuoteBar], policy: ConfirmationPolicy
) -> Tuple[_PatternCalculation, ...]:
    calculations = []
    for index, current in enumerate(bars):
        current_directions = _directions(current, policy.doji_semantics)
        if index:
            previous = bars[index - 1]
            previous_directions = _directions(previous, policy.doji_semantics)
            if _engulfs(previous, current, policy.engulfing_equality):
                gate = _availability_gate(bars[index - 1 : index + 1])
                for direction in current_directions:
                    opposite = (
                        ImpulseDirection.BEARISH
                        if direction == ImpulseDirection.BULLISH
                        else ImpulseDirection.BULLISH
                    )
                    if opposite not in previous_directions:
                        continue
                    calculations.append(
                        _PatternCalculation(
                            event=CandlePatternEvent(
                                direction=direction,
                                kind=CandlePatternKind.ENGULFING,
                                bar_start=current.start_time,
                                bar_end=current.timestamp,
                                confirmed_at=current.timestamp,
                                available_at=gate.available_at,
                                availability_basis=gate.availability_basis,
                                policy_fingerprint=policy.fingerprint,
                                body_size=_body_size(current),
                                reference_value=_body_size(previous),
                                required_body_size=_body_size(previous),
                                reference_window_start=previous.start_time,
                                reference_window_end=previous.timestamp,
                            ),
                            dependency_start_index=index - 1,
                        )
                    )

        lookback = policy.displacement_lookback_bars
        if policy.displacement_history == DisplacementHistory.PREVIOUS_BARS_ONLY:
            if index < lookback:
                continue
            reference_start = index - lookback
            reference_end = index
        elif (
            policy.displacement_history
            == DisplacementHistory.CURRENT_BAR_INCLUSIVE
        ):
            if index + 1 < lookback:
                continue
            reference_start = index - lookback + 1
            reference_end = index + 1
        else:
            raise ValueError("displacement_history is not implemented")

        reference_bars = bars[reference_start:reference_end]
        median = _median(
            tuple(_body_size(bar) for bar in reference_bars),
            policy.displacement_median,
        )
        required = policy.displacement_body_multiple * median
        body = _body_size(current)
        if body < required:
            continue
        dependencies = bars[reference_start : index + 1]
        gate = _availability_gate(dependencies)
        for direction in current_directions:
            calculations.append(
                _PatternCalculation(
                    event=CandlePatternEvent(
                        direction=direction,
                        kind=CandlePatternKind.DISPLACEMENT,
                        bar_start=current.start_time,
                        bar_end=current.timestamp,
                        confirmed_at=current.timestamp,
                        available_at=gate.available_at,
                        availability_basis=gate.availability_basis,
                        policy_fingerprint=policy.fingerprint,
                        body_size=body,
                        reference_value=median,
                        required_body_size=required,
                        reference_window_start=reference_bars[0].start_time,
                        reference_window_end=reference_bars[-1].timestamp,
                    ),
                    dependency_start_index=reference_start,
                )
            )
    return tuple(calculations)


def m15_candle_events(
    bars: Iterable[QuoteBar],
    *,
    policy: ConfirmationPolicy,
    as_of: Optional[datetime] = None
) -> Tuple[CandlePatternEvent, ...]:
    """Return positive bullish and bearish M15 candle-pattern events.

    Engulfment compares body intervals, not wicks.  Displacement uses the
    selected exact median convention and history-inclusion policy.  ``as_of``
    filters on evidence availability rather than candle close time.
    """

    if not isinstance(policy, ConfirmationPolicy):
        raise TypeError("policy must be a ConfirmationPolicy")
    normalized_as_of = _normalize_as_of(as_of)
    source = tuple(bars)
    _validate_m15_bars(source)
    calculations = _pattern_calculations(source, policy)
    return tuple(
        calculation.event
        for calculation in calculations
        if normalized_as_of is None
        or calculation.event.available_at <= normalized_as_of
    )


def _is_crossover(
    direction: ImpulseDirection,
    *,
    prior_close: float,
    prior_ema: float,
    current_close: float,
    current_ema: float,
    timing: CrossoverTiming
) -> bool:
    current_reference = (
        current_ema
        if timing == CrossoverTiming.SAME_BAR_EMA
        else prior_ema
        if timing == CrossoverTiming.CURRENT_CLOSE_VS_PRIOR_EMA
        else None
    )
    if current_reference is None:
        raise ValueError("crossover_timing is not implemented")
    if direction == ImpulseDirection.BULLISH:
        return prior_close <= prior_ema and current_close > current_reference
    return prior_close >= prior_ema and current_close < current_reference


def _selected_pattern_calculations(
    matches: Dict[CandlePatternKind, _PatternCalculation],
    *,
    combination: CandleCombination,
    ema_dependency_start: int,
    current_index: int,
    source: Sequence[QuoteBar]
) -> Tuple[_PatternCalculation, ...]:
    engulfing = matches.get(CandlePatternKind.ENGULFING)
    displacement = matches.get(CandlePatternKind.DISPLACEMENT)
    if combination == CandleCombination.ENGULFING_ONLY:
        return () if engulfing is None else (engulfing,)
    if combination == CandleCombination.DISPLACEMENT_ONLY:
        return () if displacement is None else (displacement,)
    if combination == CandleCombination.ENGULFING_AND_DISPLACEMENT:
        if engulfing is None or displacement is None:
            return ()
        return (engulfing, displacement)
    if combination == CandleCombination.ENGULFING_OR_DISPLACEMENT:
        candidates = tuple(
            item for item in (engulfing, displacement) if item is not None
        )
        if not candidates:
            return ()
        gates = []
        for item in candidates:
            start = min(ema_dependency_start, item.dependency_start_index)
            gate = _availability_gate(source[start : current_index + 1])
            gates.append((gate.available_at, item))
        earliest = min(available_at for available_at, _item in gates)
        # Include every proof path actually visible at the earliest event time.
        return tuple(
            item
            for available_at, item in gates
            if available_at == earliest
        )
    raise ValueError("candle_combination is not implemented")


def m15_confirmation_events(
    bars: Iterable[QuoteBar],
    *,
    policy: ConfirmationPolicy,
    as_of: Optional[datetime] = None
) -> Tuple[ConfirmationEvent, ...]:
    """Return point-in-time EMA-cross-plus-candle confirmation events.

    One event is emitted per bar and direction.  Under an OR policy, the event
    is gated by the earliest complete qualifying proof path and records only the
    pattern evidence available at that time; later evidence never mutates it.
    """

    if not isinstance(policy, ConfirmationPolicy):
        raise TypeError("policy must be a ConfirmationPolicy")
    normalized_as_of = _normalize_as_of(as_of)
    source = tuple(bars)
    _validate_m15_bars(source)
    ema = _ema_calculations(source, policy)
    pattern_calculations = _pattern_calculations(source, policy)
    patterns_by_bar_direction = {}  # type: Dict[Tuple[int, ImpulseDirection], Dict[CandlePatternKind, _PatternCalculation]]
    indexes = {bar.start_time: index for index, bar in enumerate(source)}
    for calculation in pattern_calculations:
        index = indexes[calculation.event.bar_start]
        patterns_by_bar_direction.setdefault(
            (index, calculation.event.direction), {}
        )[calculation.event.kind] = calculation

    events = []
    for index in range(1, len(source)):
        prior_ema = ema[index - 1]
        current_ema = ema[index]
        if prior_ema is None or current_ema is None:
            continue
        prior_close = _mid_close(source[index - 1])
        current_close = _mid_close(source[index])
        for direction in (ImpulseDirection.BULLISH, ImpulseDirection.BEARISH):
            if not _is_crossover(
                direction,
                prior_close=prior_close,
                prior_ema=prior_ema.event.value,
                current_close=current_close,
                current_ema=current_ema.event.value,
                timing=policy.crossover_timing,
            ):
                continue
            selected_patterns = _selected_pattern_calculations(
                patterns_by_bar_direction.get((index, direction), {}),
                combination=policy.candle_combination,
                ema_dependency_start=current_ema.dependency_start_index,
                current_index=index,
                source=source,
            )
            if not selected_patterns:
                continue
            dependency_start = min(
                (current_ema.dependency_start_index,)
                + tuple(item.dependency_start_index for item in selected_patterns)
            )
            gate = _availability_gate(source[dependency_start : index + 1])
            current_reference = (
                current_ema.event.value
                if policy.crossover_timing == CrossoverTiming.SAME_BAR_EMA
                else prior_ema.event.value
            )
            event = ConfirmationEvent(
                direction=direction,
                bar_start=source[index].start_time,
                bar_end=source[index].timestamp,
                confirmed_at=source[index].timestamp,
                available_at=gate.available_at,
                availability_basis=gate.availability_basis,
                policy_fingerprint=policy.fingerprint,
                ema_period=policy.ema_period,
                ema_initialization=policy.ema_initialization,
                crossover_timing=policy.crossover_timing,
                prior_close=prior_close,
                prior_ema=prior_ema.event.value,
                current_close=current_close,
                current_ema=current_ema.event.value,
                current_crossover_reference=current_reference,
                proving_patterns=tuple(
                    item.event.kind for item in selected_patterns
                ),
                candle_combination=policy.candle_combination,
                touch_bar_confirmation=policy.touch_bar_confirmation,
                confirmation_expiry_bars=policy.confirmation_expiry_bars,
            )
            if normalized_as_of is None or event.available_at <= normalized_as_of:
                events.append(event)
    return tuple(events)


def confirmation_time_eligible_for_touch(
    event: ConfirmationEvent,
    *,
    touch_bar_start: datetime,
    touch_bar_end: datetime,
    policy: ConfirmationPolicy
) -> bool:
    """Return whether a confirmation bar lies inside the selected touch window.

    ``confirmation_expiry_bars`` is the maximum number of M15 bar intervals
    between the touch bar's end and confirmation bar's end.  Zero therefore
    means touch-bar-only when touch-bar confirmation is allowed.  This helper
    checks structural timing only; integration must also gate the retest and
    confirmation by their availability timestamps.
    """

    if not isinstance(event, ConfirmationEvent):
        raise TypeError("event must be a ConfirmationEvent")
    if not isinstance(policy, ConfirmationPolicy):
        raise TypeError("policy must be a ConfirmationPolicy")
    if event.policy_fingerprint != policy.fingerprint:
        raise ValueError("event policy fingerprint conflicts with policy")
    _validate_m15_interval(event.bar_start, event.bar_end, "confirmation bar")
    _validate_m15_interval(touch_bar_start, touch_bar_end, "touch bar")
    intervals = (event.bar_end - touch_bar_end) / M15_DURATION
    if not float(intervals).is_integer():
        return False
    minimum = (
        0
        if policy.touch_bar_confirmation
        == TouchBarConfirmation.TOUCH_BAR_ALLOWED
        else 1
    )
    return minimum <= intervals <= policy.confirmation_expiry_bars


def _validate_m15_interval(start: datetime, end: datetime, label: str) -> None:
    for value, field_name in ((start, "start"), (end, "end")):
        if not isinstance(value, datetime):
            raise TypeError("{} {} must be a datetime".format(label, field_name))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("{} {} must include a timezone".format(label, field_name))
        if value.astimezone(timezone.utc) != value or value.tzinfo != timezone.utc:
            raise ValueError("{} must be normalized to UTC".format(label))
    if end - start != M15_DURATION:
        raise ValueError("{} must span exactly 15 minutes".format(label))
    if start.minute % 15 or start.second or start.microsecond:
        raise ValueError("{} must align to a UTC 15-minute boundary".format(label))


__all__ = [
    "CandleCombination",
    "CandlePatternEvent",
    "CandlePatternKey",
    "CandlePatternKind",
    "ConfirmationEvent",
    "ConfirmationKey",
    "ConfirmationPolicy",
    "CrossoverTiming",
    "DisplacementHistory",
    "DojiSemantics",
    "EmaEvent",
    "EmaInitialization",
    "EmaKey",
    "EngulfingEquality",
    "MedianConvention",
    "TouchBarConfirmation",
    "confirmation_time_eligible_for_touch",
    "m15_candle_events",
    "m15_confirmation_events",
    "m15_ema_events",
]
