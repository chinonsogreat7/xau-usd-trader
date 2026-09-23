"""Explicit, immutable provenance for offline MT5 complete-quote tick imports.

The plan records operator claims, not broker attestation or permission checks.
It never connects to MT5 and never infers a timezone, schedule, or symbol mapping.
"""

from dataclasses import dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Sequence, Tuple, Union
from urllib.parse import urlsplit

from .dataset_manifest import (
    AcquisitionBasis, BarConvention, BarTimeframe, DatasetCoverage, DatasetHashes,
    DatasetInstrument, DatasetManifest, DatasetProvider, DatasetRights, DatasetSource,
    IntervalConvention, KnownGap, SessionCalendar, TimestampConvention,
    validate_calendar_binding, validate_dataset_binding,
)
from .domain import AvailabilityBasis, QuoteBar
from .session_calendar import SessionCalendarArtifact


SCHEMA_NAME = "xau_trader.mt5_tick_import_plan"
SCHEMA_VERSION = 1
IMPORTER_VERSION = "mt5-complete-quotes-v1"
MAX_PLAN_BYTES = 1024 * 1024
_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]00:00)"
)
_PLACEHOLDERS = {
    "example", "na", "n_a", "none", "not_applicable", "not_available",
    "not_known", "not_sure", "null", "pending", "placeholder", "replace_me",
    "tbd", "t_b_d", "to_be_determined", "todo", "unknown", "unspecified",
}


