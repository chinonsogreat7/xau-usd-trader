"""Opt-in research interpretation of observed, partially open candles.

The user selected inclusion in indicators and signals. Each observed partial
slot counts once, without scaling its range/body to a full-duration candle.
This module defines eligible evidence, not trades or a replacement strategy.
"""

from datetime import timedelta
import hashlib
import json

from .session_buckets import SessionBucketAggregation, validate_session_bucket_aggregation


POLICY_VERSION = "include-observed-partial-candles-v1"


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def partial_candle_semantics():
    return {
        "version": POLICY_VERSION,
        "status": "provisional_research_user_selected",
        "partial_m15_indicators": "include_observed_partial_slot_once_equal_bar_weight",
        "partial_m15_signals": "eligible_for_same_signal_rules_not_an_automatic_signal",
        "partial_h1": "combine_all_observed_open_children_in_same_utc_hour_count_once",
        "ohlc": "actual_bid_ask_observations_only_no_duration_scaling_or_padding",
        "availability": "nominal_utc_bucket_end_never_earlier_at_session_close",
        "missing_open_fragment": "block_replay_do_not_skip_or_relabel_as_closed",
        "fully_closed_slot": "no_candle_no_indicator_update",
        "indicator_history": "retain_across_documented_closures_no_reset_no_time_compression",
        "entry": "only_if_next_nominal_m15_boundary_is_open_no_queue_for_reopening",
        "calendar_knowledge": "supplied_schedule_assumed_known_not_verified_point_in_time",
        "execution": None,
    }


def _entry_boundary(closures, closure_index, timestamp, coverage_end):
    if timestamp >= coverage_end:
        return False, "next_boundary_outside_import_coverage"
    if closure_index < len(closures) and closures[closure_index].start <= timestamp < closures[closure_index].end:
        return False, "next_boundary_inside_scheduled_closure"
    return True, "scheduled_open_not_proof_of_executable_quote"


def evaluate_session_bucket_policy(aggregation: SessionBucketAggregation) -> dict:
    """Recompute evidence before returning a versioned indicator/signal plan.

    Availability and next-open checks use only the slot's data and supplied
    calendar, not future ticks. A full replay must reject missing-open data and
    separately enforce warm-up and its complete strategy rules.
    """
    checked = validate_session_bucket_aggregation(aggregation)
    semantics = partial_candle_semantics()
    m15 = []
    closures = checked.calendar.closures
    closure_index = 0
    for bucket in checked.buckets:
        eligible = bucket.observation_status == "observed"
        # A single sweep keeps classification linear in buckets + closures.
        # An observation slot can open later than its nominal start; that must
        # not make the earlier (still closed) boundary an eligible entry time.
        while closure_index < len(closures) and closures[closure_index].end <= bucket.end_time:
            closure_index += 1
        entry_open, entry_reason = _entry_boundary(closures, closure_index, bucket.end_time, checked.coverage_end)
        m15.append({
            "start_time": _iso(bucket.start_time), "end_time": _iso(bucket.end_time),
            "available_at": _iso(bucket.end_time),
            "state": bucket.state, "observation_status": bucket.observation_status,
            "scheduled_open_microseconds": bucket.scheduled_open_microseconds,
            "scheduled_closed_microseconds": bucket.scheduled_closed_microseconds,
            "tick_count": bucket.tick_count,
            "indicator_input_eligible": eligible,
            "signal_input_eligible": eligible,
            "reason": ("observed_partial_candle_included" if bucket.state == "partial_open" else
                       "observed_full_candle_included") if eligible else bucket.observation_status,
            "next_nominal_entry_boundary_open": entry_open,
            "next_nominal_entry_boundary_reason": entry_reason,
            "next_nominal_entry_input_eligible": eligible and entry_open,
            "observed_ohlc": None if bucket.observed_ohlc is None else bucket.observed_ohlc.as_dict(),
        })
    h1 = []
    for offset in range(0, len(checked.buckets), 4):
        children = checked.buckets[offset:offset + 4]
        if len(children) != 4 or children[-1].end_time - children[0].start_time != timedelta(hours=1):
            raise ValueError("session policy requires four nominal M15 slots per covered UTC hour")
        opened = [c for c in children if c.state != "closed"]
        eligible = bool(opened) and all(c.observation_status == "observed" for c in opened)
        open_us = sum(c.scheduled_open_microseconds for c in children)
        state = "closed" if not open_us else "full_open" if open_us == 3600000000 else "partial_open"
        ohlc = None
        if eligible:
            values = [c.observed_ohlc for c in opened]
            ohlc = {}
            for side in ("bid", "ask"):
                ohlc.update({
                    side + "_open": getattr(values[0], side + "_open"),
                    side + "_high": max(getattr(v, side + "_high") for v in values),
                    side + "_low": min(getattr(v, side + "_low") for v in values),
                    side + "_close": getattr(values[-1], side + "_close"),
                })
        h1.append({
            "start_time": _iso(children[0].start_time), "end_time": _iso(children[-1].end_time),
            "available_at": _iso(children[-1].end_time),
            "state": state,
            "observation_status": "observed" if eligible else "closed" if not opened else "missing_open_data",
            "scheduled_open_microseconds": open_us,
            "scheduled_closed_microseconds": 3600000000 - open_us,
            "observed_open_m15_count": sum(c.observation_status == "observed" for c in children),
            "scheduled_open_m15_count": len(opened),
            "indicator_input_eligible": eligible, "signal_input_eligible": eligible,
            "observed_ohlc": ohlc,
        })
    result = {
        "schema": "xau_trader.session_bucket_policy", "version": 1,
        "semantics": semantics, "policy_fingerprint": _fingerprint(semantics),
        "raw_sha256": checked.parsed.raw_sha256,
        "calendar_fingerprint": checked.calendar.fingerprint,
        "availability_basis": checked.availability_basis.value,
        "scope": "input_eligibility_plan_not_indicator_or_signal_calculation",
        "missing_open_data_blocks_replay": any(c.observation_status == "missing_open_data" for c in checked.buckets),
        "counts": {
            "eligible_m15": sum(r["indicator_input_eligible"] for r in m15),
            "included_partial_m15": sum(r["indicator_input_eligible"] and r["state"] == "partial_open" for r in m15),
            "eligible_h1": sum(r["indicator_input_eligible"] for r in h1),
            "included_partial_h1": sum(r["indicator_input_eligible"] and r["state"] == "partial_open" for r in h1),
        },
        "m15": m15, "h1": h1,
        "strategy_replay_executed": False, "execution": None,
        "warnings": [
            "Eligibility is an input rule, not a BUY/SELL decision or a full-strategy readiness check.",
            "A short observed partial candle has equal bar weight under this explicit research hypothesis.",
            "One tick per open fragment does not prove the broker supplied every tick.",
        ],
    }
    result["fingerprint"] = _fingerprint(result)
    return result
