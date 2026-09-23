"""Calendar-bound feature replay, deliberately separate from strategy decisions.

This first session-aware layer preserves the real UTC timeline and indicator
history across explicitly declared whole-hour closures. It does not reinterpret
the existing zone lifecycle, confirmation lifetime, or next-open entry policy.
"""

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Iterable

from .domain import AvailabilityBasis, QuoteBar
from .m15_confirmation import m15_ema_events
from .multitimeframe import aggregate_m15_to_h1, confirmed_h1_pivots
from .research_baseline import ResearchPolicyBundle
from .session_calendar import SessionCalendarArtifact
from .supply_demand import h1_atr_events


SESSION_FEATURE_ENGINE_VERSION = "session-features-v1"


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _json_value(getattr(value, field.name)) for field in fields(value)
            if not (field.name == "calendar" and getattr(value, field.name) is None)
        }
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _fingerprint(value) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_session_feature_replay(
    m15_bars: Iterable[QuoteBar],
    *,
    calendar: SessionCalendarArtifact,
    policy_bundle: ResearchPolicyBundle,
    dataset_fingerprint: str,
) -> dict:
    """Compute H1 bars, ATR, confirmed pivots and M15 EMA, without any signals.

    ``dataset_fingerprint`` is caller-supplied provenance, not independently
    verified rights. The CLI additionally binds exact source/CSV/calendar bytes
    to manifest v2 before calling this kernel. Lower-level feature functions
    validate every bar interval and every intervening closure; no rows are
    fabricated, trimmed, timestamp-compressed or used to reset indicator seeds.
    """

    if not isinstance(calendar, SessionCalendarArtifact):
        raise TypeError("calendar must be a SessionCalendarArtifact")
    if not isinstance(policy_bundle, ResearchPolicyBundle):
        raise TypeError("policy_bundle must be a ResearchPolicyBundle")
    if (not isinstance(dataset_fingerprint, str) or len(dataset_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in dataset_fingerprint)):
        raise ValueError("dataset_fingerprint must be a lowercase SHA-256 digest")
    source = tuple(m15_bars)
    if not source:
        raise ValueError("session feature replay requires non-empty M15 input")
    if any(not isinstance(bar, QuoteBar) for bar in source):
        raise TypeError("M15 input must contain QuoteBar values")
    if len({bar.availability_basis for bar in source}) != 1:
        raise ValueError("session feature input must use one availability basis")
    for index, bar in enumerate(source):
        if index and bar.available_at < source[index - 1].available_at:
            raise ValueError("M15 receipt times must be non-decreasing in bar order")
        if bar.availability_basis in (
            AvailabilityBasis.SYNTHETIC, AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
        ) and bar.available_at != bar.timestamp:
            raise ValueError("synthetic and historical-close availability must equal bar end")

    # EMA's validator also checks immutable input price/availability invariants.
    ema = m15_ema_events(source, policy=policy_bundle.confirmation_policy, calendar=calendar)
    h1 = aggregate_m15_to_h1(source, calendar=calendar)
    atr = h1_atr_events(
        h1, period=policy_bundle.impulse_policy.atr_period,
        method=policy_bundle.impulse_policy.atr_method, calendar=calendar,
    )
    pivots = confirmed_h1_pivots(h1, calendar=calendar)
    bars_json = _json_value(source)
    gaps = [
        {"start": _json_value(previous.timestamp), "end": _json_value(current.start_time)}
        for previous, current in zip(source, source[1:])
        if previous.timestamp != current.start_time
    ]
    report = {
        "schema": "xau_trader.session_feature_replay",
        "version": 1,
        "engine_version": SESSION_FEATURE_ENGINE_VERSION,
        "status": "session_features_complete",
        "diagnostic_only": True,
        "execution": None,
        "strategy_candidates_generated": False,
        "full_strategy_replay_supported": False,
        "dataset_fingerprint": dataset_fingerprint,
        "input_bars_fingerprint": _fingerprint(bars_json),
        "calendar_fingerprint": calendar.fingerprint,
        "calendar": calendar.as_dict(),
        "policy_bundle_fingerprint": policy_bundle.fingerprint,
        "policy_bundle": _json_value(policy_bundle),
        "semantics": {
            "clock": "real_utc_timestamps",
            "closures": "exact_declared_whole_utc_hours_only",
            "history": "all_supplied_open_bars_preserved_across_closures",
            "h1": "four_actual_m15_bars_in_one_utc_hour",
            "atr": "previous_traded_close_includes_reopening_price_jump",
            "ema": "seed_and_recursive_state_retained_across_closures",
            "pivots": "three_actual_h1_bars_on_each_side",
            "availability": "latest_dependency_receipt_no_backdating",
        },
        "counts": {
            "m15_bars": len(source), "h1_bars": len(h1),
            "atr_events": len(atr), "ema_events": len(ema),
            "pivot_events": len(pivots), "scheduled_gaps": len(gaps),
        },
        "warmup": {
            "atr_seeded": bool(atr), "ema_seeded": bool(ema),
            "pivot_window_available": len(h1) >= 7,
            "full_strategy_readiness_assessed": False,
        },
        "observed_closures": gaps,
        "m15_bars": bars_json,
        "h1_bars": _json_value(h1),
        "h1_atr": _json_value(atr),
        "h1_pivots": _json_value(pivots),
        "m15_ema": _json_value(ema),
        "warnings": [
            "Feature evidence only: no zones, confirmations, candidates, orders or returns.",
            "A calendar records source claims; hashes do not verify its truth or data-use rights.",
            "Partial-hour closures remain unsupported; no broker calendar is bundled.",
        ],
    }
    report["fingerprint"] = _fingerprint(report)
    return report