class Mt5ImportPlanError(ValueError):
    """The import plan or its binding to local artifacts is invalid."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise Mt5ImportPlanError("{} must be a string".format(name))
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
           for char in value):
        raise Mt5ImportPlanError("{} contains invalid text characters".format(name))
    clean = value.strip()
    token = re.sub(r"[\W_]+", "_", clean.casefold()).strip("_")
    if (not token or token in _PLACEHOLDERS or token.startswith("replace_me_")
            or token.startswith("your_") or (clean.startswith("<") and clean.endswith(">"))):
        raise Mt5ImportPlanError("{} must be nonempty and not a placeholder".format(name))
    if re.search(
        r"\b(?:authorization|api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]"
        r"|\b(?:bearer|basic)\s+|://[^\s/@]+(?::[^\s/@]*)?@",
        clean, re.IGNORECASE,
    ):
        raise Mt5ImportPlanError("{} must not contain credentials".format(name))
    return clean


def _reference(value: object, name: str) -> str:
    clean = _text(value, name)
    if "?" in clean or "#" in clean:
        raise Mt5ImportPlanError("{} must omit query strings and fragments".format(name))
    try:
        parsed = urlsplit(clean)
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise Mt5ImportPlanError("{} is malformed".format(name)) from exc
    if has_credentials:
        raise Mt5ImportPlanError("{} must not contain URL credentials".format(name))
    return clean


def _sha256(value: object, name: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise Mt5ImportPlanError("{} must be an exact lowercase SHA256 digest".format(name))
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise Mt5ImportPlanError("{} must be a datetime".format(name))
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise Mt5ImportPlanError("{} must use an explicit UTC timezone".format(name))
    return value.astimezone(timezone.utc)


def _parse_utc(value: object, name: str) -> datetime:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise Mt5ImportPlanError("{} must be an ISO 8601 UTC timestamp".format(name))
    try:
        return _utc(datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value), name)
    except ValueError as exc:
        raise Mt5ImportPlanError("{} must be a valid UTC timestamp".format(name)) from exc


def _format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Mt5ImportSource:
    route: str
    account_environment: str
    acquisition_basis: AcquisitionBasis
    source_symbol: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _reference(self.route, "source.route"))
        for name in ("account_environment", "source_symbol"):
            object.__setattr__(self, name, _text(getattr(self, name), "source." + name))
        try:
            basis = AcquisitionBasis(self.acquisition_basis)
        except (ValueError, TypeError) as exc:
            raise Mt5ImportPlanError("source.acquisition_basis is not supported") from exc
        if basis not in (AcquisitionBasis.USER_EXPORT, AcquisitionBasis.SYNTHETIC_GENERATION):
            raise Mt5ImportPlanError("only user_export or synthetic_generation is supported")
        object.__setattr__(self, "acquisition_basis", basis)
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at, "source.retrieved_at"))


@dataclass(frozen=True)
class Mt5ImportTimestamp:
    utc_offset_minutes: int
    evidence_reference: str

    def __post_init__(self) -> None:
        offset = self.utc_offset_minutes
        if isinstance(offset, bool) or not isinstance(offset, int) or not -840 <= offset <= 840:
            raise Mt5ImportPlanError("timestamp.utc_offset_minutes must be an integer from -840 to 840")
        object.__setattr__(self, "evidence_reference", _reference(
            self.evidence_reference, "timestamp.evidence_reference"
        ))


@dataclass(frozen=True)
class Mt5ImportCoverage:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for name in ("start", "end"):
            value = _utc(getattr(self, name), "coverage." + name)
            if value.minute or value.second or value.microsecond:
                raise Mt5ImportPlanError("coverage must begin and end on whole UTC hours")
            object.__setattr__(self, name, value)
        if self.end <= self.start:
            raise Mt5ImportPlanError("coverage.end must be after coverage.start")


@dataclass(frozen=True)
class Mt5ImportPlan:
    provider: DatasetProvider
    instrument: DatasetInstrument
    source: Mt5ImportSource
    rights: DatasetRights
    timestamp: Mt5ImportTimestamp
    coverage: Mt5ImportCoverage
    raw_sha256: str
    session_calendar: SessionCalendar
    schema: str = SCHEMA_NAME
    version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_NAME:
            raise Mt5ImportPlanError("schema is not supported")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version != 1:
            raise Mt5ImportPlanError("version is not supported")
        for name, expected in (
            ("provider", DatasetProvider), ("instrument", DatasetInstrument),
            ("source", Mt5ImportSource), ("rights", DatasetRights),
            ("timestamp", Mt5ImportTimestamp), ("coverage", Mt5ImportCoverage),
            ("session_calendar", SessionCalendar),
        ):
            if not isinstance(getattr(self, name), expected):
                raise Mt5ImportPlanError("{} must be a {}".format(name, expected.__name__))
        # The existing public metadata classes retain v1 behavior. Add the stricter
        # text requirements at this new boundary, including direct construction.
        for name in ("provider", "instrument", "rights"):
            instance = getattr(self, name)
            for field in fields(instance):
                _text(getattr(instance, field.name), name + "." + field.name)
        for name in ("calendar_id", "version"):
            _text(getattr(self.session_calendar, name), "session_calendar." + name)
        _reference(self.session_calendar.artifact_uri, "session_calendar.artifact_uri")
        _sha256(self.session_calendar.artifact_sha256, "session_calendar.artifact_sha256")
        _sha256(self.session_calendar.content_fingerprint, "session_calendar.content_fingerprint")
        _sha256(self.raw_sha256, "raw_sha256")
        if self.source.retrieved_at < self.coverage.end:
            raise Mt5ImportPlanError("source.retrieved_at cannot precede coverage.end")

    @property
    def availability_basis(self) -> AvailabilityBasis:
        if self.source.acquisition_basis == AcquisitionBasis.USER_EXPORT:
            return AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION
        return AvailabilityBasis.SYNTHETIC

    @property
    def coverage_start(self) -> datetime:
        return self.coverage.start

    @property
    def coverage_end(self) -> datetime:
        return self.coverage.end

    @property
    def utc_offset_minutes(self) -> int:
        return self.timestamp.utc_offset_minutes

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema, "version": self.version,
            "provider": {"name": self.provider.name, "legal_entity": self.provider.legal_entity},
            "instrument": {"symbol": self.instrument.symbol, "product_form": self.instrument.product_form},
            "source": {
                "route": self.source.route, "account_environment": self.source.account_environment,
                "acquisition_basis": self.source.acquisition_basis.value,
                "source_symbol": self.source.source_symbol,
                "retrieved_at": _format_utc(self.source.retrieved_at),
            },
            "rights": {field.name: getattr(self.rights, field.name) for field in fields(self.rights)},
            "timestamp": {"utc_offset_minutes": self.timestamp.utc_offset_minutes,
                          "evidence_reference": self.timestamp.evidence_reference},
            "coverage": {"start": _format_utc(self.coverage.start), "end": _format_utc(self.coverage.end)},
            "raw_sha256": self.raw_sha256,
            "session_calendar": {
                "id": self.session_calendar.calendar_id, "version": self.session_calendar.version,
                "artifact_uri": self.session_calendar.artifact_uri,
                "artifact_sha256": self.session_calendar.artifact_sha256,
                "content_fingerprint": self.session_calendar.content_fingerprint,
            },
        }

    @property
    def canonical_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def _object(value: object, expected: set, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise Mt5ImportPlanError("{} must be an object with string field names".format(name))
    for key in value:
        _text(key, name + " field name")
    missing, unknown = expected - set(value), set(value) - expected
    if missing:
        raise Mt5ImportPlanError("{} is missing fields: {}".format(name, ", ".join(sorted(missing))))
    if unknown:
        raise Mt5ImportPlanError("{} has unknown fields: {}".format(name, ", ".join(sorted(unknown))))
    return value


def mt5_import_plan_from_dict(payload: Mapping[str, Any]) -> Mt5ImportPlan:
    """Parse the closed plan schema without inventing source or rights claims."""
    root = _object(payload, {"schema", "version", "provider", "instrument", "source", "rights",
                             "timestamp", "coverage", "raw_sha256", "session_calendar"}, "plan")
    provider = _object(root["provider"], {"name", "legal_entity"}, "provider")
    instrument = _object(root["instrument"], {"symbol", "product_form"}, "instrument")
    source = _object(root["source"], {"route", "account_environment", "acquisition_basis",
                                      "source_symbol", "retrieved_at"}, "source")
    rights = _object(root["rights"], {"basis", "api_data_agreement_version", "retention_basis"}, "rights")
    timestamp = _object(root["timestamp"], {"utc_offset_minutes", "evidence_reference"}, "timestamp")
    coverage = _object(root["coverage"], {"start", "end"}, "coverage")
    calendar = _object(root["session_calendar"], {"id", "version", "artifact_uri",
                                                  "artifact_sha256", "content_fingerprint"}, "session_calendar")
    try:
        # Validate before public metadata normalizes leading/trailing whitespace.
        for name, section in (("provider", provider), ("instrument", instrument), ("rights", rights)):
            for key, value in section.items():
                _text(value, name + "." + key)
        for key in ("id", "version"):
            _text(calendar[key], "session_calendar." + key)
        _reference(calendar["artifact_uri"], "session_calendar.artifact_uri")
        return Mt5ImportPlan(
            schema=root["schema"], version=root["version"],
            provider=DatasetProvider(**provider), instrument=DatasetInstrument(**instrument),
            source=Mt5ImportSource(
                route=source["route"], account_environment=source["account_environment"],
                acquisition_basis=source["acquisition_basis"], source_symbol=source["source_symbol"],
                retrieved_at=_parse_utc(source["retrieved_at"], "source.retrieved_at"),
            ),
            rights=DatasetRights(**rights), timestamp=Mt5ImportTimestamp(**timestamp),
            coverage=Mt5ImportCoverage(
                start=_parse_utc(coverage["start"], "coverage.start"),
                end=_parse_utc(coverage["end"], "coverage.end"),
            ),
            raw_sha256=root["raw_sha256"],
            session_calendar=SessionCalendar(
                calendar_id=calendar["id"], version=calendar["version"],
                artifact_uri=calendar["artifact_uri"], artifact_sha256=calendar["artifact_sha256"],
                content_fingerprint=calendar["content_fingerprint"],
            ),
        )
    except Mt5ImportPlanError:
        raise
    except (ValueError, TypeError) as exc:
        raise Mt5ImportPlanError(str(exc)) from exc


def _reject_duplicate_fields(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        _text(key, "JSON field name")
        if key in result:
            raise Mt5ImportPlanError("duplicate JSON field: {}".format(key))
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise Mt5ImportPlanError("non-standard JSON constant: {}".format(value))


def parse_mt5_import_plan_bytes(raw: bytes) -> Mt5ImportPlan:
    """Parse an exact, bounded UTF-8 snapshot for caller-side artifact hashing."""
    if not isinstance(raw, bytes):
        raise Mt5ImportPlanError("import plan snapshot must be bytes")
    if not raw or len(raw) > MAX_PLAN_BYTES:
        raise Mt5ImportPlanError("import plan must be nonempty and at most 1 MiB")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_fields,
                             parse_constant=_reject_json_constant)
    except Mt5ImportPlanError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise Mt5ImportPlanError("import plan is not valid UTF-8 JSON") from exc
    return mt5_import_plan_from_dict(payload)


def load_mt5_import_plan(path: Union[str, Path]) -> Mt5ImportPlan:
    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_PLAN_BYTES + 1)
    except OSError as exc:
        raise Mt5ImportPlanError("could not read {}: {}".format(source, exc)) from exc
    return parse_mt5_import_plan_bytes(raw)


def build_mt5_import_manifest(
    plan: Mt5ImportPlan, bars: Sequence[QuoteBar], calendar: SessionCalendarArtifact, *,
    raw_sha256: str, normalized_csv_sha256: str, calendar_artifact_sha256: str,
) -> DatasetManifest:
    """Create a v2 sidecar only after exact local-data/calendar binding succeeds."""
    if not isinstance(plan, Mt5ImportPlan):
        raise Mt5ImportPlanError("plan must be a Mt5ImportPlan")
    if not isinstance(calendar, SessionCalendarArtifact):
        raise Mt5ImportPlanError("calendar must be a SessionCalendarArtifact")
    raw_sha256 = _sha256(raw_sha256, "raw_sha256")
    normalized_csv_sha256 = _sha256(normalized_csv_sha256, "normalized_csv_sha256")
    calendar_artifact_sha256 = _sha256(calendar_artifact_sha256, "calendar_artifact_sha256")
    if raw_sha256 != plan.raw_sha256:
        raise Mt5ImportPlanError("raw_sha256 does not match the import plan")
    actual_bars = tuple(bars)
    if not actual_bars or any(not isinstance(bar, QuoteBar) for bar in actual_bars):
        raise Mt5ImportPlanError("bars must contain at least one QuoteBar and only QuoteBar values")
    if (actual_bars[0].start_time != plan.coverage.start
            or actual_bars[-1].timestamp != plan.coverage.end):
        raise Mt5ImportPlanError("bar coverage must exactly match the import plan")
    try:
        calendar.require_hour_aligned_closures()
        gaps = []
        previous = None
        for bar in actual_bars:
            calendar.validate_bar(bar.start_time, bar.timestamp)
            if previous is not None:
                calendar.validate_transition(previous.timestamp, bar.start_time)
                if previous.timestamp < bar.start_time:
                    closure = next(item for item in calendar.closures
                                   if item.start == previous.timestamp and item.end == bar.start_time)
                    gaps.append(KnownGap(start=closure.start, end=closure.end, reason=closure.reason))
            previous = bar
        manifest = DatasetManifest(
            schema="xau_trader.dataset_manifest", version=2, provider=plan.provider,
            instrument=plan.instrument, rights=plan.rights, session_calendar=plan.session_calendar,
            source=DatasetSource(
                route=plan.source.route, account_environment=plan.source.account_environment,
                acquisition_basis=plan.source.acquisition_basis,
                request_parameters=(
                    ("importer_version", IMPORTER_VERSION),
                    ("import_plan_fingerprint", plan.fingerprint),
                    ("source_symbol", plan.source.source_symbol),
                    ("utc_offset_minutes", str(plan.timestamp.utc_offset_minutes)),
                    ("timezone_evidence_reference", plan.timestamp.evidence_reference),
                    ("quote_policy", "complete_bid_ask_snapshots_no_fill"),
                    ("bar_open_semantics", "first_observed_tick_not_guaranteed_boundary_quote"),
                    ("tick_completeness", "operator_claim_not_verified"),
                ),
                time_normalization_rule=(
                    "Subtract the fixed signed UTC offset of {} minutes from each source tick "
                    "timestamp; bucket into half-open UTC M15 intervals. This operator-supplied "
                    "offset is not a daylight-saving schedule or broker-timezone attestation."
                ).format(plan.timestamp.utc_offset_minutes),
                retrieved_at=plan.source.retrieved_at,
            ),
            bars=BarConvention(
                timeframe=BarTimeframe.M15, duration_seconds=900, timezone="UTC",
                interval=IntervalConvention.HALF_OPEN, timestamp=TimestampConvention.COMPLETED_BAR_END,
                availability_basis=plan.availability_basis,
            ),
            hashes=DatasetHashes(raw_sha256=raw_sha256, normalized_csv_sha256=normalized_csv_sha256),
            coverage=DatasetCoverage(start=plan.coverage.start, end=plan.coverage.end,
                                     row_count=len(actual_bars), known_gaps=tuple(gaps)),
        )
        validate_dataset_binding(manifest, actual_bars, raw_sha256=raw_sha256,
                                 normalized_csv_sha256=normalized_csv_sha256)
        validate_calendar_binding(manifest, calendar, artifact_sha256=calendar_artifact_sha256)
        return manifest
    except ValueError as exc:
        raise Mt5ImportPlanError(str(exc)) from exc


__all__ = [
    "SCHEMA_NAME", "SCHEMA_VERSION", "IMPORTER_VERSION", "MAX_PLAN_BYTES", "Mt5ImportPlanError", "Mt5ImportSource",
    "Mt5ImportTimestamp", "Mt5ImportCoverage", "Mt5ImportPlan", "mt5_import_plan_from_dict",
    "parse_mt5_import_plan_bytes", "load_mt5_import_plan", "build_mt5_import_manifest",
]
