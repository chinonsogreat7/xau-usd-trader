"""One named, provisional policy bundle for diagnostic XAU/USD research.

The supported detector variants are capabilities, not strategy approval.  This
module makes the first operator hypothesis explicit so hand reconciliation can
refer to one immutable bundle instead of reconstructing choices at call sites.
It contains no data loading, candidate execution, broker, order, fill, sizing,
account, backtest, or P&L behavior.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib

from .candidate_signals import (
    CandidatePolicy,
    EntryTiming,
    RegimeAlignment,
    RetestValidity,
)
from .m15_confirmation import (
    CandleCombination,
    ConfirmationPolicy,
    CrossoverTiming,
    DisplacementHistory,
    DojiSemantics,
    EmaInitialization,
    EngulfingEquality,
    MedianConvention,
    TouchBarConfirmation,
)
from .market_regime import (
    InsufficientEvidencePolicy,
    MixedStructurePolicy,
    RegimePolicy,
    StructureRule,
)
from .supply_demand import (
    AtrMethod,
    AtrTiming,
    ImpulseMovementPolicy,
    ImpulsePolicy,
    OriginSelectionPolicy,
)
from .zone_lifecycle import (
    EqualTimeOrder,
    OverlapSelection,
    RetestPolicy,
    TouchEpisodePolicy,
    ZoneLifecyclePolicy,
    ZonePriceBasis,
)


OPERATOR_DOCUMENT_SHA256 = (
    "392d911ac1ed1b11d0235999aa8f8399e1d7bceaa012cb6c70e23f5bb9cc292f"
)


class BaselineStatus(str, Enum):
    """Review state; deliberately has no approved or production value."""

    PROVISIONAL_UNAPPROVED = "provisional_unapproved"


@dataclass(frozen=True)
class ResearchPolicyBundle:
    """Fully bound policies for one named diagnostic research revision."""

    baseline_id: str
    revision: int
    status: BaselineStatus
    source_sha256: str
    impulse_policy: ImpulsePolicy
    origin_policy: OriginSelectionPolicy
    origin_lookback_bars: int
    lifecycle_policy: ZoneLifecyclePolicy
    regime_policy: RegimePolicy
    confirmation_policy: ConfirmationPolicy
    candidate_policy: CandidatePolicy

    def __post_init__(self) -> None:
        if not isinstance(self.baseline_id, str) or not self.baseline_id:
            raise ValueError("baseline_id must be a non-empty string")
        if any(
            not (character.islower() or character.isdigit() or character == "-")
            for character in self.baseline_id
        ):
            raise ValueError("baseline_id must be a lowercase slug")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("revision must be an integer")
        if self.revision < 1:
            raise ValueError("revision must be positive")
        if not isinstance(self.status, BaselineStatus):
            raise TypeError("status must be a BaselineStatus")
        _validate_sha256(self.source_sha256, "source_sha256")
        if not isinstance(self.impulse_policy, ImpulsePolicy):
            raise TypeError("impulse_policy must be an ImpulsePolicy")
        if not isinstance(self.origin_policy, OriginSelectionPolicy):
            raise TypeError("origin_policy must be an OriginSelectionPolicy")
        if isinstance(self.origin_lookback_bars, bool) or not isinstance(
            self.origin_lookback_bars, int
        ):
            raise TypeError("origin_lookback_bars must be an integer")
        if self.origin_lookback_bars < 1:
            raise ValueError("origin_lookback_bars must be positive")
        if not isinstance(self.lifecycle_policy, ZoneLifecyclePolicy):
            raise TypeError("lifecycle_policy must be a ZoneLifecyclePolicy")
        if not isinstance(self.regime_policy, RegimePolicy):
            raise TypeError("regime_policy must be a RegimePolicy")
        if not isinstance(self.confirmation_policy, ConfirmationPolicy):
            raise TypeError("confirmation_policy must be a ConfirmationPolicy")
        if not isinstance(self.candidate_policy, CandidatePolicy):
            raise TypeError("candidate_policy must be a CandidatePolicy")
        if (
            self.candidate_policy.expected_impulse_policy_fingerprint
            != self.impulse_policy.fingerprint
        ):
            raise ValueError("candidate policy must bind the bundle impulse policy")
        if self.candidate_policy.expected_origin_policy != self.origin_policy:
            raise ValueError("candidate policy must bind the bundle origin policy")
        if (
            self.candidate_policy.expected_origin_lookback_bars
            != self.origin_lookback_bars
        ):
            raise ValueError("candidate policy must bind the bundle origin lookback")

    @property
    def canonical_identity(self) -> str:
        """Versioned identity binding the source and every child policy."""

        return ";".join(
            (
                "xauusd-research-policy-bundle-v1",
                "baseline_id={}".format(self.baseline_id),
                "revision={}".format(self.revision),
                "status={}".format(self.status.value),
                "source_sha256={}".format(self.source_sha256),
                "impulse={}".format(self.impulse_policy.fingerprint),
                "origin_policy={}".format(self.origin_policy.value),
                "origin_lookback_bars={}".format(self.origin_lookback_bars),
                "lifecycle={}".format(self.lifecycle_policy.fingerprint),
                "regime={}".format(self.regime_policy.fingerprint),
                "confirmation={}".format(self.confirmation_policy.fingerprint),
                "candidate={}".format(self.candidate_policy.fingerprint),
            )
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_identity.encode("ascii")).hexdigest()


def _validate_sha256(value: object, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("{} must be a lowercase SHA-256 digest".format(field_name))


def provisional_diagnostic_baseline_v1() -> ResearchPolicyBundle:
    """Return the selected but unapproved first diagnostic policy revision."""

    impulse = ImpulsePolicy(
        movement=ImpulseMovementPolicy.NET_OPEN_TO_CLOSE,
        atr_method=AtrMethod.WILDER_SMA_SEED,
        atr_timing=AtrTiming.BEFORE_IMPULSE_WINDOW,
        atr_period=14,
        minimum_directional_bars=3,
        movement_atr_multiple=1.5,
        body_atr_multiple=0.8,
    )
    origin_policy = OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW
    origin_lookback_bars = 20
    lifecycle = ZoneLifecyclePolicy(
        retest_policy=RetestPolicy.FIRST_TOUCH_ONLY,
        touch_episode=TouchEpisodePolicy.CONTIGUOUS_INCLUSIVE_OVERLAP,
        equal_time_order=EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH,
        overlap_selection=OverlapSelection.NEWEST_ORIGIN_THEN_ZONE_KEY,
        price_basis=ZonePriceBasis.MID,
    )
    regime = RegimePolicy(
        structure_rule=StructureRule.STRICT_LAST_TWO_CONFIRMED_HIGH_LOW,
        insufficient_evidence=InsufficientEvidencePolicy.UNKNOWN,
        mixed_structure=MixedStructurePolicy.RANGE,
    )
    confirmation = ConfirmationPolicy(
        ema_period=8,
        ema_initialization=EmaInitialization.SMA_PERIOD_SEED,
        crossover_timing=CrossoverTiming.SAME_BAR_EMA,
        engulfing_equality=EngulfingEquality.INCLUSIVE,
        displacement_lookback_bars=20,
        displacement_median=MedianConvention.MEAN_OF_MIDDLE_TWO,
        displacement_history=DisplacementHistory.PREVIOUS_BARS_ONLY,
        displacement_body_multiple=1.2,
        doji_semantics=DojiSemantics.NEITHER_DIRECTION,
        candle_combination=CandleCombination.ENGULFING_OR_DISPLACEMENT,
        touch_bar_confirmation=TouchBarConfirmation.TOUCH_BAR_ALLOWED,
        confirmation_expiry_bars=2,
    )
    candidate = CandidatePolicy(
        expected_impulse_policy_fingerprint=impulse.fingerprint,
        expected_origin_policy=origin_policy,
        expected_origin_lookback_bars=origin_lookback_bars,
        entry_timing=EntryTiming.NEXT_M15_OPEN_ONLY,
        regime_alignment=RegimeAlignment.STRICT_DIRECTIONAL,
        retest_validity=RetestValidity.ZONE_VALID_AT_CONFIRMATION_STEP,
    )
    return ResearchPolicyBundle(
        baseline_id="xauusd-supply-demand-provisional",
        revision=1,
        status=BaselineStatus.PROVISIONAL_UNAPPROVED,
        source_sha256=OPERATOR_DOCUMENT_SHA256,
        impulse_policy=impulse,
        origin_policy=origin_policy,
        origin_lookback_bars=origin_lookback_bars,
        lifecycle_policy=lifecycle,
        regime_policy=regime,
        confirmation_policy=confirmation,
        candidate_policy=candidate,
    )


__all__ = [
    "BaselineStatus",
    "OPERATOR_DOCUMENT_SHA256",
    "ResearchPolicyBundle",
    "provisional_diagnostic_baseline_v1",
]
