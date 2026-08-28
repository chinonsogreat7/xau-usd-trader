"""Deterministic, paper-only artifacts for a diagnostic replay.

The JSON artifact is the complete evidence trace.  The CSV artifact is a
fixed-column decision index intended for inspection and filtering.  Neither
format adds execution meaning to a :class:`DiagnosticReplayResult`.
"""

import csv
from datetime import datetime, timezone
from enum import Enum
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Dict, List, Optional, Tuple, Union

from .candidate_signals import CandidateDecision, CandidatePolicy
from .diagnostic_replay import (
    DIAGNOSTIC_REPLAY_ENGINE_VERSION,
    DIAGNOSTIC_REPLAY_SCHEMA_VERSION,
    DiagnosticReplayResult,
    validate_diagnostic_replay_result,
)
from .domain import QuoteBar
from .m15_confirmation import (
    CandlePatternEvent,
    ConfirmationEvent,
    ConfirmationPolicy,
    EmaEvent,
)
from .market_regime import H1RegimeEvent, RegimePolicy
from .multitimeframe import H1PivotEvent
from .research_baseline import ResearchPolicyBundle
from .supply_demand import AtrEvent, ImpulseEvent, ImpulsePolicy, ZoneEvent
from .zone_lifecycle import (
    CompletedBarObservation,
    RetestEpisode,
    RetestEpisodeEnded,
    RetestEpisodeStarted,
    SealedAvailabilityGroup,
    ZoneBook,
    ZoneBookUpdate,
    ZoneInvalidated,
    ZoneLifecycleEvent,
    ZoneLifecyclePolicy,
    ZoneObservationTransition,
    ZoneState,
)


TRACE_SCHEMA = "xauusd_diagnostic_trace"
TRACE_SCHEMA_VERSION = 1


DECISION_CSV_COLUMNS = (
    "schema",
    "schema_version",
    "diagnostic_only",
    "execution",
    "dataset_fingerprint",
    "replay_fingerprint",
    "replay_engine_version",
    "replay_schema_version",
    "evidence_fingerprint",
    "input_bars_fingerprint",
    "policy_bundle_fingerprint",
    "decision_index",
    "readiness",
    "action",
    "reasons",
    "m15_bar_start",
    "m15_bar_end",
    "next_m15_open",
    "proposed_entry_at",
    "available_at",
    "transition_available_at",
    "transition_sealed_through",
    "direction",
    "confirmation_key",
    "confirmation_direction",
    "confirmation_bar_start",
    "confirmation_bar_end",
    "confirmation_available_at",
    "confirmation_proving_patterns",
    "regime_key",
    "regime",
    "regime_structure_bar_start",
    "regime_structure_bar_end",
    "regime_available_at",
    "selected_zone_keys",
    "selected_retest_ordinals",
    "policy_fingerprint",
    "candidate_policy_fingerprint",
    "lifecycle_policy_fingerprint",
    "regime_policy_fingerprint",
    "confirmation_policy_fingerprint",
)


