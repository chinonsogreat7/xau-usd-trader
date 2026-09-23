"""Finite, explicit UTC closure evidence for feature-only market-data replay.

A calendar records supplied provenance and closures; it never infers weekends,
holidays, provider practices, or missing market data. Intervals are half-open.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Tuple, Union
from urllib.parse import urlsplit


SCHEMA_NAME = "xau_trader.session_calendar"
SCHEMA_VERSION = 1
SUPPORTED_INSTRUMENT = "XAU_USD"
MAX_CALENDAR_BYTES = 1024 * 1024


class SessionCalendarError(ValueError):
    """Raised for invalid calendar evidence or an unaccounted-for interval."""


_PLACEHOLDERS = {
    "example", "na", "n_a", "none", "not_applicable", "not_available",
    "not_known", "not_sure", "null", "pending", "placeholder", "replace_me",
    "tbd", "t_b_d", "to_be_determined", "todo", "unknown", "unspecified",
}
_UTC_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]00:00)"
)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise SessionCalendarError("{} must be a string".format(field_name))
    if any(
        ord(character) < 32
        or 127 <= ord(character) <= 159
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise SessionCalendarError("{} contains invalid text characters".format(field_name))
    clean = value.strip()
    if not clean:
        raise SessionCalendarError("{} must not be empty".format(field_name))
    token = re.sub(r"[\W_]+", "_", clean.casefold()).strip("_")
    if (
        not token
        or token in _PLACEHOLDERS
        or token.startswith("replace_me_")
        or token.startswith("your_")
        or (clean.startswith("<") and clean.endswith(">"))
    ):
        raise SessionCalendarError("{} must not be a placeholder".format(field_name))
    return clean


def _source_reference(value: object) -> str:
    clean = _required_text(value, "source_reference")
    if "?" in clean or "#" in clean:
        raise SessionCalendarError("source_reference must omit query strings and fragments")
    try:
        parsed = urlsplit(clean)
        contains_credentials = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise SessionCalendarError("source_reference is malformed") from exc
    if contains_credentials or re.search(
        r"\bauthorization\s*[:=]|\b(?:bearer|basic)\s+", clean, re.IGNORECASE
    ):
        raise SessionCalendarError("source_reference must not contain credentials or authorization")
    return clean


def _utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise SessionCalendarError("{} must be a datetime".format(field_name))
    if value.tzinfo is None or value.utcoffset() is None:
        raise SessionCalendarError("{} must include an explicit UTC offset".format(field_name))
    if value.utcoffset().total_seconds() != 0:
        raise SessionCalendarError("{} must use UTC".format(field_name))
    return value.astimezone(timezone.utc)


def _parse_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or _UTC_TIMESTAMP.fullmatch(value) is None:
        raise SessionCalendarError("{} must be an ISO 8601 UTC timestamp".format(field_name))
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise SessionCalendarError("{} is not a valid UTC timestamp".format(field_name)) from exc
    return _utc(parsed, field_name)


def _format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ScheduledClosure:
    start: datetime
    end: datetime
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start, "closure.start"))
        object.__setattr__(self, "end", _utc(self.end, "closure.end"))
        object.__setattr__(self, "reason", _required_text(self.reason, "closure.reason"))
        if self.end <= self.start:
            raise SessionCalendarError("closure.end must be after closure.start")


@dataclass(frozen=True)
class SessionCalendarArtifact:
    calendar_id: str
    revision: str
    provider_name: str
    provider_legal_entity: str
    instrument: str
    product_form: str
    coverage_start: datetime
    coverage_end: datetime
    source_reference: str
    retrieved_at: datetime
    closures: Tuple[ScheduledClosure, ...]
    schema: str = SCHEMA_NAME
    version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_NAME:
            raise SessionCalendarError("schema is not supported")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise SessionCalendarError("version must be an integer")
        if self.version != SCHEMA_VERSION:
            raise SessionCalendarError("version is not supported")
        for name in (
            "calendar_id", "revision", "provider_name", "provider_legal_entity",
            "instrument", "product_form",
        ):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if self.instrument != SUPPORTED_INSTRUMENT:
            raise SessionCalendarError("instrument must be XAU_USD")
        object.__setattr__(self, "source_reference", _source_reference(self.source_reference))
        for name in ("coverage_start", "coverage_end", "retrieved_at"):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.coverage_end <= self.coverage_start:
            raise SessionCalendarError("coverage_end must be after coverage_start")
        # A published schedule can cover future dates; retrieval is provenance,
        # not a requirement that the scheduled period has already elapsed.
        if not isinstance(self.closures, (tuple, list)):
            raise SessionCalendarError("closures must be a sequence of ScheduledClosure values")
        closures = tuple(self.closures)
        previous = None
        for closure in closures:
            if not isinstance(closure, ScheduledClosure):
                raise SessionCalendarError("closures must contain only ScheduledClosure values")
            if closure.start < self.coverage_start or closure.end > self.coverage_end:
                raise SessionCalendarError("closures must lie wholly within calendar coverage")
            if previous is not None and closure.start <= previous.end:
                raise SessionCalendarError("closures must be ordered, nonoverlapping, and nonadjacent")
            previous = closure
        object.__setattr__(self, "closures", closures)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "id": self.calendar_id,
            "revision": self.revision,
            "provider_name": self.provider_name,
            "provider_legal_entity": self.provider_legal_entity,
            "instrument": self.instrument,
            "product_form": self.product_form,
            "coverage_start": _format_utc(self.coverage_start),
            "coverage_end": _format_utc(self.coverage_end),
            "source_reference": self.source_reference,
            "retrieved_at": _format_utc(self.retrieved_at),
            "closures": [
                {"start": _format_utc(item.start), "end": _format_utc(item.end), "reason": item.reason}
                for item in self.closures
            ],
        }

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    def validate_bar(self, start: datetime, end: datetime) -> None:
        """Require one positive interval inside coverage and outside closures."""
        start = _utc(start, "bar.start")
        end = _utc(end, "bar.end")
        if end <= start:
            raise SessionCalendarError("bar.end must be after bar.start")
        if start < self.coverage_start or end > self.coverage_end:
            raise SessionCalendarError("bar must lie wholly within calendar coverage")
        if any(start < closure.end and end > closure.start for closure in self.closures):
            raise SessionCalendarError("bar overlaps a scheduled closure")

    def validate_transition(self, previous_end: datetime, next_start: datetime) -> None:
        """Allow continuous data or a gap exactly equal to one declared closure."""
        previous_end = _utc(previous_end, "previous_end")
        next_start = _utc(next_start, "next_start")
        if not (
            self.coverage_start <= previous_end <= self.coverage_end
            and self.coverage_start <= next_start <= self.coverage_end
        ):
            raise SessionCalendarError("transition endpoints must lie within calendar coverage")
        if next_start < previous_end:
            raise SessionCalendarError("transition must not go backwards or overlap")
        if next_start == previous_end:
            return
        if not any(
            closure.start == previous_end and closure.end == next_start
            for closure in self.closures
        ):
            raise SessionCalendarError("gap must exactly match one declared scheduled closure")

    def require_hour_aligned_closures(self) -> None:
        """Enforce the version-1 feature kernel's full UTC hour restriction."""
        for closure in self.closures:
            for endpoint in (closure.start, closure.end):
                if endpoint.minute != 0 or endpoint.second != 0 or endpoint.microsecond != 0:
                    raise SessionCalendarError("feature replay requires whole UTC hour closure boundaries")


