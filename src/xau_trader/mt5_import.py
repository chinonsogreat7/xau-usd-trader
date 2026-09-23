"""Offline, auditable MT5 complete-quote tick normalization; never execution."""

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Sequence, Tuple

from .data import quote_bars_csv_bytes
from .dataset_manifest import validate_calendar_binding, validate_dataset_binding
from .mt5_import_plan import MAX_PLAN_BYTES, build_mt5_import_manifest, parse_mt5_import_plan_bytes
from .mt5_ticks import MAX_RAW_BYTES, aggregate_mt5_ticks_m15, parse_mt5_tick_bytes
from .session_calendar import MAX_CALENDAR_BYTES, parse_session_calendar_bytes


def _json_value(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def _read_snapshot(path: Path, limit: int) -> bytes:
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("MT5 import inputs must be regular files")
    if metadata.st_size > limit:
        raise ValueError("MT5 import input exceeds its supported byte limit")
    with path.open("rb") as handle:
        snapshot = handle.read(limit + 1)
    if len(snapshot) > limit:
        raise ValueError("MT5 import input exceeds its supported byte limit")
    return snapshot


def _validate_outputs(inputs: Sequence[Path], outputs: Sequence[Path]) -> None:
    protected = {path.resolve(strict=False) for path in inputs}
    resolved = tuple(path.resolve(strict=False) for path in outputs)
    if len(set(resolved)) != len(resolved):
        raise ValueError("import output paths must be different")
    if any(path in protected for path in resolved):
        raise ValueError("an import output cannot replace an input")
    for path in outputs:
        if path.exists() or path.is_symlink():
            raise FileExistsError("import output already exists: {}".format(path))


def _publish_new_artifacts(payloads: Sequence[Tuple[Path, bytes]]) -> None:
    """Stage all bytes, exclusively publish, and roll back our links on failure.

    This is not a crash-atomic multi-file transaction. Retain and compare the
    report/manifest hashes to detect an interrupted process or changed artifact.
    """
    staged = []
    identities = []
    created = []
    try:
        for target, payload in payloads:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=".{}-".format(target.name), suffix=".tmp", dir=str(target.parent),
            )
            staging = Path(name)
            staged.append(staging)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                identities.append(os.fstat(handle.fileno()))
        for (target, _), staging, identity in zip(payloads, staged, identities):
            os.link(str(staging), str(target))
            created.append((target, identity))
    except BaseException:
        # Do not remove another writer's file if it replaced one of our links.
        for target, identity in reversed(created):
            try:
                current = target.lstat()
                if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    target.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        for staging in staged:
            try:
                staging.unlink()
            except FileNotFoundError:
                pass