PathLike = Union[str, os.PathLike]


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime):
        raise TypeError("trace timestamp must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trace timestamp must include a timezone")
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_utc(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else _utc(value)


def _enum(value: Enum) -> object:
    if not isinstance(value, Enum):
        raise TypeError("trace enum value must be an Enum")
    return value.value


def _quote_bar(bar: QuoteBar) -> Dict[str, object]:
    return {
        "start_time": _utc(bar.start_time),
        "end_time": _utc(bar.timestamp),
        "available_at": _utc(bar.available_at),
        "availability_basis": _enum(bar.availability_basis),
        "bid": {
            "open": bar.bid_open,
            "high": bar.bid_high,
            "low": bar.bid_low,
            "close": bar.bid_close,
        },
        "ask": {
            "open": bar.ask_open,
            "high": bar.ask_high,
            "low": bar.ask_low,
            "close": bar.ask_close,
        },
        "volume": bar.volume,
    }


def _pivot(event: H1PivotEvent) -> Dict[str, object]:
    return {
        "kind": _enum(event.kind),
        "pivot_bar_start": _utc(event.pivot_bar_start),
        "pivot_bar_end": _utc(event.pivot_bar_end),
        "confirmed_at": _utc(event.confirmed_at),
        "available_at": _utc(event.available_at),
        "availability_basis": _enum(event.availability_basis),
        "bid_price": event.bid_price,
        "ask_price": event.ask_price,
        "mid_price": event.mid_price,
        "left_wing": event.left_wing,
        "right_wing": event.right_wing,
    }


def _pivot_key(event: H1PivotEvent) -> List[object]:
    return [
        _enum(event.kind),
        _utc(event.pivot_bar_start),
        _utc(event.pivot_bar_end),
        _utc(event.confirmed_at),
        _utc(event.available_at),
        _enum(event.availability_basis),
        event.bid_price,
        event.ask_price,
        event.mid_price,
        event.left_wing,
        event.right_wing,
    ]


def _atr(event: AtrEvent) -> Dict[str, object]:
    return {
        "bar_start": _utc(event.bar_start),
        "bar_end": _utc(event.bar_end),
        "available_at": _utc(event.available_at),
        "availability_basis": _enum(event.availability_basis),
        "method": _enum(event.method),
        "period": event.period,
        "true_range": event.true_range,
        "value": event.value,
    }


def _impulse_key(event: ImpulseEvent) -> List[object]:
    return [
        event.policy_fingerprint,
        _enum(event.direction),
        _utc(event.window_start),
        _utc(event.window_end),
    ]


def _impulse(event: ImpulseEvent) -> Dict[str, object]:
    return {
        "key": _impulse_key(event),
        "direction": _enum(event.direction),
        "window_start": _utc(event.window_start),
        "window_end": _utc(event.window_end),
        "confirmed_at": _utc(event.confirmed_at),
        "available_at": _utc(event.available_at),
        "availability_basis": _enum(event.availability_basis),
        "movement_policy": _enum(event.movement_policy),
        "atr_method": _enum(event.atr_method),
        "atr_timing": _enum(event.atr_timing),
        "atr_period": event.atr_period,
        "minimum_directional_bars": event.minimum_directional_bars,
        "policy_fingerprint": event.policy_fingerprint,
        "atr_snapshot_bar_end": _utc(event.atr_snapshot_bar_end),
        "atr_value": event.atr_value,
        "directional_movement": event.directional_movement,
        "directional_bar_count": event.directional_bar_count,
        "largest_body": event.largest_body,
        "movement_atr_multiple": event.movement_atr_multiple,
        "body_atr_multiple": event.body_atr_multiple,
        "window_bars": event.window_bars,
    }


def _zone_key(event: ZoneEvent) -> List[object]:
    return [
        _enum(event.kind),
        _enum(event.origin_policy),
        event.origin_lookback_bars,
        _utc(event.origin_bar_start),
        [
            event.impulse_key[0],
            _enum(event.impulse_key[1]),
            _utc(event.impulse_key[2]),
            _utc(event.impulse_key[3]),
        ],
    ]


def _zone(event: ZoneEvent) -> Dict[str, object]:
    return {
        "key": _zone_key(event),
        "kind": _enum(event.kind),
        "origin_policy": _enum(event.origin_policy),
        "origin_lookback_bars": event.origin_lookback_bars,
        "origin_bar_start": _utc(event.origin_bar_start),
        "origin_bar_end": _utc(event.origin_bar_end),
        "impulse_window_start": _utc(event.impulse_window_start),
        "impulse_window_end": _utc(event.impulse_window_end),
        "formed_at": _utc(event.formed_at),
        "available_at": _utc(event.available_at),
        "availability_basis": _enum(event.availability_basis),
        "lower_price": event.lower_price,
        "upper_price": event.upper_price,
        "impulse_key": [
            event.impulse_key[0],
            _enum(event.impulse_key[1]),
            _utc(event.impulse_key[2]),
            _utc(event.impulse_key[3]),
        ],
    }


def _regime_key(event: H1RegimeEvent) -> List[object]:
    return [
        event.policy_fingerprint,
        _utc(event.structure_bar_start),
        [_pivot_key(pivot) for pivot in event.high_pivots],
        [_pivot_key(pivot) for pivot in event.low_pivots],
    ]


def _regime(event: H1RegimeEvent) -> Dict[str, object]:
    return {
        "key": _regime_key(event),
        "regime": _enum(event.regime),
        "high_structure": _enum(event.high_structure),
        "low_structure": _enum(event.low_structure),
        "structure_bar_start": _utc(event.structure_bar_start),
        "structure_bar_end": _utc(event.structure_bar_end),
        "confirmed_at": _utc(event.confirmed_at),
        "available_at": _utc(event.available_at),
        "availability_basis": _enum(event.availability_basis),
        "structure_rule": _enum(event.structure_rule),
        "insufficient_evidence": _enum(event.insufficient_evidence),
        "mixed_structure": _enum(event.mixed_structure),
        "policy_fingerprint": event.policy_fingerprint,
        "high_pivots": [_pivot(pivot) for pivot in event.high_pivots],
        "low_pivots": [_pivot(pivot) for pivot in event.low_pivots],
    }


def _ema(event: EmaEvent) -> Dict[str, object]:
    return {
        "bar_start": _utc(event.bar_start),
        "bar_end": _utc(event.bar_end),
        "available_at": _utc(event.available_at),
        "availability_basis": _enum(event.availability_basis),
        "policy_fingerprint": event.policy_fingerprint,
        "period": event.period,
        "initialization": _enum(event.initialization),
        "seed_bar_start": _utc(event.seed_bar_start),
        "close": event.close,
        "value": event.value,
    }


def _candle(event: CandlePatternEvent) -> Dict[str, object]:
    return {
        "direction": _enum(event.direction),
        "kind": _enum(event.kind),
        "bar_start": _utc(event.bar_start),
        "bar_end": _utc(event.bar_end),
        "confirmed_at": _utc(event.confirmed_at),
        "available_at": _utc(event.available_at),
        "availability_basis": _enum(event.availability_basis),
        "policy_fingerprint": event.policy_fingerprint,
        "body_size": event.body_size,
        "reference_value": event.reference_value,
        "required_body_size": event.required_body_size,
        "reference_window_start": _utc(event.reference_window_start),
        "reference_window_end": _utc(event.reference_window_end),
    }


def _confirmation_key(event: ConfirmationEvent) -> List[object]:
    return [
        event.policy_fingerprint,
        _enum(event.direction),
        _utc(event.bar_start),
        _utc(event.bar_end),
        _utc(event.confirmed_at),
        _utc(event.available_at),
        _enum(event.availability_basis),
        event.ema_period,
        _enum(event.ema_initialization),
        _enum(event.crossover_timing),
        event.prior_close,
        event.prior_ema,
        event.current_close,
        event.current_ema,
        event.current_crossover_reference,
        [_enum(kind) for kind in event.proving_patterns],
        _enum(event.candle_combination),
        _enum(event.touch_bar_confirmation),
        event.confirmation_expiry_bars,
    ]


def _confirmation(event: ConfirmationEvent) -> Dict[str, object]:
    return {
        "key": _confirmation_key(event),
        "direction": _enum(event.direction),
        "bar_start": _utc(event.bar_start),
        "bar_end": _utc(event.bar_end),
        "confirmed_at": _utc(event.confirmed_at),
        "available_at": _utc(event.available_at),
        "availability_basis": _enum(event.availability_basis),
        "policy_fingerprint": event.policy_fingerprint,
        "ema_period": event.ema_period,
        "ema_initialization": _enum(event.ema_initialization),
        "crossover_timing": _enum(event.crossover_timing),
        "prior_close": event.prior_close,
        "prior_ema": event.prior_ema,
        "current_close": event.current_close,
        "current_ema": event.current_ema,
        "current_crossover_reference": event.current_crossover_reference,
        "proving_patterns": [_enum(kind) for kind in event.proving_patterns],
        "candle_combination": _enum(event.candle_combination),
        "touch_bar_confirmation": _enum(event.touch_bar_confirmation),
        "confirmation_expiry_bars": event.confirmation_expiry_bars,
    }


def _episode(episode: RetestEpisode) -> Dict[str, object]:
    return {
        "ordinal": episode.ordinal,
        "eligible": episode.eligible,
        "policy_fingerprint": episode.policy_fingerprint,
        "first_touch_bar_start": _utc(episode.first_touch_bar_start),
        "first_touch_bar_end": _utc(episode.first_touch_bar_end),
        "first_touch_available_at": _utc(episode.first_touch_available_at),
        "last_touch_bar_end": _utc(episode.last_touch_bar_end),
        "last_touch_available_at": _utc(episode.last_touch_available_at),
    }


def _zone_state(state: ZoneState) -> Dict[str, object]:
    return {
        "zone": _zone(state.zone),
        "policy_fingerprint": state.policy_fingerprint,
        "valid": state.valid,
        "retest_episode_count": state.retest_episode_count,
        "active_episode": (
            None if state.active_episode is None else _episode(state.active_episode)
        ),
        "invalidating_h1_start": _optional_utc(state.invalidating_h1_start),
        "invalidating_h1_end": _optional_utc(state.invalidating_h1_end),
        "invalidated_at": _optional_utc(state.invalidated_at),
        "next_h1_start": _utc(state.next_h1_start),
        "next_m15_start": _utc(state.next_m15_start),
    }


def _lifecycle_event(event: ZoneLifecycleEvent) -> Dict[str, object]:
    if isinstance(event, RetestEpisodeStarted):
        return {
            "event_type": "retest_episode_started",
            "zone": _zone(event.zone),
            "policy_fingerprint": event.policy_fingerprint,
            "ordinal": event.ordinal,
            "eligible": event.eligible,
            "m15_bar_start": _utc(event.m15_bar_start),
            "m15_bar_end": _utc(event.m15_bar_end),
            "available_at": _utc(event.available_at),
        }
    if isinstance(event, RetestEpisodeEnded):
        return {
            "event_type": "retest_episode_ended",
            "zone": _zone(event.zone),
            "policy_fingerprint": event.policy_fingerprint,
            "ordinal": event.ordinal,
            "reason": _enum(event.reason),
            "last_touch_bar_end": _utc(event.last_touch_bar_end),
            "ended_by_timeframe": _enum(event.ended_by_timeframe),
            "ended_by_bar_start": _utc(event.ended_by_bar_start),
            "ended_by_bar_end": _utc(event.ended_by_bar_end),
            "available_at": _utc(event.available_at),
        }
    if isinstance(event, ZoneInvalidated):
        return {
            "event_type": "zone_invalidated",
            "zone": _zone(event.zone),
            "policy_fingerprint": event.policy_fingerprint,
            "h1_bar_start": _utc(event.h1_bar_start),
            "h1_bar_end": _utc(event.h1_bar_end),
            "available_at": _utc(event.available_at),
            "mid_close": event.mid_close,
            "invalidation_boundary": event.invalidation_boundary,
        }
    raise TypeError("unsupported lifecycle event type: {}".format(type(event).__name__))


def _observation(observation: CompletedBarObservation) -> Dict[str, object]:
    return {
        "timeframe": _enum(observation.timeframe),
        "bar": _quote_bar(observation.bar),
    }


def _availability_group(group: SealedAvailabilityGroup) -> Dict[str, object]:
    return {
        "available_at": _utc(group.available_at),
        "sealed_through": _utc(group.sealed_through),
        "observations": [
            _observation(observation) for observation in group.observations
        ],
        "formations": [_zone(zone) for zone in group.formations],
    }


def _transition(transition: ZoneObservationTransition) -> Dict[str, object]:
    return {
        "observation": _observation(transition.observation),
        "policy_fingerprint": transition.policy_fingerprint,
        "sealed_through": _utc(transition.sealed_through),
        "states_before": [_zone_state(state) for state in transition.states_before],
        "states_after": [_zone_state(state) for state in transition.states_after],
        "events": [_lifecycle_event(event) for event in transition.events],
    }


def _zone_book(book: ZoneBook) -> Dict[str, object]:
    return {
        "policy_fingerprint": book.policy_fingerprint,
        "states": [_zone_state(state) for state in book.states],
        "next_h1_start": _utc(book.next_h1_start),
        "next_m15_start": _utc(book.next_m15_start),
        "last_group_available_at": _optional_utc(book.last_group_available_at),
        "sealed_through": _optional_utc(book.sealed_through),
    }


def _lifecycle(update: ZoneBookUpdate) -> Dict[str, object]:
    return {
        "book": _zone_book(update.book),
        "events": [_lifecycle_event(event) for event in update.events],
        "transitions": [
            _transition(transition) for transition in update.transitions
        ],
    }


def _retest_start(event: RetestEpisodeStarted) -> Dict[str, object]:
    # Keep the decision evidence shape identical to the lifecycle union member.
    return _lifecycle_event(event)


def _decision_readiness(
    result: DiagnosticReplayResult, event: CandidateDecision
) -> str:
    if event.m15_bar_start < result.readiness.post_pre_roll_m15_start:
        return "pre_roll"
    return "post_pre_roll"


def _decision(
    result: DiagnosticReplayResult, event: CandidateDecision
) -> Dict[str, object]:
    return {
        "readiness": _decision_readiness(result, event),
        "action": _enum(event.action),
        "reasons": [_enum(reason) for reason in event.reasons],
        "m15_bar_start": _utc(event.m15_bar_start),
        "m15_bar_end": _utc(event.m15_bar_end),
        "next_m15_open": _utc(event.next_m15_open),
        "proposed_entry_at": _optional_utc(event.proposed_entry_at),
        "available_at": _utc(event.available_at),
        "transition_available_at": _utc(event.transition_available_at),
        "transition_sealed_through": _utc(event.transition_sealed_through),
        "direction": None if event.direction is None else _enum(event.direction),
        "confirmation": (
            None if event.confirmation is None else _confirmation(event.confirmation)
        ),
        "regime": None if event.regime is None else _regime(event.regime),
        "selected_retests": [
            _retest_start(retest) for retest in event.selected_retests
        ],
        "policy_fingerprint": event.policy_fingerprint,
        "candidate_policy_fingerprint": event.candidate_policy_fingerprint,
        "lifecycle_policy_fingerprint": event.lifecycle_policy_fingerprint,
        "regime_policy_fingerprint": event.regime_policy_fingerprint,
        "confirmation_policy_fingerprint": event.confirmation_policy_fingerprint,
    }


def _impulse_policy(policy: ImpulsePolicy) -> Dict[str, object]:
    return {
        "movement": _enum(policy.movement),
        "atr_method": _enum(policy.atr_method),
        "atr_timing": _enum(policy.atr_timing),
        "atr_period": policy.atr_period,
        "minimum_directional_bars": policy.minimum_directional_bars,
        "movement_atr_multiple": policy.movement_atr_multiple,
        "body_atr_multiple": policy.body_atr_multiple,
        "fingerprint": policy.fingerprint,
    }


def _lifecycle_policy(policy: ZoneLifecyclePolicy) -> Dict[str, object]:
    return {
        "retest_policy": _enum(policy.retest_policy),
        "touch_episode": _enum(policy.touch_episode),
        "equal_time_order": _enum(policy.equal_time_order),
        "overlap_selection": _enum(policy.overlap_selection),
        "price_basis": _enum(policy.price_basis),
        "fingerprint": policy.fingerprint,
    }


def _regime_policy(policy: RegimePolicy) -> Dict[str, object]:
    return {
        "structure_rule": _enum(policy.structure_rule),
        "insufficient_evidence": _enum(policy.insufficient_evidence),
        "mixed_structure": _enum(policy.mixed_structure),
        "fingerprint": policy.fingerprint,
    }


def _confirmation_policy(policy: ConfirmationPolicy) -> Dict[str, object]:
    return {
        "ema_period": policy.ema_period,
        "ema_initialization": _enum(policy.ema_initialization),
        "crossover_timing": _enum(policy.crossover_timing),
        "engulfing_equality": _enum(policy.engulfing_equality),
        "displacement_lookback_bars": policy.displacement_lookback_bars,
        "displacement_median": _enum(policy.displacement_median),
        "displacement_history": _enum(policy.displacement_history),
        "displacement_body_multiple": policy.displacement_body_multiple,
        "doji_semantics": _enum(policy.doji_semantics),
        "candle_combination": _enum(policy.candle_combination),
        "touch_bar_confirmation": _enum(policy.touch_bar_confirmation),
        "confirmation_expiry_bars": policy.confirmation_expiry_bars,
        "fingerprint": policy.fingerprint,
    }


def _candidate_policy(policy: CandidatePolicy) -> Dict[str, object]:
    return {
        "expected_impulse_policy_fingerprint": (
            policy.expected_impulse_policy_fingerprint
        ),
        "expected_origin_policy": _enum(policy.expected_origin_policy),
        "expected_origin_lookback_bars": policy.expected_origin_lookback_bars,
        "entry_timing": _enum(policy.entry_timing),
        "regime_alignment": _enum(policy.regime_alignment),
        "retest_validity": _enum(policy.retest_validity),
        "fingerprint": policy.fingerprint,
    }


def _policy_bundle(bundle: ResearchPolicyBundle) -> Dict[str, object]:
    return {
        "baseline_id": bundle.baseline_id,
        "revision": bundle.revision,
        "status": _enum(bundle.status),
        "source_sha256": bundle.source_sha256,
        "fingerprint": bundle.fingerprint,
        "impulse_policy": _impulse_policy(bundle.impulse_policy),
        "origin_policy": _enum(bundle.origin_policy),
        "origin_lookback_bars": bundle.origin_lookback_bars,
        "lifecycle_policy": _lifecycle_policy(bundle.lifecycle_policy),
        "regime_policy": _regime_policy(bundle.regime_policy),
        "confirmation_policy": _confirmation_policy(bundle.confirmation_policy),
        "candidate_policy": _candidate_policy(bundle.candidate_policy),
    }


def _readiness(result: DiagnosticReplayResult) -> Dict[str, object]:
    readiness = result.readiness
    return {
        "history_boundary": _enum(readiness.history_boundary),
        "pre_roll_m15_bars": readiness.pre_roll_m15_bars,
        "minimum_input_m15_bars": readiness.minimum_input_m15_bars,
        "post_pre_roll_m15_start": _utc(readiness.post_pre_roll_m15_start),
        "pre_roll_decision_count": readiness.pre_roll_decision_count,
        "post_pre_roll_decision_count": readiness.post_pre_roll_decision_count,
    }


def _action_counts(decisions: Tuple[CandidateDecision, ...]) -> Dict[str, int]:
    return {
        action: sum(decision.action.value == action for decision in decisions)
        for action in ("buy", "sell", "no_trade")
    }


def diagnostic_trace_dict(result: DiagnosticReplayResult) -> Dict[str, object]:
    """Return the complete schema-v1 trace as JSON-compatible primitives."""

    if not isinstance(result, DiagnosticReplayResult):
        raise TypeError("result must be a DiagnosticReplayResult")
    validate_diagnostic_replay_result(result)
    counts = {
        "m15_bars": len(result.m15_bars),
        "h1_bars": len(result.h1_bars),
        "h1_atr": len(result.h1_atr),
        "h1_pivots": len(result.h1_pivots),
        "h1_regimes": len(result.h1_regimes),
        "h1_impulses": len(result.h1_impulses),
        "zones": len(result.zones),
        "m15_ema": len(result.m15_ema),
        "m15_candles": len(result.m15_candles),
        "m15_confirmations": len(result.m15_confirmations),
        "availability_groups": len(result.availability_groups),
        "lifecycle_events": len(result.lifecycle.events),
        "lifecycle_transitions": len(result.lifecycle.transitions),
        "decisions": len(result.decisions),
        "pre_roll_decisions": len(result.pre_roll_decisions),
        "post_pre_roll_decisions": len(result.post_pre_roll_decisions),
        "decision_actions": _action_counts(result.decisions),
        "pre_roll_actions": _action_counts(result.pre_roll_decisions),
        "post_pre_roll_actions": _action_counts(result.post_pre_roll_decisions),
    }
    return {
        "schema": TRACE_SCHEMA,
        "schema_version": TRACE_SCHEMA_VERSION,
        "diagnostic_only": True,
        "execution": None,
        "dataset": {"fingerprint": result.dataset_fingerprint},
        "replay": {
            "fingerprint": result.fingerprint,
            "evidence_fingerprint": result.evidence_fingerprint,
            "engine_version": DIAGNOSTIC_REPLAY_ENGINE_VERSION,
            "schema_version": DIAGNOSTIC_REPLAY_SCHEMA_VERSION,
            "input_bars_fingerprint": result.input_bars_fingerprint,
            "policy_bundle_fingerprint": result.policy_bundle_fingerprint,
            "as_of": _optional_utc(result.as_of),
            "availability_basis": _enum(result.availability_basis),
        },
        "readiness": _readiness(result),
        "policy_bundle": _policy_bundle(result.policy_bundle),
        "counts": counts,
        "streams": {
            "m15_bars": [_quote_bar(bar) for bar in result.m15_bars],
            "h1_bars": [_quote_bar(bar) for bar in result.h1_bars],
            "h1_atr": [_atr(event) for event in result.h1_atr],
            "h1_pivots": [_pivot(event) for event in result.h1_pivots],
            "h1_regimes": [_regime(event) for event in result.h1_regimes],
            "h1_impulses": [_impulse(event) for event in result.h1_impulses],
            "zones": [_zone(event) for event in result.zones],
            "m15_ema": [_ema(event) for event in result.m15_ema],
            "m15_candles": [_candle(event) for event in result.m15_candles],
            "m15_confirmations": [
                _confirmation(event) for event in result.m15_confirmations
            ],
            "availability_groups": [
                _availability_group(group) for group in result.availability_groups
            ],
        },
        "lifecycle": _lifecycle(result.lifecycle),
        "decisions": [_decision(result, event) for event in result.decisions],
    }


def diagnostic_trace_json_bytes(result: DiagnosticReplayResult) -> bytes:
    """Serialize a result to deterministic UTF-8 JSON ending in one newline."""

    text = json.dumps(
        diagnostic_trace_dict(result),
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        separators=(",", ": "),
        sort_keys=True,
    )
    return (text + "\n").encode("utf-8")


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _csv_row(
    result: DiagnosticReplayResult,
    index: int,
    event: CandidateDecision,
    *,
    replay_fingerprint: str,
    evidence_fingerprint: str,
) -> Dict[str, object]:
    confirmation = event.confirmation
    regime = event.regime
    return {
        "schema": TRACE_SCHEMA,
        "schema_version": TRACE_SCHEMA_VERSION,
        "diagnostic_only": "true",
        "execution": "",
        "dataset_fingerprint": result.dataset_fingerprint,
        "replay_fingerprint": replay_fingerprint,
        "replay_engine_version": DIAGNOSTIC_REPLAY_ENGINE_VERSION,
        "replay_schema_version": DIAGNOSTIC_REPLAY_SCHEMA_VERSION,
        "evidence_fingerprint": evidence_fingerprint,
        "input_bars_fingerprint": result.input_bars_fingerprint,
        "policy_bundle_fingerprint": result.policy_bundle_fingerprint,
        "decision_index": index,
        "readiness": _decision_readiness(result, event),
        "action": _enum(event.action),
        "reasons": _compact_json([_enum(reason) for reason in event.reasons]),
        "m15_bar_start": _utc(event.m15_bar_start),
        "m15_bar_end": _utc(event.m15_bar_end),
        "next_m15_open": _utc(event.next_m15_open),
        "proposed_entry_at": "" if event.proposed_entry_at is None else _utc(event.proposed_entry_at),
        "available_at": _utc(event.available_at),
        "transition_available_at": _utc(event.transition_available_at),
        "transition_sealed_through": _utc(event.transition_sealed_through),
        "direction": "" if event.direction is None else _enum(event.direction),
        "confirmation_key": "" if confirmation is None else _compact_json(_confirmation_key(confirmation)),
        "confirmation_direction": "" if confirmation is None else _enum(confirmation.direction),
        "confirmation_bar_start": "" if confirmation is None else _utc(confirmation.bar_start),
        "confirmation_bar_end": "" if confirmation is None else _utc(confirmation.bar_end),
        "confirmation_available_at": "" if confirmation is None else _utc(confirmation.available_at),
        "confirmation_proving_patterns": (
            ""
            if confirmation is None
            else _compact_json([_enum(kind) for kind in confirmation.proving_patterns])
        ),
        "regime_key": "" if regime is None else _compact_json(_regime_key(regime)),
        "regime": "" if regime is None else _enum(regime.regime),
        "regime_structure_bar_start": "" if regime is None else _utc(regime.structure_bar_start),
        "regime_structure_bar_end": "" if regime is None else _utc(regime.structure_bar_end),
        "regime_available_at": "" if regime is None else _utc(regime.available_at),
        "selected_zone_keys": _compact_json(
            [_zone_key(retest.zone) for retest in event.selected_retests]
        ),
        "selected_retest_ordinals": _compact_json(
            [retest.ordinal for retest in event.selected_retests]
        ),
        "policy_fingerprint": event.policy_fingerprint,
        "candidate_policy_fingerprint": event.candidate_policy_fingerprint,
        "lifecycle_policy_fingerprint": event.lifecycle_policy_fingerprint,
        "regime_policy_fingerprint": event.regime_policy_fingerprint,
        "confirmation_policy_fingerprint": event.confirmation_policy_fingerprint,
    }


def diagnostic_decisions_csv_bytes(result: DiagnosticReplayResult) -> bytes:
    """Serialize one fixed-column CSV row per diagnostic decision."""

    if not isinstance(result, DiagnosticReplayResult):
        raise TypeError("result must be a DiagnosticReplayResult")
    validate_diagnostic_replay_result(result)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=DECISION_CSV_COLUMNS,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    replay_fingerprint = result.fingerprint
    evidence_fingerprint = result.evidence_fingerprint
    for index, event in enumerate(result.decisions):
        writer.writerow(
            _csv_row(
                result,
                index,
                event,
                replay_fingerprint=replay_fingerprint,
                evidence_fingerprint=evidence_fingerprint,
            )
        )
    return output.getvalue().encode("utf-8")


def _target_path(path: PathLike) -> Path:
    try:
        target = Path(path)
    except TypeError as exc:
        raise TypeError("artifact path must be a string or path-like value") from exc
    if not target.name:
        raise ValueError("artifact path must name a file")
    return target


def _preflight_target(target: Path, overwrite: bool) -> None:
    if not isinstance(overwrite, bool):
        raise TypeError("overwrite must be a bool")
    if not target.parent.exists():
        raise FileNotFoundError("artifact parent directory does not exist: {}".format(target.parent))
    if not target.parent.is_dir():
        raise NotADirectoryError("artifact parent is not a directory: {}".format(target.parent))
    if not overwrite and target.exists():
        raise FileExistsError("artifact already exists: {}".format(target))
    if target.exists() and target.is_dir():
        raise IsADirectoryError("artifact target is a directory: {}".format(target))


def _stage(target: Path, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".{}-".format(target.name),
        suffix=".tmp",
        dir=str(target.parent),
    )
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        raise
    return staged


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write_one(target: Path, payload: bytes, overwrite: bool) -> Path:
    _preflight_target(target, overwrite)
    staged = _stage(target, payload)
    try:
        if overwrite:
            os.replace(str(staged), str(target))
        else:
            os.link(str(staged), str(target))
            _discard(staged)
    finally:
        _discard(staged)
    return target


def write_diagnostic_trace_json(
    result: DiagnosticReplayResult,
    path: PathLike,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write the complete JSON trace, refusing collisions by default."""

    payload = diagnostic_trace_json_bytes(result)
    target = _target_path(path)
    return _write_one(target, payload, overwrite)


def write_diagnostic_decisions_csv(
    result: DiagnosticReplayResult,
    path: PathLike,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write the decision CSV, refusing collisions by default."""

    payload = diagnostic_decisions_csv_bytes(result)
    target = _target_path(path)
    return _write_one(target, payload, overwrite)


def write_diagnostic_trace_artifacts(
    result: DiagnosticReplayResult,
    json_path: PathLike,
    csv_path: PathLike,
    *,
    overwrite: bool = False,
) -> Tuple[Path, Path]:
    """Write the JSON and CSV together after validating both destinations.

    With the default collision policy, a concurrent collision during commit is
    rolled back so the call does not leave only one newly-created artifact.
    """

    if not isinstance(overwrite, bool):
        raise TypeError("overwrite must be a bool")
    # Serialize both before touching either destination.  This includes the
    # finite-number checks imposed by ``allow_nan=False``.
    json_payload = diagnostic_trace_json_bytes(result)
    csv_payload = diagnostic_decisions_csv_bytes(result)
    json_target = _target_path(json_path)
    csv_target = _target_path(csv_path)
    if os.path.abspath(str(json_target)) == os.path.abspath(str(csv_target)):
        raise ValueError("JSON and CSV artifact paths must be different")
    _preflight_target(json_target, overwrite)
    _preflight_target(csv_target, overwrite)

    json_stage = _stage(json_target, json_payload)
    try:
        csv_stage = _stage(csv_target, csv_payload)
    except BaseException:
        _discard(json_stage)
        raise

    created = []  # type: List[Path]
    try:
        if overwrite:
            os.replace(str(json_stage), str(json_target))
            os.replace(str(csv_stage), str(csv_target))
        else:
            os.link(str(json_stage), str(json_target))
            created.append(json_target)
            os.link(str(csv_stage), str(csv_target))
            created.append(csv_target)
    except BaseException:
        if not overwrite:
            for target in reversed(created):
                _discard(target)
        raise
    finally:
        _discard(json_stage)
        _discard(csv_stage)
    return (json_target, csv_target)


__all__ = [
    "DECISION_CSV_COLUMNS",
    "TRACE_SCHEMA",
    "TRACE_SCHEMA_VERSION",
    "diagnostic_decisions_csv_bytes",
    "diagnostic_trace_dict",
    "diagnostic_trace_json_bytes",
    "write_diagnostic_decisions_csv",
    "write_diagnostic_trace_artifacts",
    "write_diagnostic_trace_json",
]