def _mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SessionCalendarError("{} must be an object".format(context))
    if any(not isinstance(key, str) for key in value):
        raise SessionCalendarError("{} field names must be strings".format(context))
    return value


def _exact_fields(payload: Mapping[str, Any], expected: set, context: str) -> None:
    actual = set(payload)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise SessionCalendarError("{} is missing fields: {}".format(context, ", ".join(missing)))
    if unknown:
        raise SessionCalendarError("{} has unknown fields: {}".format(context, ", ".join(unknown)))


def session_calendar_from_dict(payload: Mapping[str, Any]) -> SessionCalendarArtifact:
    """Parse the closed calendar schema; no implicit sessions or missing values."""
    root = _mapping(payload, "calendar")
    _exact_fields(root, {
        "schema", "version", "id", "revision", "provider_name", "provider_legal_entity",
        "instrument", "product_form", "coverage_start", "coverage_end", "source_reference",
        "retrieved_at", "closures",
    }, "calendar")
    if not isinstance(root["closures"], list):
        raise SessionCalendarError("closures must be an array")
    closures = []
    for index, item in enumerate(root["closures"]):
        context = "closures[{}]".format(index)
        closure = _mapping(item, context)
        _exact_fields(closure, {"start", "end", "reason"}, context)
        closures.append(ScheduledClosure(
            start=_parse_utc(closure["start"], context + ".start"),
            end=_parse_utc(closure["end"], context + ".end"),
            reason=closure["reason"],
        ))
    return SessionCalendarArtifact(
        schema=root["schema"], version=root["version"], calendar_id=root["id"],
        revision=root["revision"], provider_name=root["provider_name"],
        provider_legal_entity=root["provider_legal_entity"], instrument=root["instrument"],
        product_form=root["product_form"],
        coverage_start=_parse_utc(root["coverage_start"], "coverage_start"),
        coverage_end=_parse_utc(root["coverage_end"], "coverage_end"),
        source_reference=root["source_reference"],
        retrieved_at=_parse_utc(root["retrieved_at"], "retrieved_at"), closures=tuple(closures),
    )


def _reject_duplicate_fields(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SessionCalendarError("duplicate JSON field: {}".format(key))
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SessionCalendarError("non-standard JSON constant: {}".format(value))


def parse_session_calendar_bytes(raw: bytes) -> SessionCalendarArtifact:
    """Parse at most 1 MiB of strict UTF-8 JSON from one exact byte snapshot.

    Callers can hash these same bytes to bind provenance without reopening a
    mutable path. Duplicate fields and non-standard JSON constants are rejected.
    """
    if not isinstance(raw, bytes):
        raise SessionCalendarError("calendar input must be bytes")
    if len(raw) > MAX_CALENDAR_BYTES:
        raise SessionCalendarError("calendar exceeds MAX_CALENDAR_BYTES (1 MiB)")
    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_fields,
            parse_constant=_reject_json_constant,
        )
    except SessionCalendarError:
        raise
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise SessionCalendarError("calendar is not valid UTF-8 JSON") from exc
    return session_calendar_from_dict(payload)


def load_session_calendar(path: Union[str, Path]) -> SessionCalendarArtifact:
    """Read one bounded byte snapshot and parse strict, duplicate-safe UTF-8 JSON."""
    source = Path(path)
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_CALENDAR_BYTES + 1)
    except OSError as exc:
        raise SessionCalendarError("could not read {}: {}".format(source, exc)) from exc
    return parse_session_calendar_bytes(raw)


__all__ = [
    "SCHEMA_NAME", "SCHEMA_VERSION", "SUPPORTED_INSTRUMENT", "MAX_CALENDAR_BYTES", "ScheduledClosure",
    "SessionCalendarArtifact", "SessionCalendarError", "load_session_calendar",
    "parse_session_calendar_bytes", "session_calendar_from_dict",
]
