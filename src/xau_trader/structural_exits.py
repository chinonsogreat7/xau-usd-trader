"""Provisional synthetic-only structural stops and target evidence, not orders.

This planning stage does not execute candidates. A separate adapter must bind
its plans to an explicit quote-timing contract. A plan remains an unapproved
research hypothesis, not a trade.
"""

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import (
    Context, Decimal, DivisionByZero, InvalidOperation, Overflow,
    ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext,
)
from enum import Enum
import hashlib
import json
import math
from typing import Any, Dict, Optional, Sequence, Tuple

from .candidate_signals import CandidateAction, CandidateDecision
from .diagnostic_replay import DiagnosticReplayResult, validate_diagnostic_replay_result
from .domain import AvailabilityBasis, QuoteBar
from .multitimeframe import H1PivotEvent, PivotKind
from .session_calendar import SessionCalendarArtifact
from .supply_demand import AtrEvent, ZoneEvent, ZoneKind


PLANNER_VERSION = "provisional-unapproved-structural-exits-v1"
SEMANTICS = (
    "new_operator_hypothesis_not_source_verified",
    "m15_strict_pivots_equal_wing_prices_disqualify_actual_bars_across_documented_closures",
    "m15_mid_extremes=(Decimal(str(bid_extreme))+Decimal(str(ask_extreme)))/2",
    "m15_confirmed_at_rightmost_wing_close_available_at_max_entire_window_receipt",
    "snapshot_at_candidate_available_at_include_equal_time_visible_evidence",
    "latest_directional_m15_swing_by_center_confirmed_and_available_by_snapshot",
    "latest_h1_atr_by_bar_end_available_by_snapshot_bar_end_not_after_decision_boundary",
    "buy_stop=min(zone_lower,latest_m15_swing_low)-atr_multiple*latest_h1_atr",
    "sell_stop=max(zone_upper,latest_m15_swing_high)+atr_multiple*latest_h1_atr",
    "stop_round_outward_buy_floor_sell_ceil",
    "all_visible_directional_h1_pivots_frozen_no_target_selected_at_decision",
    "target_round_toward_entry_high_floor_low_ceil",
    "target_ladder_nearest_direction_first_equal_price_latest_center_first",
    "entry_target_strictly_beyond_actual_entry_nearest_only_no_farther_rr_fallback",
    "exactly_one_selected_retest_zone_no_multizone_heuristic",
    "pre_roll_is_not_actionable_synthetic_only_no_execution_binding",
)


def _context() -> Context:
    return Context(prec=160, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                   capitals=1, clamp=0, flags=[], traps=[InvalidOperation, DivisionByZero, Overflow])


