"""Point-in-time H1 market-structure regime diagnostics.

This module consumes only already-confirmed :class:`H1PivotEvent` values.  It
does not read accounts, place orders, simulate fills, calculate P&L, or attach a
trade meaning to a regime.  Each event is a structural research observation
under a required, fingerprinted policy.

Events are generated in pivot-center order.  Their visibility is gated by the
latest availability timestamp among the exact last-two-high/last-two-low pivot
evidence stored on the event.  Consequently, delayed evidence cannot make a
classification visible early, while appending future pivots cannot rewrite an
already generated structural event.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import math
from typing import Iterable, Optional, Sequence, Tuple

from .domain import AvailabilityBasis
from .multitimeframe import (
    H1_DURATION,
    PIVOT_LEFT_WING,
    PIVOT_RIGHT_WING,
    H1PivotEvent,
    PivotKind,
)


class MarketRegimeDataError(ValueError):
    """Raised when confirmed pivots cannot be consumed without guessing."""


class StructureRule(str, Enum):
    """How confirmed pivot evidence is selected and compared."""

    STRICT_LAST_TWO_CONFIRMED_HIGH_LOW = "strict_last_two_confirmed_high_low"


class InsufficientEvidencePolicy(str, Enum):
    """Classification before two highs and two lows are available."""

    UNKNOWN = "unknown"


class MixedStructurePolicy(str, Enum):
    """Classification for fully evidenced mixed or equal structure."""

    RANGE = "range"


class MarketRegime(str, Enum):
    UNKNOWN = "unknown"
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGE = "range"


class StructureLeg(str, Enum):
    """Direction of one same-kind pair of confirmed pivots."""

    INSUFFICIENT = "insufficient"
    RISING = "rising"
    FALLING = "falling"
    EQUAL = "equal"


PivotEvidenceKey = Tuple[
    PivotKind,
    datetime,
    datetime,
    datetime,
    datetime,
    AvailabilityBasis,
    float,
    float,
    float,
    int,
    int,
]
RegimeKey = Tuple[
    str,
    datetime,
    Tuple[PivotEvidenceKey, ...],
    Tuple[PivotEvidenceKey, ...],
]


@dataclass(frozen=True)
class RegimePolicy:
    """Required semantic choices for H1 structure classification.

    No field has a default: even the first research baseline must state all
    choices rather than inheriting an implicit interpretation.
    """

    structure_rule: StructureRule
    insufficient_evidence: InsufficientEvidencePolicy
    mixed_structure: MixedStructurePolicy

    def __post_init__(self) -> None:
        _require_enum(self.structure_rule, StructureRule, "structure_rule")
        _require_enum(
            self.insufficient_evidence,
            InsufficientEvidencePolicy,
            "insufficient_evidence",
        )
        _require_enum(self.mixed_structure, MixedStructurePolicy, "mixed_structure")

    @property
    def canonical_identity(self) -> str:
        """Complete, versioned identity of every selected policy field."""

        return ";".join(
            (
                "h1-regime-policy-v1",
                "structure_rule={}".format(self.structure_rule.value),
                "insufficient_evidence={}".format(
                    self.insufficient_evidence.value
                ),
                "mixed_structure={}".format(self.mixed_structure.value),
            )
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_identity.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class H1RegimeEvent:
    """One immutable market-structure snapshot at an H1 pivot center.

    ``high_pivots`` and ``low_pivots`` contain at most the exact two pivots of
    each kind used by the classification, ordered from older to newer.  A
    snapshot with fewer than two of either kind is ``UNKNOWN``.  ``available_at``
    is the latest availability of precisely these stored evidence pivots.
    """

    regime: MarketRegime
    high_structure: StructureLeg
    low_structure: StructureLeg
    structure_bar_start: datetime
    structure_bar_end: datetime
    confirmed_at: datetime
    available_at: datetime
    availability_basis: AvailabilityBasis
    structure_rule: StructureRule
    insufficient_evidence: InsufficientEvidencePolicy
    mixed_structure: MixedStructurePolicy
    policy_fingerprint: str
    high_pivots: Tuple[H1PivotEvent, ...]
    low_pivots: Tuple[H1PivotEvent, ...]

    @property
    def key(self) -> RegimeKey:
        """Stable identity binding policy, structure time, and exact evidence."""

        return (
            self.policy_fingerprint,
            self.structure_bar_start,
            tuple(_pivot_key(pivot) for pivot in self.high_pivots),
            tuple(_pivot_key(pivot) for pivot in self.low_pivots),
        )


def _require_enum(value: object, enum_type: object, field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError("{} must be a {}".format(field_name, enum_type.__name__))


def _normalize_as_of(as_of: Optional[datetime]) -> Optional[datetime]:
    if as_of is None:
        return None
    if not isinstance(as_of, datetime):
        raise TypeError("as_of must be a datetime")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return as_of.astimezone(timezone.utc)


def _pivot_sort_key(pivot: H1PivotEvent) -> Tuple[datetime, int]:
    kind_order = 0 if pivot.kind == PivotKind.HIGH else 1
    return (pivot.pivot_bar_start, kind_order)


def _pivot_key(pivot: H1PivotEvent) -> PivotEvidenceKey:
    return (
        pivot.kind,
        pivot.pivot_bar_start,
        pivot.pivot_bar_end,
        pivot.confirmed_at,
        pivot.available_at,
        pivot.availability_basis,
        pivot.bid_price,
        pivot.ask_price,
        pivot.mid_price,
        pivot.left_wing,
        pivot.right_wing,
    )


def _require_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("{} must be a datetime".format(field_name))
    if value.tzinfo != timezone.utc:
        raise MarketRegimeDataError("{} must be normalized to UTC".format(field_name))
    return value


def _validate_pivots(pivots: Sequence[H1PivotEvent]) -> None:
    previous_key: Optional[Tuple[datetime, int]] = None
    seen = set()
    for index, pivot in enumerate(pivots):
        if not isinstance(pivot, H1PivotEvent):
            raise TypeError("pivot input must contain H1PivotEvent values")
        if not isinstance(pivot.kind, PivotKind):
            raise TypeError("pivot {} kind must be a PivotKind".format(index))

        start = _require_utc(pivot.pivot_bar_start, "pivot_bar_start")
        end = _require_utc(pivot.pivot_bar_end, "pivot_bar_end")
        confirmed_at = _require_utc(pivot.confirmed_at, "confirmed_at")
        available_at = _require_utc(pivot.available_at, "available_at")
        if end - start != H1_DURATION:
            raise MarketRegimeDataError(
                "pivot {} must refer to exactly one H1 bar".format(index)
            )
        if start.minute != 0 or start.second != 0 or start.microsecond != 0:
            raise MarketRegimeDataError(
                "pivot {} is not aligned to a UTC hour boundary".format(index)
            )
        if (
            type(pivot.left_wing) is not int
            or type(pivot.right_wing) is not int
            or pivot.left_wing != PIVOT_LEFT_WING
            or pivot.right_wing != PIVOT_RIGHT_WING
        ):
            raise MarketRegimeDataError(
                "pivot {} must use the confirmed 3-left/3-right rule".format(index)
            )
        expected_confirmation = end + PIVOT_RIGHT_WING * H1_DURATION
        if confirmed_at != expected_confirmation:
            raise MarketRegimeDataError(
                "pivot {} confirmation time conflicts with its right wing".format(index)
            )
        if available_at < confirmed_at:
            raise MarketRegimeDataError(
                "pivot {} cannot be available before confirmation".format(index)
            )
        if not isinstance(pivot.availability_basis, AvailabilityBasis):
            raise TypeError(
                "pivot {} availability_basis must be an AvailabilityBasis".format(
                    index
                )
            )

        for field_name in ("bid_price", "ask_price", "mid_price"):
            value = getattr(pivot, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise MarketRegimeDataError(
                    "pivot {} {} must be a finite positive number".format(
                        index, field_name
                    )
                )
        if pivot.bid_price > pivot.ask_price:
            raise MarketRegimeDataError(
                "pivot {} bid_price cannot exceed ask_price".format(index)
            )
        if pivot.mid_price != (pivot.bid_price + pivot.ask_price) / 2.0:
            raise MarketRegimeDataError(
                "pivot {} mid_price must be the arithmetic bid/ask midpoint".format(
                    index
                )
            )

        identity = (pivot.kind, start)
        if identity in seen:
            raise MarketRegimeDataError(
                "duplicate {} pivot at {}".format(pivot.kind.value, start.isoformat())
            )
        seen.add(identity)
        current_key = _pivot_sort_key(pivot)
        if previous_key is not None and current_key <= previous_key:
            raise MarketRegimeDataError(
                "pivots must be ordered by pivot time with high before low on a tie"
            )
        previous_key = current_key


def _structure_leg(pivots: Sequence[H1PivotEvent]) -> StructureLeg:
    if len(pivots) < 2:
        return StructureLeg.INSUFFICIENT
    previous, current = pivots[-2:]
    if current.mid_price > previous.mid_price:
        return StructureLeg.RISING
    if current.mid_price < previous.mid_price:
        return StructureLeg.FALLING
    return StructureLeg.EQUAL


def _classify(
    high_structure: StructureLeg,
    low_structure: StructureLeg,
    policy: RegimePolicy,
) -> MarketRegime:
    if policy.structure_rule != StructureRule.STRICT_LAST_TWO_CONFIRMED_HIGH_LOW:
        raise ValueError("structure_rule is not implemented")
    if (
        high_structure == StructureLeg.INSUFFICIENT
        or low_structure == StructureLeg.INSUFFICIENT
    ):
        if policy.insufficient_evidence == InsufficientEvidencePolicy.UNKNOWN:
            return MarketRegime.UNKNOWN
        raise ValueError("insufficient_evidence policy is not implemented")
    if (
        high_structure == StructureLeg.RISING
        and low_structure == StructureLeg.RISING
    ):
        return MarketRegime.BULLISH
    if (
        high_structure == StructureLeg.FALLING
        and low_structure == StructureLeg.FALLING
    ):
        return MarketRegime.BEARISH
    if policy.mixed_structure == MixedStructurePolicy.RANGE:
        return MarketRegime.RANGE
    raise ValueError("mixed_structure policy is not implemented")


def _availability_gate(
    evidence: Sequence[H1PivotEvent],
) -> H1PivotEvent:
    if not evidence:
        raise ValueError("regime evidence must not be empty")
    ordered = tuple(sorted(evidence, key=_pivot_sort_key))
    # A structurally later pivot deterministically supplies the basis on an
    # exact availability tie.
    return max(
        enumerate(ordered), key=lambda item: (item[1].available_at, item[0])
    )[1]


def h1_regime_events(
    pivots: Iterable[H1PivotEvent],
    *,
    policy: RegimePolicy,
    as_of: Optional[datetime] = None
) -> Tuple[H1RegimeEvent, ...]:
    """Classify H1 structure at each distinct confirmed pivot center.

    The input must be the deterministic chronological output of
    :func:`confirmed_h1_pivots`: pivot centers increase, with a high before a
    low when both occur on one H1 bar.  Same-center pivots are admitted as one
    atomic snapshot, preventing a transient classification that could never be
    observed between simultaneous high/low evidence.

    Bullish requires the newest two confirmed highs and newest two confirmed
    lows to be strictly rising.  Bearish requires both pairs to be strictly
    falling.  With complete evidence every mixed or equal pattern is range;
    before both pairs exist the state is unknown.  ``as_of`` filters solely on
    the exact evidence availability stored by each immutable event.
    """

    if not isinstance(policy, RegimePolicy):
        raise TypeError("policy must be a RegimePolicy")
    normalized_as_of = _normalize_as_of(as_of)
    source = tuple(pivots)
    _validate_pivots(source)

    high_history = []
    low_history = []
    events = []
    index = 0
    while index < len(source):
        group_start = index
        pivot_start = source[index].pivot_bar_start
        while (
            index < len(source)
            and source[index].pivot_bar_start == pivot_start
        ):
            pivot = source[index]
            if pivot.kind == PivotKind.HIGH:
                high_history.append(pivot)
            elif pivot.kind == PivotKind.LOW:
                low_history.append(pivot)
            else:  # Defensive guard for future enum expansion.
                raise ValueError("pivot kind is not implemented")
            index += 1

        trigger_group = source[group_start:index]
        selected_highs = tuple(high_history[-2:])
        selected_lows = tuple(low_history[-2:])
        high_structure = _structure_leg(selected_highs)
        low_structure = _structure_leg(selected_lows)
        regime = _classify(high_structure, low_structure, policy)
        exact_evidence = selected_highs + selected_lows
        gate = _availability_gate(exact_evidence)
        event = H1RegimeEvent(
            regime=regime,
            high_structure=high_structure,
            low_structure=low_structure,
            structure_bar_start=pivot_start,
            structure_bar_end=trigger_group[-1].pivot_bar_end,
            confirmed_at=trigger_group[-1].confirmed_at,
            available_at=gate.available_at,
            availability_basis=gate.availability_basis,
            structure_rule=policy.structure_rule,
            insufficient_evidence=policy.insufficient_evidence,
            mixed_structure=policy.mixed_structure,
            policy_fingerprint=policy.fingerprint,
            high_pivots=selected_highs,
            low_pivots=selected_lows,
        )
        if normalized_as_of is None or event.available_at <= normalized_as_of:
            events.append(event)

    return tuple(events)


__all__ = [
    "H1RegimeEvent",
    "InsufficientEvidencePolicy",
    "MarketRegime",
    "MarketRegimeDataError",
    "MixedStructurePolicy",
    "PivotEvidenceKey",
    "RegimeKey",
    "RegimePolicy",
    "StructureLeg",
    "StructureRule",
    "h1_regime_events",
]
