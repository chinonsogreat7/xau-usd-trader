"""Strict, provider-neutral identity and provenance for one market-data dataset.

The manifest deliberately records claims; it does not infer provider rights, session
closures, acquisition routes, or bar availability.  Binding validation succeeds only
when the caller supplies the exact loaded bars and both externally computed hashes.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from .domain import AvailabilityBasis, QuoteBar

if TYPE_CHECKING:
    from .session_calendar import SessionCalendarArtifact


SCHEMA_NAME = "xau_trader.dataset_manifest"
SCHEMA_VERSION = 1
SUPPORTED_VERSIONS = (1, 2)
SUPPORTED_INSTRUMENT = "XAU_USD"


class DatasetManifestError(ValueError):
    """Raised when a manifest or its binding to market data is invalid."""


class AcquisitionBasis(str, Enum):
    API_RESPONSE = "api_response"
    USER_EXPORT = "user_export"
    PROVIDER_DOWNLOAD = "provider_download"
    SYNTHETIC_GENERATION = "synthetic_generation"


class BarTimeframe(str, Enum):
    M15 = "M15"


class IntervalConvention(str, Enum):
    HALF_OPEN = "start_inclusive_end_exclusive"


class TimestampConvention(str, Enum):
    COMPLETED_BAR_END = "completed_bar_end"


_PLACEHOLDERS = {
    "example",
    "na",
    "none",
    "not_applicable",
    "not_known",
    "not_sure",
    "null",
    "pending",
    "placeholder",
    "replace_me",
    "tbd",
    "todo",
    "unknown",
    "unspecified",
}

_CREDENTIAL_FIELD_NAMES = {
    "apikey",
    "authorization",
    "authorizationheader",
    "auth",
    "authheader",
    "bearer",
    "cookie",
    "privatekey",
    "secretkey",
    "signature",
}


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise DatasetManifestError("{} must be a string".format(field_name))
    clean = value.strip()
    if not clean:
        raise DatasetManifestError("{} must not be empty".format(field_name))
    token = "_".join(clean.lower().replace("-", " ").split())
    if (
        token in _PLACEHOLDERS
        or token.startswith("replace_me_")
        or token.startswith("your_")
        or (clean.startswith("<") and clean.endswith(">"))
    ):
        raise DatasetManifestError("{} must not be a placeholder".format(field_name))
    return clean


def _redacted_route(value: object) -> str:
    """Accept an endpoint/route identifier, never an authenticated URL."""

    clean = _required_text(value, "source.route")
    if "?" in clean or "#" in clean:
        raise DatasetManifestError(
            "source.route must omit query strings and fragments; use redacted request_parameters"
        )
    parsed = urlsplit(clean)
    if parsed.username is not None or parsed.password is not None:
        raise DatasetManifestError("source.route must not contain URL credentials")
    lowered = clean.lower()
    if "authorization:" in lowered or "bearer " in lowered or "basic " in lowered:
        raise DatasetManifestError("source.route must not contain credentials")
    return clean


def _request_parameter_name(value: object) -> str:
    name = _required_text(value, "source.request_parameters name")
    compact = "".join(character for character in name.lower() if character.isalnum())
    credential_like = (
        compact in _CREDENTIAL_FIELD_NAMES
        or compact.endswith("token")
        or compact.endswith("secret")
        or compact.endswith("password")
        or compact.endswith("passphrase")
        or compact.endswith("credential")
        or compact.endswith("credentials")
        or "apikey" in compact
        or compact.endswith("accesskey")
        or compact.endswith("privatekey")
    )
    if credential_like:
        raise DatasetManifestError(
            "source.request_parameters must not contain credential-like field names"
        )
    return name


def _redacted_parameter_value(value: object, field_name: str) -> str:
    clean = _required_text(value, field_name)
    lowered = clean.lower()
    if "\n" in clean or "\r" in clean:
        raise DatasetManifestError("{} must be a single-line redacted value".format(field_name))
    if lowered.startswith("bearer ") or lowered.startswith("basic "):
        raise DatasetManifestError("{} must not contain credentials".format(field_name))
    return clean


def _utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise DatasetManifestError("{} must be a datetime".format(field_name))
    if value.tzinfo is None or value.utcoffset() is None:
        raise DatasetManifestError("{} must include an explicit UTC offset".format(field_name))
    if value.utcoffset().total_seconds() != 0:
        raise DatasetManifestError("{} must use UTC".format(field_name))
    return value.astimezone(timezone.utc)


def _sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise DatasetManifestError("{} must be a string".format(field_name))
    digest = value.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise DatasetManifestError(
            "{} must be a 64-character hexadecimal SHA256 digest".format(field_name)
        )
    return digest


def _calendar_sha256(value: object, field_name: str) -> str:
    """Calendar identities use strict lowercase digests without normalization."""

    if not isinstance(value, str):
        raise DatasetManifestError("{} must be a string".format(field_name))
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise DatasetManifestError(
            "{} must be a 64-character lowercase hexadecimal SHA256 digest".format(field_name)
        )
    return value


def _calendar_artifact_uri(value: object) -> str:
    field_name = "session_calendar.artifact_uri"
    clean = _required_text(value, field_name)
    if any(
        ord(character) < 32
        or 127 <= ord(character) <= 159
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise DatasetManifestError("{} must be a single-line redacted URI".format(field_name))
    if "?" in clean or "#" in clean:
        raise DatasetManifestError("{} must omit query strings and fragments".format(field_name))
    try:
        parsed = urlsplit(clean)
    except ValueError as exc:
        raise DatasetManifestError("{} must be a valid redacted URI".format(field_name)) from exc
    if parsed.username is not None or parsed.password is not None:
        raise DatasetManifestError("{} must not contain URL credentials".format(field_name))
    if re.search(r"\bauthorization\s*[:=]|\b(?:bearer|basic)\s+", clean, re.IGNORECASE):
        raise DatasetManifestError("{} must not contain credentials".format(field_name))
    return clean


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatasetManifestError("{} must be an integer".format(field_name))
    return value


def _enum(enum_type: Any, value: object, field_name: str) -> Any:
    if not isinstance(value, (str, enum_type)):
        raise DatasetManifestError("{} is not supported".format(field_name))
    try:
        return enum_type(value)
    except ValueError as exc:
        raise DatasetManifestError("{} is not supported".format(field_name)) from exc


@dataclass(frozen=True)
class DatasetProvider:
    name: str
    legal_entity: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _required_text(self.name, "provider.name"))
        object.__setattr__(
            self,
            "legal_entity",
            _required_text(self.legal_entity, "provider.legal_entity"),
        )


@dataclass(frozen=True)
class DatasetInstrument:
    symbol: str
    product_form: str

    def __post_init__(self) -> None:
        symbol = _required_text(self.symbol, "instrument.symbol")
        if symbol != SUPPORTED_INSTRUMENT:
            raise DatasetManifestError("instrument.symbol is not supported")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(
            self,
            "product_form",
            _required_text(self.product_form, "instrument.product_form"),
        )


@dataclass(frozen=True)
class DatasetSource:
    route: str
    account_environment: str
    acquisition_basis: AcquisitionBasis
    request_parameters: Tuple[Tuple[str, str], ...]
    time_normalization_rule: str
    retrieved_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _redacted_route(self.route))
        object.__setattr__(
            self,
            "account_environment",
            _required_text(self.account_environment, "source.account_environment"),
        )
        object.__setattr__(
            self,
            "acquisition_basis",
            _enum(AcquisitionBasis, self.acquisition_basis, "source.acquisition_basis"),
        )
        parameters: List[Tuple[str, str]] = []
        for parameter in tuple(self.request_parameters):
            if not isinstance(parameter, tuple) or len(parameter) != 2:
                raise DatasetManifestError(
                    "source.request_parameters must contain name/value pairs"
                )
            name = _request_parameter_name(parameter[0])
            value = _redacted_parameter_value(
                parameter[1], "source.request_parameters.{}".format(name)
            )
            parameters.append((name, value))
        if not parameters:
            raise DatasetManifestError("source.request_parameters must not be empty")
        parameters.sort(key=lambda item: item[0])
        if len({name for name, _ in parameters}) != len(parameters):
            raise DatasetManifestError("source.request_parameters names must be unique")
        object.__setattr__(self, "request_parameters", tuple(parameters))
        object.__setattr__(
            self,
            "time_normalization_rule",
            _required_text(self.time_normalization_rule, "source.time_normalization_rule"),
        )
        object.__setattr__(
            self,
            "retrieved_at",
            _utc(self.retrieved_at, "source.retrieved_at"),
        )


@dataclass(frozen=True)
class BarConvention:
    timeframe: BarTimeframe
    duration_seconds: int
    timezone: str
    interval: IntervalConvention
    timestamp: TimestampConvention
    availability_basis: AvailabilityBasis

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "timeframe", _enum(BarTimeframe, self.timeframe, "bars.timeframe")
        )
        duration = _integer(self.duration_seconds, "bars.duration_seconds")
        if duration != 15 * 60:
            raise DatasetManifestError("bars.duration_seconds must be 900 for M15")
        object.__setattr__(self, "duration_seconds", duration)
        zone = _required_text(self.timezone, "bars.timezone")
        if zone != "UTC":
            raise DatasetManifestError("bars.timezone must be UTC")
        object.__setattr__(self, "timezone", zone)
        object.__setattr__(
            self, "interval", _enum(IntervalConvention, self.interval, "bars.interval")
        )
        object.__setattr__(
            self,
            "timestamp",
            _enum(TimestampConvention, self.timestamp, "bars.timestamp"),
        )
        object.__setattr__(
            self,
            "availability_basis",
            _enum(AvailabilityBasis, self.availability_basis, "bars.availability_basis"),
        )


@dataclass(frozen=True)
class DatasetHashes:
    raw_sha256: str
    normalized_csv_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "raw_sha256", _sha256(self.raw_sha256, "hashes.raw_sha256")
        )
        object.__setattr__(
            self,
            "normalized_csv_sha256",
            _sha256(self.normalized_csv_sha256, "hashes.normalized_csv_sha256"),
        )


@dataclass(frozen=True)
class DatasetRights:
    basis: str
    api_data_agreement_version: str
    retention_basis: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "basis", _required_text(self.basis, "rights.basis"))
        object.__setattr__(
            self,
            "api_data_agreement_version",
            _required_text(
                self.api_data_agreement_version,
                "rights.api_data_agreement_version",
            ),
        )
        object.__setattr__(
            self,
            "retention_basis",
            _required_text(self.retention_basis, "rights.retention_basis"),
        )


@dataclass(frozen=True)
class SessionCalendar:
    calendar_id: str
    version: str
    artifact_uri: Optional[str] = None
    artifact_sha256: Optional[str] = None
    content_fingerprint: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "calendar_id", _required_text(self.calendar_id, "session_calendar.id")
        )
        object.__setattr__(
            self, "version", _required_text(self.version, "session_calendar.version")
        )
        artifact_fields = (self.artifact_uri, self.artifact_sha256, self.content_fingerprint)
        if any(value is not None for value in artifact_fields):
            if any(value is None for value in artifact_fields):
                raise DatasetManifestError(
                    "session_calendar artifact_uri, artifact_sha256, and "
                    "content_fingerprint must be provided together"
                )
            object.__setattr__(self, "artifact_uri", _calendar_artifact_uri(self.artifact_uri))
            for field_name in ("artifact_sha256", "content_fingerprint"):
                object.__setattr__(
                    self,
                    field_name,
                    _calendar_sha256(getattr(self, field_name), "session_calendar." + field_name),
                )


@dataclass(frozen=True)
class KnownGap:
    start: datetime
    end: datetime
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start, "known_gaps.start"))
        object.__setattr__(self, "end", _utc(self.end, "known_gaps.end"))
        object.__setattr__(self, "reason", _required_text(self.reason, "known_gaps.reason"))
        if self.start >= self.end:
            raise DatasetManifestError("known gap start must be earlier than end")
        if not _is_m15_boundary(self.start) or not _is_m15_boundary(self.end):
            raise DatasetManifestError("known gaps must align to UTC M15 boundaries")
        if (self.end - self.start).total_seconds() % (15 * 60) != 0:
            raise DatasetManifestError("known gaps must span whole M15 intervals")


@dataclass(frozen=True)
class DatasetCoverage:
    start: datetime
    end: datetime
    row_count: int
    known_gaps: Tuple[KnownGap, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start, "coverage.start"))
        object.__setattr__(self, "end", _utc(self.end, "coverage.end"))
        count = _integer(self.row_count, "coverage.row_count")
        if count < 1:
            raise DatasetManifestError("coverage.row_count must be at least 1")
        object.__setattr__(self, "row_count", count)
        gaps = tuple(self.known_gaps)
        if any(not isinstance(gap, KnownGap) for gap in gaps):
            raise DatasetManifestError("coverage.known_gaps must contain KnownGap values")
        object.__setattr__(self, "known_gaps", gaps)
        if self.start >= self.end:
            raise DatasetManifestError("coverage.start must be earlier than coverage.end")
        if not _is_m15_boundary(self.start) or not _is_m15_boundary(self.end):
            raise DatasetManifestError("coverage must align to UTC M15 boundaries")
        previous_end = None
        for gap in gaps:
            if gap.start <= self.start or gap.end >= self.end:
                raise DatasetManifestError("known gaps must be strictly inside coverage")
            if previous_end is not None and gap.start <= previous_end:
                raise DatasetManifestError("known gaps must be ordered, separated, and non-overlapping")
            previous_end = gap.end
        covered_seconds = (self.end - self.start).total_seconds() - sum(
            (gap.end - gap.start).total_seconds() for gap in gaps
        )
        if covered_seconds != count * 15 * 60:
            raise DatasetManifestError(
                "coverage interval, known gaps, and row_count are inconsistent"
            )


@dataclass(frozen=True)
class DatasetManifest:
    schema: str
    version: int
    provider: DatasetProvider
    instrument: DatasetInstrument
    source: DatasetSource
    bars: BarConvention
    hashes: DatasetHashes
    rights: DatasetRights
    session_calendar: SessionCalendar
    coverage: DatasetCoverage

    def __post_init__(self) -> None:
        schema = _required_text(self.schema, "schema")
        if schema != SCHEMA_NAME:
            raise DatasetManifestError("schema is not supported")
        object.__setattr__(self, "schema", schema)
        version = _integer(self.version, "version")
        if version not in SUPPORTED_VERSIONS:
            raise DatasetManifestError("version is not supported")
        object.__setattr__(self, "version", version)
        expected_types = (
            ("provider", DatasetProvider),
            ("instrument", DatasetInstrument),
            ("source", DatasetSource),
            ("bars", BarConvention),
            ("hashes", DatasetHashes),
            ("rights", DatasetRights),
            ("session_calendar", SessionCalendar),
            ("coverage", DatasetCoverage),
        )
        for field_name, expected_type in expected_types:
            if not isinstance(getattr(self, field_name), expected_type):
                raise DatasetManifestError(
                    "{} must be a {}".format(field_name, expected_type.__name__)
                )
        artifact_fields = (
            self.session_calendar.artifact_uri,
            self.session_calendar.artifact_sha256,
            self.session_calendar.content_fingerprint,
        )
        if version == 1 and any(value is not None for value in artifact_fields):
            raise DatasetManifestError("manifest v1 must not contain calendar artifact fields")
        if version == 2 and any(value is None for value in artifact_fields):
            raise DatasetManifestError("manifest v2 requires all calendar artifact fields")
        if self.source.retrieved_at < self.coverage.end:
            raise DatasetManifestError("source.retrieved_at cannot precede coverage.end")

    def as_dict(self) -> Dict[str, Any]:
        calendar = {
            "id": self.session_calendar.calendar_id,
            "version": self.session_calendar.version,
        }
        if self.version == 2:
            calendar.update(
                {
                    "artifact_uri": self.session_calendar.artifact_uri,
                    "artifact_sha256": self.session_calendar.artifact_sha256,
                    "content_fingerprint": self.session_calendar.content_fingerprint,
                }
            )
        return {
            "schema": self.schema,
            "version": self.version,
            "provider": {
                "name": self.provider.name,
                "legal_entity": self.provider.legal_entity,
            },
            "instrument": {
                "symbol": self.instrument.symbol,
                "product_form": self.instrument.product_form,
            },
            "source": {
                "route": self.source.route,
                "account_environment": self.source.account_environment,
                "acquisition_basis": self.source.acquisition_basis.value,
                "request_parameters": dict(self.source.request_parameters),
                "time_normalization_rule": self.source.time_normalization_rule,
                "retrieved_at": _format_utc(self.source.retrieved_at),
            },
            "bars": {
                "timeframe": self.bars.timeframe.value,
                "duration_seconds": self.bars.duration_seconds,
                "timezone": self.bars.timezone,
                "interval": self.bars.interval.value,
                "timestamp": self.bars.timestamp.value,
                "availability_basis": self.bars.availability_basis.value,
            },
            "hashes": {
                "raw_sha256": self.hashes.raw_sha256,
                "normalized_csv_sha256": self.hashes.normalized_csv_sha256,
            },
            "rights": {
                "basis": self.rights.basis,
                "api_data_agreement_version": self.rights.api_data_agreement_version,
                "retention_basis": self.rights.retention_basis,
            },
            "session_calendar": calendar,
            "coverage": {
                "start": _format_utc(self.coverage.start),
                "end": _format_utc(self.coverage.end),
                "row_count": self.coverage.row_count,
                "known_gaps": [
                    {
                        "start": _format_utc(gap.start),
                        "end": _format_utc(gap.end),
                        "reason": gap.reason,
                    }
                    for gap in self.coverage.known_gaps
                ],
            },
        }

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @property
    def identity(self) -> str:
        return "{}:v{}:{}".format(self.schema, self.version, self.fingerprint)


@dataclass(frozen=True)
class DatasetBindingValidation:
    manifest_identity: str
    manifest_fingerprint: str
    row_count: int
    coverage_start: datetime
    coverage_end: datetime
    known_gap_count: int


def dataset_manifest_from_dict(payload: Mapping[str, Any]) -> DatasetManifest:
    """Parse a strict manifest mapping, rejecting missing and unknown fields."""

    root = _mapping(payload, "manifest")
    _exact_fields(
        root,
        {
            "schema",
            "version",
            "provider",
            "instrument",
            "source",
            "bars",
            "hashes",
            "rights",
            "session_calendar",
            "coverage",
        },
        "manifest",
    )
    version = _integer(root["version"], "version")
    if version not in SUPPORTED_VERSIONS:
        raise DatasetManifestError("version is not supported")
    provider = _mapping(root["provider"], "provider")
    _exact_fields(provider, {"name", "legal_entity"}, "provider")
    instrument = _mapping(root["instrument"], "instrument")
    _exact_fields(instrument, {"symbol", "product_form"}, "instrument")
    source = _mapping(root["source"], "source")
    _exact_fields(
        source,
        {
            "route",
            "account_environment",
            "acquisition_basis",
            "request_parameters",
            "time_normalization_rule",
            "retrieved_at",
        },
        "source",
    )
    bars = _mapping(root["bars"], "bars")
    _exact_fields(
        bars,
        {
            "timeframe",
            "duration_seconds",
            "timezone",
            "interval",
            "timestamp",
            "availability_basis",
        },
        "bars",
    )
    hashes = _mapping(root["hashes"], "hashes")
    _exact_fields(hashes, {"raw_sha256", "normalized_csv_sha256"}, "hashes")
    rights = _mapping(root["rights"], "rights")
    _exact_fields(
        rights,
        {"basis", "api_data_agreement_version", "retention_basis"},
        "rights",
    )
    calendar = _mapping(root["session_calendar"], "session_calendar")
    calendar_fields = {"id", "version"}
    if version == 2:
        calendar_fields.update({"artifact_uri", "artifact_sha256", "content_fingerprint"})
    _exact_fields(calendar, calendar_fields, "session_calendar")
    coverage = _mapping(root["coverage"], "coverage")
    _exact_fields(coverage, {"start", "end", "row_count", "known_gaps"}, "coverage")
    request_parameters = _mapping(source["request_parameters"], "source.request_parameters")
    parameter_items: List[Tuple[str, str]] = []
    for name, value in request_parameters.items():
        parameter_items.append(
            (
                _required_text(name, "source.request_parameters name"),
                _required_text(value, "source.request_parameters.{}".format(name)),
            )
        )
    raw_gaps = coverage["known_gaps"]
    if not isinstance(raw_gaps, list):
        raise DatasetManifestError("coverage.known_gaps must be an array")
    gaps: List[KnownGap] = []
    for index, raw_gap in enumerate(raw_gaps):
        context = "coverage.known_gaps[{}]".format(index)
        gap = _mapping(raw_gap, context)
        _exact_fields(gap, {"start", "end", "reason"}, context)
        gaps.append(
            KnownGap(
                start=_parse_utc(gap["start"], context + ".start"),
                end=_parse_utc(gap["end"], context + ".end"),
                reason=gap["reason"],
            )
        )
    return DatasetManifest(
        schema=root["schema"],
        version=root["version"],
        provider=DatasetProvider(name=provider["name"], legal_entity=provider["legal_entity"]),
        instrument=DatasetInstrument(
            symbol=instrument["symbol"], product_form=instrument["product_form"]
        ),
        source=DatasetSource(
            route=source["route"],
            account_environment=source["account_environment"],
            acquisition_basis=source["acquisition_basis"],
            request_parameters=tuple(parameter_items),
            time_normalization_rule=source["time_normalization_rule"],
            retrieved_at=_parse_utc(source["retrieved_at"], "source.retrieved_at"),
        ),
        bars=BarConvention(
            timeframe=bars["timeframe"],
            duration_seconds=bars["duration_seconds"],
            timezone=bars["timezone"],
            interval=bars["interval"],
            timestamp=bars["timestamp"],
            availability_basis=bars["availability_basis"],
        ),
        hashes=DatasetHashes(
            raw_sha256=hashes["raw_sha256"],
            normalized_csv_sha256=hashes["normalized_csv_sha256"],
        ),
        rights=DatasetRights(
            basis=rights["basis"],
            api_data_agreement_version=rights["api_data_agreement_version"],
            retention_basis=rights["retention_basis"],
        ),
        session_calendar=SessionCalendar(
            calendar_id=calendar["id"],
            version=calendar["version"],
            artifact_uri=calendar.get("artifact_uri"),
            artifact_sha256=calendar.get("artifact_sha256"),
            content_fingerprint=calendar.get("content_fingerprint"),
        ),
        coverage=DatasetCoverage(
            start=_parse_utc(coverage["start"], "coverage.start"),
            end=_parse_utc(coverage["end"], "coverage.end"),
            row_count=coverage["row_count"],
            known_gaps=tuple(gaps),
        ),
    )


def load_dataset_manifest(path: Union[str, Path]) -> DatasetManifest:
    """Load a strict JSON manifest from disk."""

    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            payload = json.load(
                handle,
                object_pairs_hook=_reject_duplicate_fields,
                parse_constant=_reject_json_constant,
            )
    except DatasetManifestError:
        raise
    except json.JSONDecodeError as exc:
        raise DatasetManifestError("manifest is not valid JSON") from exc
    except OSError as exc:
        raise DatasetManifestError("could not read {}: {}".format(source, exc)) from exc
    return dataset_manifest_from_dict(payload)


def validate_dataset_binding(
    manifest: DatasetManifest,
    bars: Sequence[QuoteBar],
    *,
    raw_sha256: str,
    normalized_csv_sha256: str,
) -> DatasetBindingValidation:
    """Bind a manifest to exact bars and caller-computed raw/normalized hashes.

    No value is derived to repair or complete the manifest.  Observed gaps must match
    the declared intervals exactly, including scheduled session closures.
    """

    if not isinstance(manifest, DatasetManifest):
        raise DatasetManifestError("manifest must be a DatasetManifest")
    actual_raw = _sha256(raw_sha256, "raw_sha256")
    actual_normalized = _sha256(normalized_csv_sha256, "normalized_csv_sha256")
    if actual_raw != manifest.hashes.raw_sha256:
        raise DatasetManifestError("raw_sha256 does not match the manifest")
    if actual_normalized != manifest.hashes.normalized_csv_sha256:
        raise DatasetManifestError("normalized_csv_sha256 does not match the manifest")
    actual_bars = tuple(bars)
    if not actual_bars:
        raise DatasetManifestError("bars must contain at least one QuoteBar")
    if any(not isinstance(bar, QuoteBar) for bar in actual_bars):
        raise DatasetManifestError("bars must contain only QuoteBar values")
    if len(actual_bars) != manifest.coverage.row_count:
        raise DatasetManifestError("bar row count does not match coverage.row_count")
    if actual_bars[0].start_time != manifest.coverage.start:
        raise DatasetManifestError("first bar start does not match coverage.start")
    if actual_bars[-1].timestamp != manifest.coverage.end:
        raise DatasetManifestError("last bar end does not match coverage.end")

    expected_seconds = manifest.bars.duration_seconds
    observed_gaps: List[Tuple[datetime, datetime]] = []
    previous = None
    for index, bar in enumerate(actual_bars):
        if bar.availability_basis != manifest.bars.availability_basis:
            raise DatasetManifestError(
                "bar {} availability basis does not match the manifest".format(index)
            )
        if bar.availability_basis in (
            AvailabilityBasis.SYNTHETIC,
            AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
        ) and bar.available_at != bar.timestamp:
            raise DatasetManifestError(
                "bar {} available_at must equal its end for {}".format(
                    index, bar.availability_basis.value
                )
            )
        if (bar.timestamp - bar.start_time).total_seconds() != expected_seconds:
            raise DatasetManifestError("bar {} duration does not match M15".format(index))
        if not _is_m15_boundary(bar.start_time) or not _is_m15_boundary(bar.timestamp):
            raise DatasetManifestError("bar {} is not aligned to a UTC M15 boundary".format(index))
        if previous is not None:
            if bar.start_time < previous.timestamp:
                raise DatasetManifestError("bar {} overlaps the previous bar".format(index))
            if bar.start_time > previous.timestamp:
                observed_gaps.append((previous.timestamp, bar.start_time))
        previous = bar
    declared_gaps = tuple((gap.start, gap.end) for gap in manifest.coverage.known_gaps)
    if tuple(observed_gaps) != declared_gaps:
        raise DatasetManifestError("observed bar gaps do not exactly match coverage.known_gaps")
    if manifest.source.retrieved_at < max(bar.available_at for bar in actual_bars):
        raise DatasetManifestError("source.retrieved_at precedes a bar's available_at")
    return DatasetBindingValidation(
        manifest_identity=manifest.identity,
        manifest_fingerprint=manifest.fingerprint,
        row_count=len(actual_bars),
        coverage_start=actual_bars[0].start_time,
        coverage_end=actual_bars[-1].timestamp,
        known_gap_count=len(observed_gaps),
    )


def validate_calendar_binding(
    manifest: DatasetManifest,
    calendar: "SessionCalendarArtifact",
    *,
    artifact_sha256: str,
) -> Dict[str, str]:
    """Bind a v2 manifest to an explicit calendar and its caller-computed file hash."""

    from .session_calendar import SessionCalendarArtifact

    if not isinstance(manifest, DatasetManifest):
        raise DatasetManifestError("manifest must be a DatasetManifest")
    if manifest.version != 2:
        raise DatasetManifestError("calendar binding requires manifest v2")
    if not isinstance(calendar, SessionCalendarArtifact):
        raise DatasetManifestError("calendar must be a SessionCalendarArtifact")
    actual_sha256 = _calendar_sha256(artifact_sha256, "artifact_sha256")
    reference = manifest.session_calendar
    if actual_sha256 != reference.artifact_sha256:
        raise DatasetManifestError("calendar artifact_sha256 does not match the manifest")
    if calendar.fingerprint != reference.content_fingerprint:
        raise DatasetManifestError("calendar content_fingerprint does not match the manifest")
    if calendar.calendar_id != reference.calendar_id:
        raise DatasetManifestError("calendar id does not match session_calendar.id")
    if calendar.revision != reference.version:
        raise DatasetManifestError("calendar revision does not match session_calendar.version")
    if calendar.provider_name != manifest.provider.name:
        raise DatasetManifestError("calendar provider_name does not match provider.name")
    if calendar.provider_legal_entity != manifest.provider.legal_entity:
        raise DatasetManifestError("calendar provider_legal_entity does not match provider.legal_entity")
    if calendar.instrument != manifest.instrument.symbol:
        raise DatasetManifestError("calendar instrument does not match instrument.symbol")
    if calendar.product_form != manifest.instrument.product_form:
        raise DatasetManifestError("calendar product_form does not match instrument.product_form")
    if (
        calendar.coverage_start > manifest.coverage.start
        or calendar.coverage_end < manifest.coverage.end
    ):
        raise DatasetManifestError("calendar coverage must contain dataset coverage")
    if calendar.retrieved_at > manifest.source.retrieved_at:
        raise DatasetManifestError("calendar retrieved_at must not follow source.retrieved_at")
    return {
        "calendar_id": calendar.calendar_id,
        "revision": calendar.revision,
        "artifact_sha256": actual_sha256,
        "content_fingerprint": calendar.fingerprint,
    }


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DatasetManifestError("{} must be an object".format(context))
    if any(not isinstance(key, str) for key in value):
        raise DatasetManifestError("{} field names must be strings".format(context))
    return value


def _exact_fields(payload: Mapping[str, Any], expected: set, context: str) -> None:
    actual = set(payload)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise DatasetManifestError(
            "{} is missing fields: {}".format(context, ", ".join(missing))
        )
    if unknown:
        raise DatasetManifestError(
            "{} has unknown fields: {}".format(context, ", ".join(unknown))
        )


def _parse_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise DatasetManifestError("{} must be an ISO 8601 string".format(field_name))
    clean = value.strip()
    if not clean:
        raise DatasetManifestError("{} must not be empty".format(field_name))
    candidate = clean[:-1] + "+00:00" if clean.endswith("Z") else clean
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise DatasetManifestError("{} is not a valid ISO 8601 timestamp".format(field_name)) from exc
    return _utc(parsed, field_name)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_m15_boundary(value: datetime) -> bool:
    return value.second == 0 and value.microsecond == 0 and value.minute % 15 == 0


def _reject_duplicate_fields(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetManifestError("duplicate JSON field: {}".format(key))
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise DatasetManifestError("non-standard JSON constant: {}".format(value))


__all__ = [
    "AcquisitionBasis",
    "BarConvention",
    "BarTimeframe",
    "DatasetBindingValidation",
    "DatasetCoverage",
    "DatasetHashes",
    "DatasetInstrument",
    "DatasetManifest",
    "DatasetManifestError",
    "DatasetProvider",
    "DatasetRights",
    "DatasetSource",
    "IntervalConvention",
    "KnownGap",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "SUPPORTED_VERSIONS",
    "SUPPORTED_INSTRUMENT",
    "SessionCalendar",
    "TimestampConvention",
    "dataset_manifest_from_dict",
    "load_dataset_manifest",
    "validate_calendar_binding",
    "validate_dataset_binding",
]
