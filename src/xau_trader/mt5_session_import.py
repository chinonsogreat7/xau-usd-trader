"""Bounded offline session-bucket evidence, not legacy bars or trade execution.

This deliberately produces a distinct artifact. Its partial-session observations
cannot be supplied as the existing M15 replay manifest or an execution scenario.
"""

import hashlib
import json
from pathlib import Path

from .mt5_import import _fingerprint, _json_value, _publish_new_artifacts, _read_snapshot, _validate_outputs
from .mt5_import_plan import MAX_PLAN_BYTES, parse_mt5_import_plan_bytes
from .mt5_ticks import MAX_RAW_BYTES, parse_mt5_tick_bytes
from .session_calendar import MAX_CALENDAR_BYTES, parse_session_calendar_bytes


SCHEMA_NAME = "xau_trader.mt5_session_bucket_import"
SCHEMA_VERSION = 1


def _bind_calendar(plan, calendar, calendar_file_sha256):
    """Bind supplied claims without invoking the full-hour legacy manifest."""
    reference = plan.session_calendar
    checks = (
        (calendar_file_sha256, reference.artifact_sha256, "calendar artifact_sha256"),
        (calendar.fingerprint, reference.content_fingerprint, "calendar content_fingerprint"),
        (calendar.calendar_id, reference.calendar_id, "calendar id"),
        (calendar.revision, reference.version, "calendar revision"),
        (calendar.provider_name, plan.provider.name, "calendar provider_name"),
        (calendar.provider_legal_entity, plan.provider.legal_entity, "calendar provider_legal_entity"),
        (calendar.instrument, plan.instrument.symbol, "calendar instrument"),
        (calendar.product_form, plan.instrument.product_form, "calendar product_form"),
    )
    for actual, expected, field in checks:
        if actual != expected:
            raise ValueError("{} does not match the import plan".format(field))
    if calendar.coverage_start > plan.coverage_start or calendar.coverage_end < plan.coverage_end:
        raise ValueError("calendar coverage must contain import coverage")
    if calendar.retrieved_at > plan.source.retrieved_at:
        raise ValueError("calendar retrieved_at must not follow source.retrieved_at")
    return {
        "calendar_id": calendar.calendar_id, "revision": calendar.revision,
        "artifact_sha256": calendar_file_sha256, "content_fingerprint": calendar.fingerprint,
    }


def import_mt5_session_tick_file(
    raw_path: Path, *, plan_path: Path, calendar_path: Path, output_json: Path,
) -> dict:
    """Preserve bounded source-order ticks and classify their scheduled sessions.

    Input metadata is operator evidence, not independently verified provider
    truth. No connection, strategy replay, order, fill or P&L calculation runs.
    """
    # Lazy imports keep the CLI's unrelated commands independent of this opt-in
    # diagnostic path and avoid promoting it to a legacy replay entry point.
    from .session_buckets import aggregate_mt5_session_buckets
    from .session_bucket_policy import evaluate_session_bucket_policy

    inputs = tuple(Path(path) for path in (raw_path, plan_path, calendar_path))
    output = Path(output_json)
    _validate_outputs(inputs, (output,))
    limits = (MAX_RAW_BYTES, MAX_PLAN_BYTES, MAX_CALENDAR_BYTES)
    snapshots = tuple(_read_snapshot(path, limit) for path, limit in zip(inputs, limits))
    hashes = tuple(hashlib.sha256(snapshot).hexdigest() for snapshot in snapshots)
    raw, plan_bytes, calendar_bytes = snapshots
    plan = parse_mt5_import_plan_bytes(plan_bytes)
    calendar = parse_session_calendar_bytes(calendar_bytes)
    binding = _bind_calendar(plan, calendar, hashes[2])
    parsed = parse_mt5_tick_bytes(raw, utc_offset_minutes=plan.utc_offset_minutes)
    if parsed.raw_sha256 != hashes[0] or plan.raw_sha256 != parsed.raw_sha256:
        raise ValueError("raw export hash does not match the import plan or input snapshot")
    aggregation = aggregate_mt5_session_buckets(
        parsed, coverage_start=plan.coverage_start, coverage_end=plan.coverage_end,
        calendar=calendar, availability_basis=plan.availability_basis,
    )
    aggregation_dict = aggregation.as_dict()
    session_policy = evaluate_session_bucket_policy(aggregation)
    counts = {
        **aggregation_dict["summary"],
        "input_ticks": len(parsed.ticks), "preserved_ticks": len(parsed.ticks),
        "excluded_ticks": 0, "invented_ticks": 0,
    }
    report = {
        "schema": SCHEMA_NAME, "version": SCHEMA_VERSION,
        "status": "mt5_session_bucket_import_complete", "diagnostic_only": True,
        "legacy_replay_admitted": False, "strategy_replay_executed": False,
        "execution_admitted": False, "execution": None,
        "broker_connected": False, "orders_submitted": 0,
        "strategy_readiness_assessed": False, "tick_completeness_verified": False,
        "source_identity_independently_verified": False, "data_rights_verified": False,
        "import_plan": plan.as_dict(), "import_plan_fingerprint": plan.fingerprint,
        "calendar": calendar.as_dict(), "calendar_fingerprint": calendar.fingerprint,
        "calendar_binding": binding,
        "input_hashes": {
            "raw_export_sha256": hashes[0], "import_plan_file_sha256": hashes[1],
            "calendar_file_sha256": hashes[2],
        },
        "format": {
            "encoding": parsed.encoding, "delimiter": parsed.delimiter,
            "columns": list(parsed.columns),
            "quote_policy": "complete_bid_ask_snapshots_no_fill",
            "equal_tick_timestamps": "preserve_original_file_order",
            "availability_basis": plan.availability_basis.value,
            "timestamp_offset_minutes": plan.utc_offset_minutes,
        },
        "ticks": _json_value(parsed.ticks),
        "aggregation": aggregation_dict,
        "session_policy": session_policy,
        "counts": counts,
        "warnings": [
            "Synthetic fixtures remain synthetic; user exports retain their declared source identity.",
            "Recorded schedules, UTC offsets and permissions are operator claims, not independently verified.",
            "Observed quotes do not prove feed completeness, live receipt latency or executable boundary fills.",
            "Partial-session observations are not admitted into existing strategy replay or execution commands.",
            "No broker connection, trading setting change, order, fill or profit/loss calculation ran.",
        ],
    }
    report["fingerprint"] = _fingerprint(report)
    payload = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False,
                          allow_nan=False) + "\n").encode("utf-8")
    # Re-read only after building the complete artifact, so a mutation during
    # parsing, classification, policy evaluation or serialization is detected.
    after = tuple(hashlib.sha256(_read_snapshot(path, limit)).hexdigest()
                  for path, limit in zip(inputs, limits))
    if after != hashes:
        raise ValueError("MT5 session import inputs changed before publication")
    _validate_outputs(inputs, (output,))
    _publish_new_artifacts(((output, payload),))
    return {
        "status": report["status"], "diagnostic_only": True,
        "legacy_replay_admitted": False, "strategy_replay_executed": False,
        "execution_admitted": False, "execution": None,
        "counts": counts, "report_fingerprint": report["fingerprint"],
        "policy_version": session_policy["semantics"]["version"],
        "policy_scope": session_policy["scope"],
        "policy_counts": session_policy["counts"],
        "missing_open_data_blocks_replay": session_policy["missing_open_data_blocks_replay"],
        "output_json": str(output), "warnings": report["warnings"],
    }