def _positive(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("{} must be a finite positive Decimal".format(name))
    if len(value.as_tuple().digits) > 40 or abs(value.as_tuple().exponent) > 40:
        raise ValueError("{} exceeds supported precision/range".format(name))


def _decimal(value: float) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("diagnostic price/ATR must be finite numeric data")
    return Decimal(str(value))


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _json(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StructuralExitPolicy:
    """Required choices; none imply source approval or strategy profitability."""

    m15_left_wing: int
    m15_right_wing: int
    stop_atr_multiple: Decimal

    def __post_init__(self) -> None:
        for name in ("m15_left_wing", "m15_right_wing"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
                raise ValueError("{} must be an integer from 1 to 10000".format(name))
        _positive(self.stop_atr_multiple, "stop_atr_multiple")

    def as_dict(self) -> Dict[str, Any]:
        return dict(version=PLANNER_VERSION, m15_left_wing=self.m15_left_wing,
                    m15_right_wing=self.m15_right_wing,
                    stop_atr_multiple=format(self.stop_atr_multiple, "f"), semantics=list(SEMANTICS))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.as_dict())


@dataclass(frozen=True)
class M15SwingEvent:
    kind: PivotKind
    pivot_bar_start: datetime
    pivot_bar_end: datetime
    confirmed_at: datetime
    available_at: datetime
    mid_price: Decimal
    left_wing: int
    right_wing: int
    policy_fingerprint: str
    calendar_fingerprint: Optional[str]


@dataclass(frozen=True)
class StructuralTargetLevel:
    price: Decimal
    pivot: H1PivotEvent


@dataclass(frozen=True)
class StructuralBracketPlan:
    candidate_key: tuple
    candidate_fingerprint: str
    action: CandidateAction
    decision_available_at: datetime
    proposed_entry_at: datetime
    selected_zone: ZoneEvent
    swing: M15SwingEvent
    atr: AtrEvent
    atr_buffer: Decimal
    stop_loss: Decimal
    target_levels: Tuple[StructuralTargetLevel, ...]
    price_tick: Decimal
    policy_fingerprint: str

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self)


@dataclass(frozen=True)
class StructuralExitDecision:
    candidate_key: tuple
    candidate_fingerprint: str
    action: CandidateAction
    available_at: datetime
    status: str
    reason: str
    plan: Optional[StructuralBracketPlan]


@dataclass(frozen=True)
class StructuralExitResult:
    replay_fingerprint: str
    dataset_fingerprint: str
    policy: StructuralExitPolicy
    price_tick: Decimal
    pre_roll_count: int
    m15_swings: Tuple[M15SwingEvent, ...]
    decisions: Tuple[StructuralExitDecision, ...]

    @property
    def engine_connected(self) -> bool:
        return False

    @property
    def broker_connected(self) -> bool:
        return False

    @property
    def hypothesis_unapproved(self) -> bool:
        return True

    def as_dict(self) -> Dict[str, Any]:
        payload = _json(self)
        payload.update(
            status="provisional_structural_exit_planning_complete", planner_version=PLANNER_VERSION,
            policy=self.policy.as_dict(), policy_fingerprint=self.policy.fingerprint,
            engine_connected=False, broker_connected=False, real_orders_submitted=0,
            hypothesis_unapproved=True, promotion_eligible=False,
            counts=dict(candidates=len(self.decisions), pre_roll=self.pre_roll_count,
                        m15_swings=len(self.m15_swings),
                        bracket_plans=sum(row.status == "bracket_plan" for row in self.decisions),
                        skipped=sum(row.status == "skip" for row in self.decisions)),
            warnings=[
                "Synthetic-only structural exit evidence; no trades, execution, returns or profitability assessment.",
                "M15 pivot wings, latest H1 ATR buffer and equal-time snapshot semantics are unapproved operator hypotheses.",
                "Target selection requires an actual entry price; no farther target may replace the nearest to improve reward/risk.",
                "This planning stage does not execute orders; execution requires a separate explicit timing contract.",
                "Stops/targets use arithmetic midpoint structure; broker specifications and executable bid/ask checks remain separate.",
            ],
        )
        payload["fingerprint"] = _fingerprint(payload)
        return payload

    @property
    def fingerprint(self) -> str:
        return self.as_dict()["fingerprint"]


def confirmed_m15_swings(
    bars: Sequence[QuoteBar], *, policy: StructuralExitPolicy,
    calendar: Optional[SessionCalendarArtifact] = None,
) -> Tuple[M15SwingEvent, ...]:
    """Detect strict M15 swings on actual bars, never inventing closure bars."""
    if not isinstance(policy, StructuralExitPolicy):
        raise TypeError("policy must be StructuralExitPolicy")
    if calendar is not None:
        if not isinstance(calendar, SessionCalendarArtifact):
            raise TypeError("calendar must be a SessionCalendarArtifact")
        calendar.require_hour_aligned_closures()
    source = tuple(bars)
    for index, bar in enumerate(source):
        if not isinstance(bar, QuoteBar) or bar.availability_basis != AvailabilityBasis.SYNTHETIC:
            raise ValueError("structural planner currently accepts synthetic QuoteBar inputs only")
        if bar.timestamp - bar.start_time != timedelta(minutes=15) or bar.start_time.minute % 15 or bar.start_time.second or bar.start_time.microsecond:
            raise ValueError("M15 bars must span aligned 15-minute UTC intervals")
        if calendar is not None:
            calendar.validate_bar(bar.start_time, bar.timestamp)
            if index:
                calendar.validate_transition(source[index - 1].timestamp, bar.start_time)
        elif index and bar.start_time != source[index - 1].timestamp:
            raise ValueError("M15 bars must be contiguous without an explicit calendar")
    result = []
    width = policy.m15_left_wing + policy.m15_right_wing + 1
    with localcontext(_context()):
        for start in range(len(source) - width + 1):
            window = source[start:start + width]
            center = window[policy.m15_left_wing]
            others = window[:policy.m15_left_wing] + window[policy.m15_left_wing + 1:]
            for kind in (PivotKind.HIGH, PivotKind.LOW):
                field = "high" if kind == PivotKind.HIGH else "low"
                def mid(bar):
                    return (_decimal(getattr(bar, "bid_" + field)) + _decimal(getattr(bar, "ask_" + field))) / 2
                price = mid(center)
                strict = all(price > mid(other) for other in others) if kind == PivotKind.HIGH else all(price < mid(other) for other in others)
                if strict:
                    result.append(M15SwingEvent(
                        kind, center.start_time, center.timestamp, window[-1].timestamp,
                        max(bar.available_at for bar in window), price,
                        policy.m15_left_wing, policy.m15_right_wing, policy.fingerprint,
                        None if calendar is None else calendar.fingerprint,
                    ))
    return tuple(result)


def _plan_candidate(
    decision: CandidateDecision, *, swings: Sequence[M15SwingEvent],
    atr_events: Sequence[AtrEvent], h1_pivots: Sequence[H1PivotEvent],
    policy: StructuralExitPolicy, price_tick: Decimal, pre_roll: bool = False,
) -> StructuralExitDecision:
    """Pure helper; only the public replay planner supplies validated evidence."""
    def result(reason, plan=None):
        return StructuralExitDecision(decision.key, _fingerprint(decision), decision.action,
                                      decision.available_at, "skip" if plan is None else "bracket_plan",
                                      reason, plan)
    if pre_roll:
        return result("pre_roll")
    if decision.action == CandidateAction.NO_TRADE:
        return result("candidate_no_trade")
    if len(decision.selected_retests) != 1:
        return result("ambiguous_selected_zone")
    zone = decision.selected_retests[0].zone
    buy = decision.action == CandidateAction.BUY
    if zone.kind != (ZoneKind.DEMAND if buy else ZoneKind.SUPPLY):
        return result("zone_direction_mismatch")
    snapshot = decision.available_at
    boundary = decision.m15_bar_end
    if zone.available_at > snapshot or zone.formed_at > boundary:
        return result("zone_not_visible")
    needed_swing = PivotKind.LOW if buy else PivotKind.HIGH
    visible_swings = [swing for swing in swings if swing.kind == needed_swing
                      and swing.confirmed_at <= snapshot and swing.confirmed_at <= boundary
                      and swing.available_at <= snapshot]
    if not visible_swings:
        return result("no_visible_m15_swing")
    swing = max(visible_swings, key=lambda item: (item.pivot_bar_start, item.confirmed_at, item.available_at))
    visible_atr = [atr for atr in atr_events if atr.bar_end <= boundary and atr.available_at <= snapshot]
    if not visible_atr:
        return result("no_visible_h1_atr")
    atr = max(visible_atr, key=lambda item: (item.bar_end, item.available_at))
    if _decimal(atr.value) <= 0:
        return result("non_positive_h1_atr")
    target_kind = PivotKind.HIGH if buy else PivotKind.LOW
    visible_targets = [pivot for pivot in h1_pivots if pivot.kind == target_kind
                       and pivot.confirmed_at <= snapshot and pivot.confirmed_at <= boundary
                       and pivot.available_at <= snapshot]
    if not visible_targets:
        return result("no_visible_target")
    with localcontext(_context()):
        buffer = policy.stop_atr_multiple * _decimal(atr.value)
        reference = min(_decimal(zone.lower_price), swing.mid_price) if buy else max(_decimal(zone.upper_price), swing.mid_price)
        raw_stop = reference - buffer if buy else reference + buffer
        stop = (raw_stop / price_tick).to_integral_value(rounding=ROUND_FLOOR if buy else ROUND_CEILING) * price_tick
        if stop <= 0:
            return result("non_positive_stop")
        # Stable sort makes newest-center evidence win ties without discarding
        # duplicate-price levels or consulting any later entry quote.
        visible_targets.sort(key=lambda item: (item.pivot_bar_start, item.confirmed_at, item.available_at), reverse=True)
        targets = tuple(StructuralTargetLevel(
            (_decimal(pivot.mid_price) / price_tick).to_integral_value(rounding=ROUND_FLOOR if buy else ROUND_CEILING) * price_tick,
            pivot,
        ) for pivot in visible_targets)
        targets = tuple(sorted(targets, key=lambda level: level.price, reverse=not buy))
        if any(level.price <= 0 for level in targets):
            return result("non_positive_target")
        plan = StructuralBracketPlan(decision.key, _fingerprint(decision), decision.action,
                                     snapshot, decision.proposed_entry_at, zone, swing, atr, buffer,
                                     stop, targets, price_tick, policy.fingerprint)
    return result("structural_evidence_ready_entry_target_unselected", plan)


def select_entry_target(plan: StructuralBracketPlan, entry_price: Decimal) -> Optional[StructuralTargetLevel]:
    """Nearest frozen directional target only; never searches farther for RR."""
    if not isinstance(plan, StructuralBracketPlan):
        raise TypeError("plan must be StructuralBracketPlan")
    _positive(entry_price, "entry_price")
    if plan.action not in (CandidateAction.BUY, CandidateAction.SELL):
        raise ValueError("plan must have a directional action")
    candidates = [level for level in plan.target_levels
                  if (level.price > entry_price if plan.action == CandidateAction.BUY else level.price < entry_price)]
    if not candidates:
        return None
    if plan.action == CandidateAction.BUY:
        return min(candidates, key=lambda level: level.price)
    return max(candidates, key=lambda level: level.price)


def plan_structural_exits(
    replay: DiagnosticReplayResult, *, policy: StructuralExitPolicy, price_tick: Decimal,
) -> StructuralExitResult:
    """Recompute a synthetic replay, then attach auditable non-executable plans."""
    if not isinstance(policy, StructuralExitPolicy):
        raise TypeError("policy must be StructuralExitPolicy")
    _positive(price_tick, "price_tick")
    validate_diagnostic_replay_result(replay)
    if replay.availability_basis != AvailabilityBasis.SYNTHETIC:
        raise ValueError("structural planner currently accepts synthetic replay evidence only")
    swings = confirmed_m15_swings(replay.m15_bars, policy=policy, calendar=replay.calendar)
    boundary = replay.readiness.post_pre_roll_m15_start
    decisions = tuple(_plan_candidate(
        decision, swings=swings, atr_events=replay.h1_atr, h1_pivots=replay.h1_pivots,
        policy=policy, price_tick=price_tick, pre_roll=decision.m15_bar_start < boundary,
    ) for decision in replay.decisions)
    return StructuralExitResult(replay.fingerprint, replay.dataset_fingerprint, policy, price_tick,
                                len(replay.pre_roll_decisions), swings, decisions)