def import_mt5_tick_file(
    raw_path: Path,
    *,
    plan_path: Path,
    calendar_path: Path,
    output_csv: Path,
    output_manifest: Path,
    report_json: Path,
) -> dict:
    """Normalize a bounded local export after validating explicit source claims.

    The first observed tick supplies each bar's open. That quote is not proof
    of a fill at the nominal bar boundary. No strategy or simulator runs here.
    """
    inputs = tuple(Path(path) for path in (raw_path, plan_path, calendar_path))
    outputs = tuple(Path(path) for path in (output_csv, output_manifest, report_json))
    raw_path, plan_path, calendar_path = inputs
    _validate_outputs(inputs, outputs)
    # Hash exactly the bytes parsed, including metadata. Separate path reads for
    # hashing and parsing could attest to A while parsing a transient version B.
    limits = (MAX_RAW_BYTES, MAX_PLAN_BYTES, MAX_CALENDAR_BYTES)
    snapshots = tuple(_read_snapshot(path, limit) for path, limit in zip(inputs, limits))
    before = tuple(hashlib.sha256(snapshot).hexdigest() for snapshot in snapshots)
    raw, plan_bytes, calendar_bytes = snapshots
    plan = parse_mt5_import_plan_bytes(plan_bytes)
    calendar = parse_session_calendar_bytes(calendar_bytes)
    parsed = parse_mt5_tick_bytes(raw, utc_offset_minutes=plan.utc_offset_minutes)
    if parsed.raw_sha256 != before[0] or plan.raw_sha256 != parsed.raw_sha256:
        raise ValueError("raw export hash does not match the import plan or input snapshot")
    aggregated = aggregate_mt5_ticks_m15(
        parsed, coverage_start=plan.coverage_start, coverage_end=plan.coverage_end,
        calendar=calendar, availability_basis=plan.availability_basis,
    )
    normalized = quote_bars_csv_bytes(aggregated.bars)
    normalized_sha256 = hashlib.sha256(normalized).hexdigest()
    manifest = build_mt5_import_manifest(
        plan, aggregated.bars, calendar,
        raw_sha256=parsed.raw_sha256, normalized_csv_sha256=normalized_sha256,
        calendar_artifact_sha256=before[2],
    )
    binding = validate_dataset_binding(
        manifest, aggregated.bars, raw_sha256=parsed.raw_sha256,
        normalized_csv_sha256=normalized_sha256,
    )
    calendar_binding = validate_calendar_binding(
        manifest, calendar, artifact_sha256=before[2],
    )
    after = tuple(hashlib.sha256(_read_snapshot(path, limit)).hexdigest()
                  for path, limit in zip(inputs, limits))
    if before != after:
        raise ValueError("MT5 import inputs changed while they were being read")
    manifest_bytes = (manifest.canonical_json + "\n").encode("utf-8")
    report = {
        "schema": "xau_trader.mt5_tick_import_report", "version": 1,
        "status": "mt5_tick_normalization_complete", "diagnostic_only": True,
        "execution": None, "strategy_replay_executed": False,
        "strategy_readiness_assessed": False, "tick_completeness_verified": False,
        "source_identity_independently_verified": False, "data_rights_verified": False,
        "import_plan": plan.as_dict(), "import_plan_fingerprint": plan.fingerprint,
        "input_hashes": {
            "raw_export_sha256": before[0], "import_plan_file_sha256": before[1],
            "calendar_file_sha256": before[2],
        },
        "normalized_csv_sha256": normalized_sha256,
        "manifest_file_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_fingerprint": manifest.fingerprint,
        "calendar_binding": calendar_binding,
        "format": {
            "encoding": parsed.encoding, "delimiter": parsed.delimiter,
            "columns": list(parsed.columns),
            "quote_policy": "complete_bid_ask_snapshots_no_fill",
            "equal_tick_timestamps": "preserve_original_file_order",
            "availability_basis": plan.availability_basis.value,
            "volume": "unknown_not_tick_count",
        },
        "counts": {
            "input_ticks": aggregated.tick_count, "m15_bars": len(aggregated.bars),
            "h1_hours": len(aggregated.bars) // 4,
            "scheduled_gaps": binding.known_gap_count, "excluded_ticks": 0,
            "invented_bars": 0,
        },
        "maximum_intertick_seconds_including_closures": aggregated.maximum_intertick_seconds,
        "maximum_boundary_silence_seconds": aggregated.maximum_boundary_silence_seconds,
        "bar_tick_evidence": _json_value(aggregated.bucket_diagnostics),
        "warnings": list(aggregated.warnings) + [
            "Normalization validates recorded inputs, not source truth, tick completeness, or data-use rights.",
            "A bar's first observed quote is not proof of an executable quote at the bar boundary.",
            "Historical close-time availability is an assumption, not measured live receipt latency.",
            "No strategy, order, fill, account operation, or profit/loss calculation ran.",
        ],
    }
    report["fingerprint"] = _fingerprint(report)
    report_bytes = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False,
                               allow_nan=False) + "\n").encode("utf-8")
    _publish_new_artifacts(tuple(zip(outputs, (normalized, manifest_bytes, report_bytes))))
    return {
        "status": report["status"], "diagnostic_only": True, "execution": None,
        "strategy_replay_executed": False, "tick_completeness_verified": False,
        "counts": report["counts"], "report_fingerprint": report["fingerprint"],
        "manifest_identity": manifest.identity,
        "outputs": {
            "csv": str(outputs[0]), "manifest": str(outputs[1]), "report_json": str(outputs[2]),
        },
        "warnings": report["warnings"],
    }
