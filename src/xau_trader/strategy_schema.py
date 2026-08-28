"""Strict, dependency-free validation for the closed StrategySpec v1 JSON contract.

This module accepts JSON-shaped data only. It never evaluates strings, imports generated
modules, renders templates, or turns natural language into executable code. Validation either
returns one immutable canonical artifact or raises a stable, pointer-addressed error.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse


SPEC_VERSION = "1.0"
MAX_DOCUMENT_BYTES = 1_000_000
MAX_JSON_DEPTH = 64
MAX_AST_DEPTH = 32
MAX_AST_NODES = 512
MAX_PARAMETERS = 128
MAX_FEATURES = 128
MAX_SOURCES = 64
MAX_EVIDENCE_ITEMS = 512
MAX_TEXT_LENGTH = 10_000

_STRATEGY_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_LOCAL_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_UTC_TIMESTAMP = re.compile(
    r"^(?P<base>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?P<fraction>\.[0-9]{1,6})?Z$"
)
_TIME = re.compile(r"^(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

_TOP_LEVEL_FIELDS = {
    "spec_version",
    "strategy_id",
    "revision",
    "name",
    "description",
    "created_at",
    "frozen_at",
    "provenance",
    "market",
    "parameters",
    "features",
    "schedule",
    "entry",
    "exit",
    "sizing",
    "execution",
    "risk",
    "assumptions",
    "unresolved_items",
    "tags",
}


class StrategySpecValidationError(ValueError):
    """One deterministic validation failure with a stable code and JSON pointer."""

    def __init__(self, code: str, pointer: str, message: str) -> None:
        self.code = code
        self.pointer = pointer or "/"
        self.message = message
        super().__init__("{} at {}: {}".format(code, self.pointer, message))


def _fail(code: str, pointer: str, message: str) -> None:
    raise StrategySpecValidationError(code, pointer, message)


def _pointer(parent: str, token: str) -> str:
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return (parent.rstrip("/") + "/" + escaped) if parent else "/" + escaped


def _reject_constant(value: str) -> None:
    _fail("invalid_json_number", "/", "non-finite JSON constants are forbidden")


def _reject_duplicate_pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_key", "/", "duplicate JSON object key {!r}".format(key))
        result[key] = value
    return result


def parse_strategy_spec_json(raw: str) -> Mapping[str, Any]:
    """Parse JSON without silently overwriting duplicate keys or accepting NaN/Infinity."""

    if not isinstance(raw, str):
        _fail("invalid_json", "/", "strategy JSON must be text")
    if len(raw.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        _fail("document_too_large", "/", "strategy JSON exceeds the one-megabyte limit")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except StrategySpecValidationError:
        raise
    except RecursionError:
        _fail("document_too_deep", "/", "JSON nesting exceeds the configured limit")
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail("invalid_json", "/", "strategy JSON is malformed: {}".format(exc))
    if not isinstance(value, dict):
        _fail("wrong_type", "/", "StrategySpec must be a JSON object")
    _assert_json_tree(value, "")
    return value


def _assert_json_tree(
    value: Any,
    pointer: str,
    depth: int = 0,
    active: Optional[Set[int]] = None,
) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail("document_too_deep", pointer, "JSON nesting exceeds the configured limit")
    if active is None:
        active = set()
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
            _fail("string_too_long", pointer, "string exceeds the configured limit")
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        _fail("wrong_type", pointer, "JSON numbers are forbidden; use integer or decimal string")
    if isinstance(value, dict):
        object_id = id(value)
        if object_id in active:
            _fail("cyclic_input", pointer, "cyclic mappings are forbidden")
        active.add(object_id)
        for key, child in value.items():
            if not isinstance(key, str):
                _fail("wrong_type", pointer, "JSON object keys must be strings")
            _assert_json_tree(child, _pointer(pointer, key), depth + 1, active)
        active.remove(object_id)
        return
    if isinstance(value, list):
        object_id = id(value)
        if object_id in active:
            _fail("cyclic_input", pointer, "cyclic arrays are forbidden")
        active.add(object_id)
        for index, child in enumerate(value):
            _assert_json_tree(child, _pointer(pointer, str(index)), depth + 1, active)
        active.remove(object_id)
        return
    _fail("wrong_type", pointer, "custom Python objects are forbidden; use JSON-shaped data")


def _object(value: Any, pointer: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail("wrong_type", pointer, "expected an object")
    return value


def _array(value: Any, pointer: str) -> Sequence[Any]:
    if not isinstance(value, list):
        _fail("wrong_type", pointer, "expected an array")
    return value


def _exact_fields(value: Mapping[str, Any], expected: Set[str], pointer: str) -> None:
    keys = set(value)
    unknown = sorted(keys - expected)
    if unknown:
        _fail("unknown_field", _pointer(pointer, unknown[0]), "field is not allowed")
    missing = sorted(expected - keys)
    if missing:
        _fail("missing_field", _pointer(pointer, missing[0]), "required field is missing")


def _text(
    value: Any,
    pointer: str,
    *,
    minimum: int = 1,
    maximum: int = 500,
) -> str:
    if not isinstance(value, str):
        _fail("wrong_type", pointer, "expected a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if len(normalized) < minimum or len(normalized) > maximum:
        _fail(
            "invalid_length",
            pointer,
            "string length must be between {} and {}".format(minimum, maximum),
        )
    return normalized


def _nullable_text(value: Any, pointer: str, *, maximum: int = 500) -> Optional[str]:
    if value is None:
        return None
    return _text(value, pointer, maximum=maximum)


def _token(value: Any, pointer: str) -> str:
    token = _text(value, pointer, maximum=64)
    if _LOCAL_ID.fullmatch(token) is None:
        _fail("invalid_id", pointer, "expected a lowercase identifier")
    return token


def _enum(value: Any, allowed: Set[str], pointer: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        _fail("invalid_enum", pointer, "expected one of {}".format(sorted(allowed)))
    return value


def _integer(
    value: Any,
    pointer: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("wrong_type", pointer, "expected an integer")
    if minimum is not None and value < minimum:
        _fail("out_of_range", pointer, "integer is below {}".format(minimum))
    if maximum is not None and value > maximum:
        _fail("out_of_range", pointer, "integer exceeds {}".format(maximum))
    return value


def _boolean(value: Any, pointer: str) -> bool:
    if not isinstance(value, bool):
        _fail("wrong_type", pointer, "expected a boolean")
    return value


def _canonical_decimal(value: Any, pointer: str) -> Tuple[str, Decimal]:
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        _fail("invalid_decimal", pointer, "expected a plain decimal string")
    if sum(character.isdigit() for character in value) > 28:
        _fail("decimal_too_precise", pointer, "decimal exceeds the 28-digit precision profile")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail("invalid_decimal", pointer, "decimal string is invalid")
    if not number.is_finite():
        _fail("invalid_decimal", pointer, "decimal must be finite")
    if number == 0:
        return "0", Decimal(0)
    canonical = format(number, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical, number


def _aligned(value: Any, minimum: Any, step: Any, pointer: str) -> bool:
    if isinstance(value, int) and isinstance(minimum, int) and isinstance(step, int):
        return (value - minimum) % step == 0
    try:
        with localcontext(Context(prec=80)):
            return (value - minimum) % step == 0
    except InvalidOperation:
        _fail("invalid_bounds", pointer, "numeric precision prevents exact step validation")


def _positive_decimal(value: Any, pointer: str, *, allow_zero: bool = False) -> str:
    canonical, number = _canonical_decimal(value, pointer)
    if number < 0 or (number == 0 and not allow_zero):
        _fail("out_of_range", pointer, "decimal must be {}zero".format("at least " if allow_zero else "greater than "))
    return canonical


def _utc_timestamp(value: Any, pointer: str) -> Tuple[str, datetime]:
    if not isinstance(value, str):
        _fail("wrong_type", pointer, "expected an RFC 3339 UTC timestamp")
    match = _UTC_TIMESTAMP.fullmatch(value)
    if match is None:
        _fail("invalid_timestamp", pointer, "timestamp must use UTC Z notation")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError:
        _fail("invalid_timestamp", pointer, "timestamp is not a real date and time")
    if parsed.microsecond:
        canonical = parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
    else:
        canonical = parsed.isoformat(timespec="seconds").replace("+00:00", "Z")
    return canonical, parsed


def _clock_time(value: Any, pointer: str) -> Tuple[str, time]:
    if not isinstance(value, str):
        _fail("wrong_type", pointer, "expected HH:MM")
    match = _TIME.fullmatch(value)
    if match is None:
        _fail("invalid_time", pointer, "expected canonical HH:MM")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        _fail("invalid_time", pointer, "time is outside the UTC clock")
    return value, time(hour, minute)


def _calendar_date(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        _fail("invalid_date", pointer, "expected canonical YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail("invalid_date", pointer, "date is not real")
    return value


def _digest(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        _fail("invalid_digest", pointer, "expected 64 lowercase hexadecimal characters")
    return value


def _uri(value: Any, pointer: str) -> Optional[str]:
    if value is None:
        return None
    clean = _text(value, pointer, maximum=2_000)
    try:
        parsed = urlparse(clean)
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
        _ = parsed.port  # force validation of malformed or out-of-range ports
    except ValueError:
        _fail("invalid_uri", pointer, "URI is malformed")
    if parsed.scheme.lower() not in {"https", "http", "local", "urn"}:
        _fail("invalid_uri", pointer, "URI scheme is not allowlisted")
    if username or password:
        _fail("invalid_uri", pointer, "URI must contain no credentials")
    if parsed.scheme.lower() in {"https", "http"}:
        if not parsed.netloc or hostname in {None, "."} or any(
            character.isspace() for character in parsed.netloc
        ):
            _fail("invalid_uri", pointer, "HTTP(S) URI requires a valid host")
    return clean


def _unique_strings(
    value: Any,
    pointer: str,
    *,
    minimum: int = 0,
    maximum: int = 256,
    item_maximum: int = 256,
) -> List[str]:
    items = _array(value, pointer)
    if len(items) < minimum or len(items) > maximum:
        _fail("invalid_length", pointer, "array length is outside the allowed range")
    result: List[str] = []
    seen: Set[str] = set()
    for index, item in enumerate(items):
        item_pointer = _pointer(pointer, str(index))
        clean = _text(item, item_pointer, maximum=item_maximum)
        if clean in seen:
            _fail("duplicate_item", item_pointer, "array values must be unique")
        seen.add(clean)
        result.append(clean)
    return result


def _validate_parameters(value: Any) -> Tuple[Dict[str, Any], Dict[str, Tuple[str, Any]]]:
    parameters = _object(value, "/parameters")
    if len(parameters) > MAX_PARAMETERS:
        _fail("too_many_parameters", "/parameters", "parameter count exceeds the limit")
    normalized: Dict[str, Any] = {}
    types: Dict[str, Tuple[str, Any]] = {}
    for raw_id, raw_parameter in parameters.items():
        param_pointer = _pointer("/parameters", raw_id)
        parameter_id = _token(raw_id, param_pointer)
        if parameter_id in normalized:
            _fail(
                "duplicate_id",
                param_pointer,
                "parameter IDs collide after canonical normalization",
            )
        parameter = _object(raw_parameter, param_pointer)
        _exact_fields(
            parameter,
            {"type", "value", "research_bounds", "choices", "description"},
            param_pointer,
        )
        parameter_type = _enum(
            parameter["type"],
            {"integer", "decimal", "boolean", "enum"},
            _pointer(param_pointer, "type"),
        )
        raw_value = parameter["value"]
        canonical_value: Any = None
        typed_value: Any = None
        if raw_value is not None:
            if parameter_type == "integer":
                canonical_value = _integer(raw_value, _pointer(param_pointer, "value"))
                typed_value = canonical_value
            elif parameter_type == "decimal":
                canonical_value, typed_value = _canonical_decimal(
                    raw_value, _pointer(param_pointer, "value")
                )
            elif parameter_type == "boolean":
                canonical_value = _boolean(raw_value, _pointer(param_pointer, "value"))
                typed_value = canonical_value
            else:
                canonical_value = _text(
                    raw_value, _pointer(param_pointer, "value"), maximum=64
                )
                typed_value = canonical_value

        raw_choices = parameter["choices"]
        if parameter_type == "enum":
            choices = _unique_strings(
                raw_choices,
                _pointer(param_pointer, "choices"),
                minimum=1,
                maximum=64,
                item_maximum=64,
            )
            if canonical_value is not None and canonical_value not in choices:
                _fail(
                    "invalid_parameter",
                    _pointer(param_pointer, "value"),
                    "enum value is not one of its declared choices",
                )
        else:
            if raw_choices is not None:
                _fail(
                    "invalid_parameter",
                    _pointer(param_pointer, "choices"),
                    "choices are allowed only for enum parameters",
                )
            choices = None

        raw_bounds = parameter["research_bounds"]
        bounds: Optional[Dict[str, Any]] = None
        if raw_bounds is not None:
            if parameter_type not in {"integer", "decimal"}:
                _fail(
                    "invalid_parameter",
                    _pointer(param_pointer, "research_bounds"),
                    "research bounds require a numeric parameter",
                )
            bounds_object = _object(raw_bounds, _pointer(param_pointer, "research_bounds"))
            _exact_fields(
                bounds_object,
                {"min", "max", "step"},
                _pointer(param_pointer, "research_bounds"),
            )
            if parameter_type == "integer":
                minimum = _integer(bounds_object["min"], _pointer(param_pointer, "research_bounds/min"))
                maximum = _integer(bounds_object["max"], _pointer(param_pointer, "research_bounds/max"))
                step = _integer(
                    bounds_object["step"],
                    _pointer(param_pointer, "research_bounds/step"),
                    minimum=1,
                )
                comparable_value = typed_value
            else:
                canonical_min, minimum = _canonical_decimal(
                    bounds_object["min"], _pointer(param_pointer, "research_bounds/min")
                )
                canonical_max, maximum = _canonical_decimal(
                    bounds_object["max"], _pointer(param_pointer, "research_bounds/max")
                )
                canonical_step, step = _canonical_decimal(
                    bounds_object["step"], _pointer(param_pointer, "research_bounds/step")
                )
                comparable_value = typed_value
            if minimum > maximum:
                _fail("invalid_bounds", _pointer(param_pointer, "research_bounds"), "min exceeds max")
            if step <= 0:
                _fail("invalid_bounds", _pointer(param_pointer, "research_bounds/step"), "step must be positive")
            if comparable_value is not None:
                if comparable_value < minimum or comparable_value > maximum:
                    _fail("invalid_bounds", _pointer(param_pointer, "value"), "value lies outside research bounds")
                if not _aligned(
                    comparable_value,
                    minimum,
                    step,
                    _pointer(param_pointer, "value"),
                ):
                    _fail("invalid_bounds", _pointer(param_pointer, "value"), "value is not aligned to the declared step")
            if not _aligned(
                maximum,
                minimum,
                step,
                _pointer(param_pointer, "research_bounds"),
            ):
                _fail("invalid_bounds", _pointer(param_pointer, "research_bounds"), "range is not aligned to step")
            if parameter_type == "integer":
                bounds = {"min": minimum, "max": maximum, "step": step}
            else:
                bounds = {"min": canonical_min, "max": canonical_max, "step": canonical_step}

        normalized[parameter_id] = {
            "type": parameter_type,
            "value": canonical_value,
            "research_bounds": bounds,
            "choices": choices,
            "description": _text(
                parameter["description"], _pointer(param_pointer, "description"), maximum=500
            ),
        }
        types[parameter_id] = (parameter_type, typed_value)
    return normalized, types


def _json_pointer(value: Any, pointer: str) -> str:
    raw = _text(value, pointer, maximum=500)
    if not raw.startswith("/") or raw == "/":
        _fail("invalid_pointer", pointer, "expected a non-root RFC 6901 JSON pointer")
    for token in raw.split("/")[1:]:
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                    _fail("invalid_pointer", pointer, "JSON pointer contains an invalid escape")
                index += 2
            else:
                index += 1
    return raw


def _resolve_pointer(document: Any, raw_pointer: str, error_pointer: str) -> None:
    current = document
    for raw_token in raw_pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                _fail("unresolved_pointer", error_pointer, "pointer does not resolve")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                _fail("unresolved_pointer", error_pointer, "array pointer token is invalid")
            index = int(token)
            if index >= len(current):
                _fail("unresolved_pointer", error_pointer, "array pointer index is out of range")
            current = current[index]
        else:
            _fail("unresolved_pointer", error_pointer, "pointer traverses a scalar value")


def _validate_provenance(
    value: Any,
) -> Tuple[Dict[str, Any], List[Tuple[str, str, str]], bool]:
    provenance = _object(value, "/provenance")
    _exact_fields(provenance, {"extraction_run_id", "parent", "sources"}, "/provenance")
    extraction_run_id = _nullable_text(
        provenance["extraction_run_id"], "/provenance/extraction_run_id", maximum=128
    )
    raw_parent = provenance["parent"]
    parent: Optional[Dict[str, Any]] = None
    if raw_parent is not None:
        parent_object = _object(raw_parent, "/provenance/parent")
        _exact_fields(parent_object, {"strategy_id", "revision"}, "/provenance/parent")
        parent_strategy = _text(
            parent_object["strategy_id"], "/provenance/parent/strategy_id", maximum=64
        )
        if _STRATEGY_ID.fullmatch(parent_strategy) is None:
            _fail("invalid_id", "/provenance/parent/strategy_id", "invalid strategy slug")
        parent = {
            "strategy_id": parent_strategy,
            "revision": _integer(
                parent_object["revision"], "/provenance/parent/revision", minimum=1
            ),
        }

    sources = _array(provenance["sources"], "/provenance/sources")
    if not sources or len(sources) > MAX_SOURCES:
        _fail("invalid_length", "/provenance/sources", "sources must contain 1 to 64 items")
    normalized_sources: List[Dict[str, Any]] = []
    source_ids: Set[str] = set()
    evidence_ids: Set[str] = set()
    support_pointers: List[Tuple[str, str, str]] = []
    total_evidence = 0
    has_video = False
    for source_index, raw_source in enumerate(sources):
        source_pointer = _pointer("/provenance/sources", str(source_index))
        source = _object(raw_source, source_pointer)
        _exact_fields(
            source,
            {
                "source_id",
                "type",
                "uri",
                "title",
                "creator",
                "published_at",
                "ingested_at",
                "content_sha256",
                "rights_basis",
                "evidence",
            },
            source_pointer,
        )
        source_id = _token(source["source_id"], _pointer(source_pointer, "source_id"))
        if source_id in source_ids:
            _fail("duplicate_id", _pointer(source_pointer, "source_id"), "source_id is duplicated")
        source_ids.add(source_id)
        source_type = _enum(
            source["type"],
            {"video", "article", "book", "operator", "experiment"},
            _pointer(source_pointer, "type"),
        )
        has_video = has_video or source_type == "video"
        canonical_ingested, ingested_at = _utc_timestamp(
            source["ingested_at"], _pointer(source_pointer, "ingested_at")
        )
        raw_published = source["published_at"]
        canonical_published: Optional[str] = None
        if raw_published is not None:
            canonical_published, published_at = _utc_timestamp(
                raw_published, _pointer(source_pointer, "published_at")
            )
            if published_at > ingested_at:
                _fail(
                    "invalid_time_order",
                    _pointer(source_pointer, "published_at"),
                    "published_at cannot be later than ingested_at",
                )

        evidence = _array(source["evidence"], _pointer(source_pointer, "evidence"))
        total_evidence += len(evidence)
        if total_evidence > MAX_EVIDENCE_ITEMS:
            _fail("too_many_evidence_items", "/provenance/sources", "evidence count exceeds the limit")
        normalized_evidence: List[Dict[str, Any]] = []
        for evidence_index, raw_evidence in enumerate(evidence):
            evidence_pointer = _pointer(_pointer(source_pointer, "evidence"), str(evidence_index))
            item = _object(raw_evidence, evidence_pointer)
            _exact_fields(
                item,
                {
                    "evidence_id",
                    "start_ms",
                    "end_ms",
                    "excerpt_sha256",
                    "supports",
                    "extractor_confidence",
                },
                evidence_pointer,
            )
            evidence_id = _token(item["evidence_id"], _pointer(evidence_pointer, "evidence_id"))
            if evidence_id in evidence_ids:
                _fail("duplicate_id", _pointer(evidence_pointer, "evidence_id"), "evidence_id is duplicated")
            evidence_ids.add(evidence_id)
            raw_start = item["start_ms"]
            raw_end = item["end_ms"]
            if (raw_start is None) != (raw_end is None):
                _fail(
                    "invalid_evidence_time",
                    evidence_pointer,
                    "start_ms and end_ms must both be null or both be integers",
                )
            if raw_start is None:
                start_ms = None
                end_ms = None
            else:
                start_ms = _integer(raw_start, _pointer(evidence_pointer, "start_ms"), minimum=0)
                end_ms = _integer(raw_end, _pointer(evidence_pointer, "end_ms"), minimum=0)
                if end_ms <= start_ms:
                    _fail(
                        "invalid_evidence_time",
                        _pointer(evidence_pointer, "end_ms"),
                        "end_ms must be later than start_ms",
                    )
            if source_type == "video" and start_ms is None:
                _fail(
                    "invalid_evidence_time",
                    evidence_pointer,
                    "video evidence requires a timestamp interval",
                )
            supports = _unique_strings(
                item["supports"],
                _pointer(evidence_pointer, "supports"),
                minimum=1,
                maximum=64,
                item_maximum=500,
            )
            supports = [
                _json_pointer(pointer_value, _pointer(_pointer(evidence_pointer, "supports"), str(index)))
                for index, pointer_value in enumerate(supports)
            ]
            for support_index, support in enumerate(supports):
                support_pointers.append(
                    (
                        support,
                        _pointer(_pointer(evidence_pointer, "supports"), str(support_index)),
                        source_type,
                    )
                )
            raw_confidence = item["extractor_confidence"]
            confidence: Optional[str] = None
            if raw_confidence is not None:
                confidence, confidence_number = _canonical_decimal(
                    raw_confidence, _pointer(evidence_pointer, "extractor_confidence")
                )
                if confidence_number < 0 or confidence_number > 1:
                    _fail(
                        "out_of_range",
                        _pointer(evidence_pointer, "extractor_confidence"),
                        "confidence must be between zero and one",
                    )
            normalized_evidence.append(
                {
                    "evidence_id": evidence_id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "excerpt_sha256": _digest(
                        item["excerpt_sha256"], _pointer(evidence_pointer, "excerpt_sha256")
                    ),
                    "supports": supports,
                    "extractor_confidence": confidence,
                }
            )
        normalized_sources.append(
            {
                "source_id": source_id,
                "type": source_type,
                "uri": _uri(source["uri"], _pointer(source_pointer, "uri")),
                "title": _text(source["title"], _pointer(source_pointer, "title"), maximum=500),
                "creator": _nullable_text(source["creator"], _pointer(source_pointer, "creator"), maximum=200),
                "published_at": canonical_published,
                "ingested_at": canonical_ingested,
                "content_sha256": _digest(
                    source["content_sha256"], _pointer(source_pointer, "content_sha256")
                ),
                "rights_basis": _enum(
                    source["rights_basis"],
                    {"user_supplied", "licensed", "public_api", "operator_authored"},
                    _pointer(source_pointer, "rights_basis"),
                ),
                "evidence": normalized_evidence,
            }
        )
    return {
        "extraction_run_id": extraction_run_id,
        "parent": parent,
        "sources": normalized_sources,
    }, support_pointers, has_video


def _validate_market(value: Any) -> Dict[str, Any]:
    market = _object(value, "/market")
    _exact_fields(
        market,
        {
            "instrument",
            "asset_type",
            "data_feed_id",
            "execution_venue_id",
            "timezone",
            "bar_duration",
            "feature_price",
            "required_quote_fields",
            "required_bar_fields",
        },
        "/market",
    )
    quote_fields = _unique_strings(
        market["required_quote_fields"],
        "/market/required_quote_fields",
        minimum=2,
        maximum=2,
        item_maximum=16,
    )
    if quote_fields != ["bid", "ask"]:
        _fail(
            "invalid_market_scope",
            "/market/required_quote_fields",
            "required quote fields must be exactly [bid, ask]",
        )
    bar_fields = _unique_strings(
        market["required_bar_fields"],
        "/market/required_bar_fields",
        minimum=1,
        maximum=5,
        item_maximum=16,
    )
    allowed_bar_fields = {"open", "high", "low", "close"}
    for index, field_name in enumerate(bar_fields):
        if field_name not in allowed_bar_fields:
            _fail(
                "invalid_market_scope",
                _pointer("/market/required_bar_fields", str(index)),
                "bar field is not supported",
            )
    return {
        "instrument": _enum(market["instrument"], {"XAU_USD"}, "/market/instrument"),
        "asset_type": _enum(
            market["asset_type"], {"spot_metal_cfd"}, "/market/asset_type"
        ),
        "data_feed_id": _text(market["data_feed_id"], "/market/data_feed_id", maximum=128),
        "execution_venue_id": _text(
            market["execution_venue_id"], "/market/execution_venue_id", maximum=128
        ),
        "timezone": _enum(market["timezone"], {"UTC"}, "/market/timezone"),
        "bar_duration": _enum(
            market["bar_duration"],
            {"PT5M", "PT15M", "PT30M", "PT1H", "PT4H"},
            "/market/bar_duration",
        ),
        "feature_price": _enum(market["feature_price"], {"mid"}, "/market/feature_price"),
        "required_quote_fields": quote_fields,
        "required_bar_fields": bar_fields,
    }


def _feature_parameter(
    value: Any,
    pointer: str,
    parameters: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Optional[int]]:
    operand = _object(value, pointer)
    if len(operand) != 1:
        _fail("invalid_feature_parameter", pointer, "feature parameter needs one discriminator")
    if "integer" in operand:
        period = _integer(operand["integer"], _pointer(pointer, "integer"), minimum=1, maximum=10_000)
        return {"integer": period}, period
    if "param" in operand:
        parameter_id = _token(operand["param"], _pointer(pointer, "param"))
        parameter = parameters.get(parameter_id)
        if parameter is None:
            _fail("unresolved_reference", _pointer(pointer, "param"), "parameter does not exist")
        if parameter["type"] != "integer":
            _fail("incompatible_type", _pointer(pointer, "param"), "period parameter must be integer")
        resolved_period = parameter["value"]
        if resolved_period is not None:
            resolved_period = _integer(
                resolved_period,
                _pointer(pointer, "param"),
                minimum=1,
                maximum=10_000,
            )
        bounds = parameter["research_bounds"]
        if bounds is not None:
            bounds_pointer = _pointer(
                _pointer(_pointer("/parameters", parameter_id), "research_bounds"),
                "min",
            )
            if bounds["min"] < 1:
                _fail(
                    "out_of_range",
                    bounds_pointer,
                    "feature-period research bounds cannot include values below 1",
                )
            if bounds["max"] > 10_000:
                _fail(
                    "out_of_range",
                    _pointer(
                        _pointer(_pointer("/parameters", parameter_id), "research_bounds"),
                        "max",
                    ),
                    "feature-period research bounds cannot exceed 10000",
                )
        return {"param": parameter_id}, resolved_period
    _fail(
        "invalid_feature_parameter",
        pointer,
        "period accepts only an integer or integer-parameter reference",
    )


def _validate_features(
    value: Any,
    parameters: Mapping[str, Any],
    market: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, str], Dict[str, int], List[str], bool]:
    features = _array(value, "/features")
    if len(features) > MAX_FEATURES:
        _fail("too_many_features", "/features", "feature count exceeds the limit")
    normalized: List[Dict[str, Any]] = []
    feature_ids: Set[str] = set()
    feature_types: Dict[str, str] = {}
    periods: Dict[str, Optional[int]] = {}
    dependencies: Dict[str, List[str]] = {}
    declarations: Dict[str, int] = {}
    allowed_bar_refs = {"bar.open", "bar.high", "bar.low", "bar.close"}
    required_bar_fields = set(market["required_bar_fields"])

    for index, raw_feature in enumerate(features):
        feature_pointer = _pointer("/features", str(index))
        feature = _object(raw_feature, feature_pointer)
        _exact_fields(
            feature,
            {"id", "kind", "inputs", "params", "output_type", "warmup_bars", "availability"},
            feature_pointer,
        )
        feature_id = _token(feature["id"], _pointer(feature_pointer, "id"))
        if feature_id in feature_ids:
            _fail("duplicate_id", _pointer(feature_pointer, "id"), "feature id is duplicated")
        feature_ids.add(feature_id)
        kind = _enum(
            feature["kind"],
            {"ema", "sma", "rsi", "atr", "rolling_high", "rolling_low"},
            _pointer(feature_pointer, "kind"),
        )
        inputs = _array(feature["inputs"], _pointer(feature_pointer, "inputs"))
        expected_inputs = 3 if kind == "atr" else 1
        if len(inputs) != expected_inputs:
            _fail(
                "invalid_feature_signature",
                _pointer(feature_pointer, "inputs"),
                "{} requires exactly {} input(s)".format(kind, expected_inputs),
            )
        normalized_inputs: List[Dict[str, str]] = []
        dependency_ids: List[str] = []
        for input_index, raw_input in enumerate(inputs):
            input_pointer = _pointer(_pointer(feature_pointer, "inputs"), str(input_index))
            input_object = _object(raw_input, input_pointer)
            _exact_fields(input_object, {"ref"}, input_pointer)
            reference = _text(input_object["ref"], _pointer(input_pointer, "ref"), maximum=128)
            if reference in allowed_bar_refs:
                field_name = reference.split(".", 1)[1]
                if field_name not in required_bar_fields:
                    _fail(
                        "missing_market_field",
                        _pointer(input_pointer, "ref"),
                        "feature uses a bar field absent from required_bar_fields",
                    )
            elif reference.startswith("features."):
                dependency_id = reference.split(".", 1)[1]
                if _LOCAL_ID.fullmatch(dependency_id) is None:
                    _fail("invalid_reference", _pointer(input_pointer, "ref"), "invalid feature reference")
                dependency_ids.append(dependency_id)
            else:
                _fail("invalid_reference", _pointer(input_pointer, "ref"), "feature input is not allowlisted")
            normalized_inputs.append({"ref": reference})
        if kind == "atr":
            actual = [item["ref"] for item in normalized_inputs]
            if actual != ["bar.high", "bar.low", "bar.close"]:
                _fail(
                    "invalid_feature_signature",
                    _pointer(feature_pointer, "inputs"),
                    "atr inputs must be bar.high, bar.low, bar.close in that order",
                )
        params = _object(feature["params"], _pointer(feature_pointer, "params"))
        _exact_fields(params, {"period"}, _pointer(feature_pointer, "params"))
        normalized_period, period = _feature_parameter(
            params["period"],
            _pointer(_pointer(feature_pointer, "params"), "period"),
            parameters,
        )
        declared_warmup = _integer(
            feature["warmup_bars"],
            _pointer(feature_pointer, "warmup_bars"),
            minimum=1,
            maximum=1_000_000,
        )
        normalized.append(
            {
                "id": feature_id,
                "kind": kind,
                "inputs": normalized_inputs,
                "params": {"period": normalized_period},
                "output_type": _enum(
                    feature["output_type"], {"decimal"}, _pointer(feature_pointer, "output_type")
                ),
                "warmup_bars": declared_warmup,
                "availability": _enum(
                    feature["availability"], {"bar_close"}, _pointer(feature_pointer, "availability")
                ),
            }
        )
        feature_types[feature_id] = "numeric"
        periods[feature_id] = period
        dependencies[feature_id] = dependency_ids
        declarations[feature_id] = declared_warmup

    for feature_id, dependency_ids in dependencies.items():
        for dependency_id in dependency_ids:
            if dependency_id not in feature_ids:
                _fail(
                    "unresolved_reference",
                    "/features",
                    "feature {} depends on missing feature {}".format(feature_id, dependency_id),
                )

    visiting: Set[str] = set()
    visited: Set[str] = set()
    topological: List[str] = []

    def visit(feature_id: str) -> None:
        if feature_id in visiting:
            _fail("feature_cycle", "/features", "feature dependency graph contains a cycle")
        if feature_id in visited:
            return
        visiting.add(feature_id)
        for dependency_id in dependencies[feature_id]:
            visit(dependency_id)
        visiting.remove(feature_id)
        visited.add(feature_id)
        topological.append(feature_id)

    for feature in normalized:
        visit(feature["id"])

    effective_warmups: Dict[str, int] = {}
    warnings: List[str] = []
    compilation_ready = True
    kinds = {feature["id"]: feature["kind"] for feature in normalized}
    for feature_id in topological:
        period = periods[feature_id]
        if period is None:
            warnings.append(
                "feature {} warm-up is unverified because its period parameter is unresolved".format(
                    feature_id
                )
            )
            compilation_ready = False
            continue
        local_warmup = period + 1 if kinds[feature_id] in {"rsi", "atr"} else period
        dependency_warmups = [effective_warmups.get(item) for item in dependencies[feature_id]]
        if any(item is None for item in dependency_warmups):
            compilation_ready = False
            continue
        effective = local_warmup
        if dependency_warmups:
            effective += max(dependency_warmups) - 1  # type: ignore[arg-type]
        effective_warmups[feature_id] = effective
        if declarations[feature_id] < effective:
            _fail(
                "understated_warmup",
                "/features",
                "feature {} declares {} bars but requires at least {}".format(
                    feature_id, declarations[feature_id], effective
                ),
            )
    return normalized, feature_types, effective_warmups, warnings, compilation_ready


def _operand(
    value: Any,
    pointer: str,
    parameters: Mapping[str, Any],
    features: Mapping[str, str],
    market: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    operand = _object(value, pointer)
    if len(operand) != 1:
        _fail("invalid_operand", pointer, "operand must contain exactly one discriminator")
    key = next(iter(operand))
    raw = operand[key]
    if key == "ref":
        reference = _text(raw, _pointer(pointer, "ref"), maximum=128)
        numeric_refs = {
            "bar.open",
            "bar.high",
            "bar.low",
            "bar.close",
            "quote.bid",
            "quote.ask",
            "position.unrealized_r",
        }
        if reference in numeric_refs:
            if reference.startswith("bar."):
                field_name = reference.split(".", 1)[1]
                if field_name not in set(market["required_bar_fields"]):
                    _fail(
                        "missing_market_field",
                        _pointer(pointer, "ref"),
                        "rule uses a bar field absent from required_bar_fields",
                    )
            return {"ref": reference}, {
                "type": "numeric",
                "time_varying": True,
                "enum_choices": None,
            }
        if reference == "position.side":
            return {"ref": reference}, {
                "type": "enum",
                "time_varying": True,
                "enum_choices": {"flat", "long", "short"},
            }
        if reference.startswith("features."):
            feature_id = reference.split(".", 1)[1]
            if feature_id not in features:
                _fail("unresolved_reference", _pointer(pointer, "ref"), "feature does not exist")
            return {"ref": reference}, {
                "type": "numeric",
                "time_varying": True,
                "enum_choices": None,
            }
        _fail("invalid_reference", _pointer(pointer, "ref"), "operand reference is not allowlisted")
    if key == "param":
        parameter_id = _token(raw, _pointer(pointer, "param"))
        parameter = parameters.get(parameter_id)
        if parameter is None:
            _fail("unresolved_reference", _pointer(pointer, "param"), "parameter does not exist")
        value_type = parameter["type"]
        return {"param": parameter_id}, {
            "type": "numeric" if value_type in {"integer", "decimal"} else value_type,
            "time_varying": False,
            "enum_choices": set(parameter["choices"] or ()) if value_type == "enum" else None,
        }
    if key == "decimal":
        canonical, _ = _canonical_decimal(raw, _pointer(pointer, "decimal"))
        return {"decimal": canonical}, {"type": "numeric", "time_varying": False, "enum_choices": None}
    if key == "integer":
        integer = _integer(raw, _pointer(pointer, "integer"))
        return {"integer": integer}, {"type": "numeric", "time_varying": False, "enum_choices": None}
    if key == "boolean":
        boolean = _boolean(raw, _pointer(pointer, "boolean"))
        return {"boolean": boolean}, {"type": "boolean", "time_varying": False, "enum_choices": None}
    if key == "enum":
        literal = _text(raw, _pointer(pointer, "enum"), maximum=64)
        return {"enum": literal}, {"type": "enum", "time_varying": False, "enum_choices": {literal}}
    _fail("invalid_operand", pointer, "operand discriminator is not allowlisted")


def _condition(
    value: Any,
    pointer: str,
    parameters: Mapping[str, Any],
    features: Mapping[str, str],
    market: Mapping[str, Any],
    counter: List[int],
    depth: int = 0,
) -> Dict[str, Any]:
    if depth > MAX_AST_DEPTH:
        _fail("ast_too_deep", pointer, "condition nesting exceeds the configured limit")
    counter[0] += 1
    if counter[0] > MAX_AST_NODES:
        _fail("ast_too_large", pointer, "condition node count exceeds the configured limit")
    condition = _object(value, pointer)
    _exact_fields(condition, {"op", "args"}, pointer)
    operation = _enum(
        condition["op"],
        {
            "all",
            "any",
            "not",
            "gt",
            "gte",
            "lt",
            "lte",
            "eq",
            "neq",
            "crosses_above",
            "crosses_below",
        },
        _pointer(pointer, "op"),
    )
    args = _array(condition["args"], _pointer(pointer, "args"))
    if operation in {"all", "any"}:
        if not args:
            _fail("invalid_arity", _pointer(pointer, "args"), "all/any require at least one condition")
        return {
            "op": operation,
            "args": [
                _condition(
                    item,
                    _pointer(_pointer(pointer, "args"), str(index)),
                    parameters,
                    features,
                    market,
                    counter,
                    depth + 1,
                )
                for index, item in enumerate(args)
            ],
        }
    if operation == "not":
        if len(args) != 1:
            _fail("invalid_arity", _pointer(pointer, "args"), "not requires exactly one condition")
        return {
            "op": operation,
            "args": [
                _condition(
                    args[0],
                    _pointer(_pointer(pointer, "args"), "0"),
                    parameters,
                    features,
                    market,
                    counter,
                    depth + 1,
                )
            ],
        }
    if len(args) != 2:
        _fail("invalid_arity", _pointer(pointer, "args"), "comparison requires two operands")
    left, left_meta = _operand(
        args[0], _pointer(_pointer(pointer, "args"), "0"), parameters, features, market
    )
    right, right_meta = _operand(
        args[1], _pointer(_pointer(pointer, "args"), "1"), parameters, features, market
    )
    if operation in {"gt", "gte", "lt", "lte", "crosses_above", "crosses_below"}:
        if left_meta["type"] != "numeric" or right_meta["type"] != "numeric":
            _fail("incompatible_type", pointer, "operator requires numeric operands")
    elif left_meta["type"] != right_meta["type"]:
        _fail("incompatible_type", pointer, "equality operands use different type domains")
    if operation in {"crosses_above", "crosses_below"}:
        if not left_meta["time_varying"] and not right_meta["time_varying"]:
            _fail("invalid_cross", pointer, "cross requires at least one time-varying operand")
        if any(
            operand_value.get("ref", "").startswith("position.")
            for operand_value in (left, right)
        ):
            _fail(
                "invalid_cross",
                pointer,
                "the current profile has no prior-position series for cross evaluation",
            )
    if left_meta["type"] == "enum" and right_meta["type"] == "enum":
        left_choices = left_meta["enum_choices"]
        right_choices = right_meta["enum_choices"]
        if left_choices is not None and right_choices is not None and not (left_choices & right_choices):
            _fail("incompatible_type", pointer, "enum domains do not overlap")
    return {"op": operation, "args": [left, right]}


def _validate_schedule(value: Any, bar_duration: str) -> Dict[str, Any]:
    schedule = _object(value, "/schedule")
    _exact_fields(
        schedule,
        {"days", "windows", "exclude_dates", "close_before_weekend"},
        "/schedule",
    )
    raw_days = _unique_strings(
        schedule["days"], "/schedule/days", minimum=1, maximum=5, item_maximum=3
    )
    weekday_order = ["MON", "TUE", "WED", "THU", "FRI"]
    for index, day in enumerate(raw_days):
        if day not in weekday_order:
            _fail("invalid_schedule", _pointer("/schedule/days", str(index)), "day is not supported")
    days = [day for day in weekday_order if day in raw_days]

    raw_windows = _array(schedule["windows"], "/schedule/windows")
    if not raw_windows or len(raw_windows) > 16:
        _fail("invalid_length", "/schedule/windows", "windows must contain 1 to 16 items")
    windows_with_times: List[Tuple[time, time, Dict[str, str]]] = []
    for index, raw_window in enumerate(raw_windows):
        window_pointer = _pointer("/schedule/windows", str(index))
        window = _object(raw_window, window_pointer)
        _exact_fields(window, {"start_utc", "end_utc"}, window_pointer)
        start_text, start = _clock_time(window["start_utc"], _pointer(window_pointer, "start_utc"))
        end_text, end = _clock_time(window["end_utc"], _pointer(window_pointer, "end_utc"))
        if end <= start:
            _fail("invalid_schedule", window_pointer, "window must end later on the same UTC day")
        windows_with_times.append((start, end, {"start_utc": start_text, "end_utc": end_text}))
    windows_with_times.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(windows_with_times, windows_with_times[1:]):
        if current[0] < previous[1]:
            _fail("overlapping_schedule", "/schedule/windows", "UTC windows must not overlap")

    exclude_dates = _unique_strings(
        schedule["exclude_dates"],
        "/schedule/exclude_dates",
        maximum=366,
        item_maximum=10,
    )
    canonical_dates = sorted(
        _calendar_date(item, _pointer("/schedule/exclude_dates", str(index)))
        for index, item in enumerate(exclude_dates)
    )
    close = _object(schedule["close_before_weekend"], "/schedule/close_before_weekend")
    _exact_fields(close, {"enabled", "cutoff_utc"}, "/schedule/close_before_weekend")
    enabled = _boolean(close["enabled"], "/schedule/close_before_weekend/enabled")
    raw_cutoff = close["cutoff_utc"]
    if enabled:
        cutoff, cutoff_time = _clock_time(
            raw_cutoff, "/schedule/close_before_weekend/cutoff_utc"
        )
        duration_minutes = {
            "PT5M": 5,
            "PT15M": 15,
            "PT30M": 30,
            "PT1H": 60,
            "PT4H": 240,
        }[bar_duration]
        cutoff_minutes = cutoff_time.hour * 60 + cutoff_time.minute
        if cutoff_minutes % duration_minutes != 0:
            _fail(
                "invalid_schedule",
                "/schedule/close_before_weekend/cutoff_utc",
                "weekend cutoff must align to a completed bar boundary",
            )
    else:
        if raw_cutoff is not None:
            _fail(
                "invalid_schedule",
                "/schedule/close_before_weekend/cutoff_utc",
                "cutoff must be null when weekend closing is disabled",
            )
        cutoff = None
    return {
        "days": days,
        "windows": [item[2] for item in windows_with_times],
        "exclude_dates": canonical_dates,
        "close_before_weekend": {"enabled": enabled, "cutoff_utc": cutoff},
    }


def _optional_condition(
    value: Any,
    pointer: str,
    parameters: Mapping[str, Any],
    features: Mapping[str, str],
    market: Mapping[str, Any],
    counter: List[int],
) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    return _condition(value, pointer, parameters, features, market, counter)


def _condition_requires_single_bar_confirmation(condition: Optional[Mapping[str, Any]]) -> bool:
    """Return whether repeated truth cannot represent v1 multi-bar confirmation safely."""

    if condition is None:
        return False
    if condition["op"] in {"crosses_above", "crosses_below"}:
        return True
    if condition["op"] in {"all", "any", "not"}:
        return any(
            _condition_requires_single_bar_confirmation(child)
            for child in condition["args"]
        )
    return any(
        operand.get("ref", "").startswith("position.")
        for operand in condition["args"]
    )


def _condition_uses_reference(
    condition: Optional[Mapping[str, Any]],
    reference: str,
) -> bool:
    if condition is None:
        return False
    if condition["op"] in {"all", "any", "not"}:
        return any(
            _condition_uses_reference(child, reference)
            for child in condition["args"]
        )
    return any(operand.get("ref") == reference for operand in condition["args"])


def _validate_entry(
    value: Any,
    parameters: Mapping[str, Any],
    features: Mapping[str, str],
    market: Mapping[str, Any],
    counter: List[int],
) -> Dict[str, Any]:
    entry = _object(value, "/entry")
    _exact_fields(
        entry,
        {"long", "short", "confirmation_bars", "cooldown_bars", "max_entries_per_bar"},
        "/entry",
    )
    long_condition = _optional_condition(
        entry["long"], "/entry/long", parameters, features, market, counter
    )
    short_condition = _optional_condition(
        entry["short"], "/entry/short", parameters, features, market, counter
    )
    if long_condition is None and short_condition is None:
        _fail("missing_entry", "/entry", "at least one direction needs an entry condition")
    for direction, condition in (("long", long_condition), ("short", short_condition)):
        if _condition_uses_reference(condition, "position.unrealized_r"):
            _fail(
                "invalid_reference",
                _pointer("/entry", direction),
                "flat-entry evaluation has no unrealized_r value",
            )
    max_entries = _integer(entry["max_entries_per_bar"], "/entry/max_entries_per_bar")
    if max_entries != 1:
        _fail("invalid_execution_semantics", "/entry/max_entries_per_bar", "v1 requires exactly one")
    confirmation_bars = _integer(
        entry["confirmation_bars"], "/entry/confirmation_bars", minimum=1, maximum=100
    )
    if confirmation_bars > 1 and any(
        _condition_requires_single_bar_confirmation(condition)
        for condition in (long_condition, short_condition)
    ):
        _fail(
            "invalid_confirmation",
            "/entry/confirmation_bars",
            "multi-bar confirmation cannot repeat cross events or current-position references",
        )
    return {
        "long": long_condition,
        "short": short_condition,
        "confirmation_bars": confirmation_bars,
        "cooldown_bars": _integer(
            entry["cooldown_bars"], "/entry/cooldown_bars", minimum=0, maximum=100_000
        ),
        "max_entries_per_bar": 1,
    }


def _feature_reference(value: Any, pointer: str, features: Mapping[str, str]) -> str:
    reference = _text(value, pointer, maximum=128)
    if not reference.startswith("features."):
        _fail("invalid_reference", pointer, "expected a feature reference")
    feature_id = reference.split(".", 1)[1]
    if feature_id not in features:
        _fail("unresolved_reference", pointer, "feature does not exist")
    return reference


def _positive_multiple(
    value: Any,
    pointer: str,
    parameters: Mapping[str, Any],
) -> Dict[str, Any]:
    multiple = _object(value, pointer)
    if len(multiple) != 1:
        _fail("invalid_multiple", pointer, "multiple needs one discriminator")
    if "decimal" in multiple:
        return {"decimal": _positive_decimal(multiple["decimal"], _pointer(pointer, "decimal"))}
    if "param" in multiple:
        parameter_id = _token(multiple["param"], _pointer(pointer, "param"))
        parameter = parameters.get(parameter_id)
        if parameter is None:
            _fail("unresolved_reference", _pointer(pointer, "param"), "parameter does not exist")
        if parameter["type"] != "decimal":
            _fail("incompatible_type", _pointer(pointer, "param"), "multiple parameter must be decimal")
        if parameter["value"] is not None and Decimal(parameter["value"]) <= 0:
            _fail("out_of_range", _pointer(pointer, "param"), "multiple parameter must be positive")
        bounds = parameter["research_bounds"]
        if bounds is not None and Decimal(bounds["min"]) <= 0:
            _fail(
                "out_of_range",
                _pointer(
                    _pointer(_pointer("/parameters", parameter_id), "research_bounds"),
                    "min",
                ),
                "multiple research bounds must remain strictly positive",
            )
        return {"param": parameter_id}
    _fail("invalid_multiple", pointer, "multiple accepts decimal or parameter only")


def _validate_exit(
    value: Any,
    parameters: Mapping[str, Any],
    features: Mapping[str, str],
    market: Mapping[str, Any],
    counter: List[int],
) -> Dict[str, Any]:
    exit_rules = _object(value, "/exit")
    _exact_fields(
        exit_rules,
        {"signal", "initial_stop", "take_profit", "trailing_stop", "max_holding_bars"},
        "/exit",
    )
    signal = _object(exit_rules["signal"], "/exit/signal")
    _exact_fields(signal, {"long", "short"}, "/exit/signal")
    normalized_signal = {
        "long": _optional_condition(
            signal["long"], "/exit/signal/long", parameters, features, market, counter
        ),
        "short": _optional_condition(
            signal["short"], "/exit/signal/short", parameters, features, market, counter
        ),
    }

    stop = _object(exit_rules["initial_stop"], "/exit/initial_stop")
    _exact_fields(stop, {"type", "feature_ref", "multiple", "snapshot"}, "/exit/initial_stop")
    initial_stop = {
        "type": _enum(stop["type"], {"feature_multiple"}, "/exit/initial_stop/type"),
        "feature_ref": _feature_reference(
            stop["feature_ref"], "/exit/initial_stop/feature_ref", features
        ),
        "multiple": _positive_multiple(
            stop["multiple"], "/exit/initial_stop/multiple", parameters
        ),
        "snapshot": _enum(stop["snapshot"], {"at_signal"}, "/exit/initial_stop/snapshot"),
    }

    raw_take_profit = exit_rules["take_profit"]
    take_profit: Optional[Dict[str, Any]] = None
    if raw_take_profit is not None:
        take = _object(raw_take_profit, "/exit/take_profit")
        _exact_fields(take, {"type", "multiple"}, "/exit/take_profit")
        take_profit = {
            "type": _enum(
                take["type"], {"initial_risk_multiple"}, "/exit/take_profit/type"
            ),
            "multiple": _positive_multiple(
                take["multiple"], "/exit/take_profit/multiple", parameters
            ),
        }

    raw_trailing = exit_rules["trailing_stop"]
    trailing: Optional[Dict[str, Any]] = None
    if raw_trailing is not None:
        trail = _object(raw_trailing, "/exit/trailing_stop")
        trail_type = trail.get("type")
        if trail_type == "feature_multiple":
            _exact_fields(
                trail,
                {"type", "feature_ref", "multiple", "activation_r", "update_at"},
                "/exit/trailing_stop",
            )
            trailing = {
                "type": "feature_multiple",
                "feature_ref": _feature_reference(
                    trail["feature_ref"], "/exit/trailing_stop/feature_ref", features
                ),
                "multiple": _positive_multiple(
                    trail["multiple"], "/exit/trailing_stop/multiple", parameters
                ),
                "activation_r": _positive_decimal(
                    trail["activation_r"], "/exit/trailing_stop/activation_r"
                ),
                "update_at": _enum(
                    trail["update_at"], {"bar_close"}, "/exit/trailing_stop/update_at"
                ),
            }
        elif trail_type == "break_even":
            _exact_fields(
                trail,
                {"type", "activation_r", "offset_r", "update_at"},
                "/exit/trailing_stop",
            )
            trailing = {
                "type": "break_even",
                "activation_r": _positive_decimal(
                    trail["activation_r"], "/exit/trailing_stop/activation_r"
                ),
                "offset_r": _positive_decimal(
                    trail["offset_r"], "/exit/trailing_stop/offset_r", allow_zero=True
                ),
                "update_at": _enum(
                    trail["update_at"], {"bar_close"}, "/exit/trailing_stop/update_at"
                ),
            }
        else:
            _fail("invalid_enum", "/exit/trailing_stop/type", "trailing-stop type is not supported")

    raw_holding = exit_rules["max_holding_bars"]
    max_holding = (
        None
        if raw_holding is None
        else _integer(raw_holding, "/exit/max_holding_bars", minimum=1, maximum=1_000_000)
    )
    return {
        "signal": normalized_signal,
        "initial_stop": initial_stop,
        "take_profit": take_profit,
        "trailing_stop": trailing,
        "max_holding_bars": max_holding,
    }


def _validate_sizing(value: Any) -> Dict[str, Any]:
    sizing = _object(value, "/sizing")
    _exact_fields(
        sizing,
        {
            "type",
            "risk_fraction_of_equity",
            "equity_source",
            "stop_distance_source",
            "quantity_rounding",
            "min_units",
            "max_units",
            "unit_step",
        },
        "/sizing",
    )
    risk_text, risk_number = _canonical_decimal(
        sizing["risk_fraction_of_equity"], "/sizing/risk_fraction_of_equity"
    )
    if risk_number <= 0 or risk_number > 1:
        _fail("out_of_range", "/sizing/risk_fraction_of_equity", "risk fraction must be in (0, 1]")
    min_text, minimum = _canonical_decimal(sizing["min_units"], "/sizing/min_units")
    max_text, maximum = _canonical_decimal(sizing["max_units"], "/sizing/max_units")
    step_text, step = _canonical_decimal(sizing["unit_step"], "/sizing/unit_step")
    if minimum <= 0 or maximum < minimum or step <= 0:
        _fail("invalid_sizing", "/sizing", "unit bounds and step are inconsistent")
    if not _aligned(minimum, Decimal(0), step, "/sizing/min_units") or not _aligned(
        maximum, Decimal(0), step, "/sizing/max_units"
    ):
        _fail("invalid_sizing", "/sizing", "unit limits must align to unit_step")
    return {
        "type": _enum(sizing["type"], {"fixed_fractional"}, "/sizing/type"),
        "risk_fraction_of_equity": risk_text,
        "equity_source": _enum(
            sizing["equity_source"], {"paper_account_nav"}, "/sizing/equity_source"
        ),
        "stop_distance_source": _enum(
            sizing["stop_distance_source"], {"initial_stop"}, "/sizing/stop_distance_source"
        ),
        "quantity_rounding": _enum(
            sizing["quantity_rounding"], {"down"}, "/sizing/quantity_rounding"
        ),
        "min_units": min_text,
        "max_units": max_text,
        "unit_step": step_text,
    }


def _validate_execution(value: Any) -> Dict[str, Any]:
    execution = _object(value, "/execution")
    expected = {
        "environment",
        "decision_point",
        "earliest_entry",
        "entry_order_type",
        "time_in_force",
        "allow_partial_fill",
        "price_side",
        "cost_model_id",
        "intrabar_ambiguity",
        "signal_ttl_bars",
    }
    _exact_fields(execution, expected, "/execution")
    time_in_force = _enum(execution["time_in_force"], {"FOK", "IOC"}, "/execution/time_in_force")
    partial = _boolean(execution["allow_partial_fill"], "/execution/allow_partial_fill")
    if time_in_force == "FOK" and partial:
        _fail(
            "invalid_execution_semantics",
            "/execution/allow_partial_fill",
            "FOK cannot allow partial fills",
        )
    return {
        "environment": _enum(execution["environment"], {"PAPER"}, "/execution/environment"),
        "decision_point": _enum(
            execution["decision_point"], {"bar_close"}, "/execution/decision_point"
        ),
        "earliest_entry": _enum(
            execution["earliest_entry"], {"next_observable_quote"}, "/execution/earliest_entry"
        ),
        "entry_order_type": _enum(
            execution["entry_order_type"], {"market"}, "/execution/entry_order_type"
        ),
        "time_in_force": time_in_force,
        "allow_partial_fill": partial,
        "price_side": _enum(
            execution["price_side"], {"natural_bid_ask"}, "/execution/price_side"
        ),
        "cost_model_id": _text(execution["cost_model_id"], "/execution/cost_model_id", maximum=128),
        "intrabar_ambiguity": _enum(
            execution["intrabar_ambiguity"], {"stop_first"}, "/execution/intrabar_ambiguity"
        ),
        "signal_ttl_bars": _integer(
            execution["signal_ttl_bars"], "/execution/signal_ttl_bars", minimum=1, maximum=100
        ),
    }


def _validate_risk(value: Any) -> Dict[str, Any]:
    risk = _object(value, "/risk")
    expected = {
        "max_open_positions",
        "max_trades_per_utc_day",
        "max_daily_loss_fraction",
        "max_spread_price",
        "max_quote_age_ms",
        "min_stop_distance_price",
        "max_stop_distance_price",
        "close_on_data_stale",
        "entry_kill_switch_required",
    }
    _exact_fields(risk, expected, "/risk")
    max_open = _integer(risk["max_open_positions"], "/risk/max_open_positions")
    if max_open != 1:
        _fail("invalid_risk_policy", "/risk/max_open_positions", "v1 requires exactly one")
    daily_text, daily = _canonical_decimal(
        risk["max_daily_loss_fraction"], "/risk/max_daily_loss_fraction"
    )
    if daily <= 0 or daily > 1:
        _fail("out_of_range", "/risk/max_daily_loss_fraction", "daily loss fraction must be in (0, 1]")
    spread = _positive_decimal(risk["max_spread_price"], "/risk/max_spread_price")
    min_text, minimum = _canonical_decimal(
        risk["min_stop_distance_price"], "/risk/min_stop_distance_price"
    )
    max_text, maximum = _canonical_decimal(
        risk["max_stop_distance_price"], "/risk/max_stop_distance_price"
    )
    if minimum <= 0 or maximum < minimum:
        _fail("invalid_risk_policy", "/risk", "stop-distance bounds are inconsistent")
    kill_switch = _boolean(
        risk["entry_kill_switch_required"], "/risk/entry_kill_switch_required"
    )
    if not kill_switch:
        _fail(
            "invalid_risk_policy",
            "/risk/entry_kill_switch_required",
            "entry kill switch is mandatory",
        )
    return {
        "max_open_positions": 1,
        "max_trades_per_utc_day": _integer(
            risk["max_trades_per_utc_day"],
            "/risk/max_trades_per_utc_day",
            minimum=1,
            maximum=100_000,
        ),
        "max_daily_loss_fraction": daily_text,
        "max_spread_price": spread,
        "max_quote_age_ms": _integer(
            risk["max_quote_age_ms"], "/risk/max_quote_age_ms", minimum=1, maximum=86_400_000
        ),
        "min_stop_distance_price": min_text,
        "max_stop_distance_price": max_text,
        "close_on_data_stale": _boolean(
            risk["close_on_data_stale"], "/risk/close_on_data_stale"
        ),
        "entry_kill_switch_required": True,
    }


def _validate_assumptions(value: Any) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]], bool]:
    assumptions = _array(value, "/assumptions")
    if len(assumptions) > 256:
        _fail("invalid_length", "/assumptions", "assumption count exceeds the limit")
    normalized: List[Dict[str, Any]] = []
    identifiers: Set[str] = set()
    pointers: List[Tuple[str, str]] = []
    has_operator_interpretation = False
    for index, raw_assumption in enumerate(assumptions):
        assumption_pointer = _pointer("/assumptions", str(index))
        assumption = _object(raw_assumption, assumption_pointer)
        _exact_fields(assumption, {"id", "text", "affects", "source"}, assumption_pointer)
        identifier = _token(assumption["id"], _pointer(assumption_pointer, "id"))
        if identifier in identifiers:
            _fail("duplicate_id", _pointer(assumption_pointer, "id"), "assumption id is duplicated")
        identifiers.add(identifier)
        source = _enum(
            assumption["source"],
            {"explicit", "operator_interpretation", "simulation_policy"},
            _pointer(assumption_pointer, "source"),
        )
        has_operator_interpretation = has_operator_interpretation or source == "operator_interpretation"
        affects = _unique_strings(
            assumption["affects"],
            _pointer(assumption_pointer, "affects"),
            minimum=1,
            maximum=64,
            item_maximum=500,
        )
        canonical_affects: List[str] = []
        for affect_index, raw_pointer in enumerate(affects):
            error_pointer = _pointer(_pointer(assumption_pointer, "affects"), str(affect_index))
            canonical = _json_pointer(raw_pointer, error_pointer)
            canonical_affects.append(canonical)
            pointers.append((canonical, error_pointer))
        normalized.append(
            {
                "id": identifier,
                "text": _text(assumption["text"], _pointer(assumption_pointer, "text"), maximum=2_000),
                "affects": canonical_affects,
                "source": source,
            }
        )
    return normalized, pointers, has_operator_interpretation


def _validate_unresolved(value: Any) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    unresolved = _array(value, "/unresolved_items")
    if len(unresolved) > 256:
        _fail("invalid_length", "/unresolved_items", "unresolved-item count exceeds the limit")
    normalized: List[Dict[str, Any]] = []
    identifiers: Set[str] = set()
    pointers: List[Tuple[str, str]] = []
    for index, raw_item in enumerate(unresolved):
        item_pointer = _pointer("/unresolved_items", str(index))
        item = _object(raw_item, item_pointer)
        _exact_fields(item, {"id", "question", "affects", "severity"}, item_pointer)
        identifier = _token(item["id"], _pointer(item_pointer, "id"))
        if identifier in identifiers:
            _fail("duplicate_id", _pointer(item_pointer, "id"), "unresolved id is duplicated")
        identifiers.add(identifier)
        affects = _unique_strings(
            item["affects"],
            _pointer(item_pointer, "affects"),
            minimum=1,
            maximum=64,
            item_maximum=500,
        )
        canonical_affects: List[str] = []
        for affect_index, raw_pointer in enumerate(affects):
            error_pointer = _pointer(_pointer(item_pointer, "affects"), str(affect_index))
            canonical = _json_pointer(raw_pointer, error_pointer)
            canonical_affects.append(canonical)
            pointers.append((canonical, error_pointer))
        normalized.append(
            {
                "id": identifier,
                "question": _text(item["question"], _pointer(item_pointer, "question"), maximum=2_000),
                "affects": canonical_affects,
                "severity": _enum(
                    item["severity"],
                    {"blocking", "non_blocking"},
                    _pointer(item_pointer, "severity"),
                ),
            }
        )
    return normalized, pointers


def _path_covers(pointer_value: str, material_path: str) -> bool:
    return pointer_value == material_path or material_path.startswith(pointer_value + "/")


@dataclass(frozen=True)
class ValidatedStrategySpecV1:
    """Immutable canonical StrategySpec artifact; results and registry state are excluded."""

    canonical_json: str
    spec_hash: str
    warnings: Tuple[str, ...]
    compilation_ready: bool
    promotion_ready: bool

    def __post_init__(self) -> None:
        try:
            document = json.loads(self.canonical_json, parse_constant=_reject_constant)
        except (json.JSONDecodeError, StrategySpecValidationError) as exc:
            raise ValueError("canonical_json is invalid") from exc
        canonical = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if canonical != self.canonical_json:
            raise ValueError("canonical_json is not canonical")
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.spec_hash != expected:
            raise ValueError("spec_hash does not match canonical_json")
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def as_dict(self) -> Dict[str, Any]:
        """Return a detached mutable copy of the canonical document."""

        return json.loads(self.canonical_json)


def validate_strategy_spec_v1(
    document: Mapping[str, Any],
    *,
    require_promotable: bool = False,
) -> ValidatedStrategySpecV1:
    """Validate and canonicalize one complete JSON-shaped StrategySpec v1 document."""

    _assert_json_tree(document, "")
    root = _object(document, "")
    try:
        serialized_size = len(
            json.dumps(root, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
    except (TypeError, ValueError) as exc:
        _fail("invalid_json_shape", "/", "document cannot be represented as strict JSON: {}".format(exc))
    if serialized_size > MAX_DOCUMENT_BYTES:
        _fail("document_too_large", "/", "StrategySpec exceeds the one-megabyte limit")
    _exact_fields(root, _TOP_LEVEL_FIELDS, "")

    if root["spec_version"] != SPEC_VERSION:
        _fail("unsupported_version", "/spec_version", "only StrategySpec 1.0 is supported")
    strategy_id = _text(root["strategy_id"], "/strategy_id", maximum=64)
    if _STRATEGY_ID.fullmatch(strategy_id) is None:
        _fail("invalid_id", "/strategy_id", "strategy_id is not a valid lowercase slug")
    revision = _integer(root["revision"], "/revision", minimum=1)
    canonical_created, created_at = _utc_timestamp(root["created_at"], "/created_at")
    raw_frozen = root["frozen_at"]
    canonical_frozen: Optional[str] = None
    frozen_at: Optional[datetime] = None
    if raw_frozen is not None:
        canonical_frozen, frozen_at = _utc_timestamp(raw_frozen, "/frozen_at")
        if frozen_at < created_at:
            _fail("invalid_time_order", "/frozen_at", "frozen_at cannot precede created_at")

    provenance, evidence_pointers, has_video = _validate_provenance(root["provenance"])
    if frozen_at is not None:
        for source_index, source in enumerate(provenance["sources"]):
            source_pointer = _pointer("/provenance/sources", str(source_index))
            _, ingested_at = _utc_timestamp(
                source["ingested_at"], _pointer(source_pointer, "ingested_at")
            )
            if ingested_at > frozen_at:
                _fail(
                    "future_provenance",
                    _pointer(source_pointer, "ingested_at"),
                    "a frozen strategy cannot cite evidence ingested after frozen_at",
                )
    parent = provenance["parent"]
    if revision == 1 and parent is not None:
        _fail("invalid_parent", "/provenance/parent", "revision 1 cannot have a parent")
    if revision > 1:
        if parent is None:
            _fail("invalid_parent", "/provenance/parent", "later revisions require a parent")
        if parent["strategy_id"] != strategy_id or parent["revision"] != revision - 1:
            _fail(
                "invalid_parent",
                "/provenance/parent",
                "parent must be the immediately preceding revision of this strategy",
            )

    market = _validate_market(root["market"])
    parameters, _ = _validate_parameters(root["parameters"])
    features, feature_types, effective_warmups, warnings, feature_ready = _validate_features(
        root["features"], parameters, market
    )
    condition_counter = [0]
    schedule = _validate_schedule(root["schedule"], market["bar_duration"])
    entry = _validate_entry(
        root["entry"], parameters, feature_types, market, condition_counter
    )
    exit_rules = _validate_exit(
        root["exit"], parameters, feature_types, market, condition_counter
    )
    sizing = _validate_sizing(root["sizing"])
    execution = _validate_execution(root["execution"])
    risk = _validate_risk(root["risk"])
    assumptions, assumption_pointers, has_operator_interpretation = _validate_assumptions(
        root["assumptions"]
    )
    unresolved, unresolved_pointers = _validate_unresolved(root["unresolved_items"])
    tags = _unique_strings(root["tags"], "/tags", maximum=32, item_maximum=64)

    normalized: Dict[str, Any] = {
        "spec_version": SPEC_VERSION,
        "strategy_id": strategy_id,
        "revision": revision,
        "name": _text(root["name"], "/name", maximum=120),
        "description": _text(root["description"], "/description", minimum=0, maximum=2_000),
        "created_at": canonical_created,
        "frozen_at": canonical_frozen,
        "provenance": provenance,
        "market": market,
        "parameters": parameters,
        "features": features,
        "schedule": schedule,
        "entry": entry,
        "exit": exit_rules,
        "sizing": sizing,
        "execution": execution,
        "risk": risk,
        "assumptions": assumptions,
        "unresolved_items": unresolved,
        "tags": tags,
    }

    for pointer_value, error_pointer, _ in evidence_pointers:
        _resolve_pointer(normalized, pointer_value, error_pointer)
    for pointer_value, error_pointer in assumption_pointers + unresolved_pointers:
        _resolve_pointer(normalized, pointer_value, error_pointer)

    if has_operator_interpretation:
        has_operator_source = any(
            source["type"] == "operator" for source in provenance["sources"]
        )
        if not has_operator_source:
            _fail(
                "missing_operator_source",
                "/assumptions",
                "operator interpretations require an operator provenance source",
            )

    used_parameters: Set[str] = set()

    def collect_parameter_references(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) == {"param"} and isinstance(value["param"], str):
                used_parameters.add(value["param"])
            for child in value.values():
                collect_parameter_references(child)
        elif isinstance(value, list):
            for child in value:
                collect_parameter_references(child)

    collect_parameter_references(normalized)

    if has_video:
        support_values = [
            item[0] for item in evidence_pointers if item[2] in {"video", "operator"}
        ]
        operator_affects = [
            affect
            for assumption in assumptions
            if assumption["source"] == "operator_interpretation"
            for affect in assumption["affects"]
        ]
        material_paths = [
            "/entry/confirmation_bars",
            "/entry/cooldown_bars",
            "/exit/initial_stop",
            "/features",
            "/market/bar_duration",
            "/sizing",
            "/schedule",
            "/execution/decision_point",
        ]
        material_paths.extend(
            _pointer(_pointer("/parameters", parameter_id), "value")
            for parameter_id in sorted(used_parameters)
        )
        if entry["long"] is not None:
            material_paths.append("/entry/long")
        if entry["short"] is not None:
            material_paths.append("/entry/short")
        if exit_rules["signal"]["long"] is not None:
            material_paths.append("/exit/signal/long")
        if exit_rules["signal"]["short"] is not None:
            material_paths.append("/exit/signal/short")
        for optional_path, value in (
            ("/exit/take_profit", exit_rules["take_profit"]),
            ("/exit/trailing_stop", exit_rules["trailing_stop"]),
            ("/exit/max_holding_bars", exit_rules["max_holding_bars"]),
        ):
            if value is not None:
                material_paths.append(optional_path)
        for material_path in material_paths:
            covered = any(
                _path_covers(pointer_value, material_path)
                for pointer_value in support_values + operator_affects
            )
            if not covered:
                _fail(
                    "missing_evidence",
                    material_path,
                    "video-derived material rule lacks evidence or operator clarification",
                )

    concrete_parameters = all(item["value"] is not None for item in parameters.values())
    compilation_ready = feature_ready and concrete_parameters and not unresolved
    promotion_ready = compilation_ready and canonical_frozen is not None
    if require_promotable and not promotion_ready:
        if canonical_frozen is None:
            _fail("not_frozen", "/frozen_at", "promotion requires a frozen specification")
        if unresolved:
            _fail(
                "unresolved_items",
                "/unresolved_items",
                "all unresolved items block promotion",
            )
        if not concrete_parameters:
            _fail("unresolved_parameter", "/parameters", "all parameters need concrete values")
        _fail("not_compilable", "/features", "feature warm-up could not be verified")

    for parameter_id in sorted(set(parameters) - used_parameters):
        warnings.append("parameter {} is declared but unused".format(parameter_id))
    for source in provenance["sources"]:
        for evidence in source["evidence"]:
            confidence = evidence["extractor_confidence"]
            if confidence is not None and Decimal(confidence) < Decimal("0.5"):
                warnings.append("evidence {} has extraction confidence below 0.5".format(evidence["evidence_id"]))
    if effective_warmups:
        declared_history = max(item["warmup_bars"] for item in features) if features else 0
        required_history = max(effective_warmups.values())
        if declared_history > required_history * 10:
            warnings.append("declared feature warm-up is unusually larger than required")

    canonical_json = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    spec_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return ValidatedStrategySpecV1(
        canonical_json=canonical_json,
        spec_hash=spec_hash,
        warnings=tuple(sorted(set(warnings))),
        compilation_ready=compilation_ready,
        promotion_ready=promotion_ready,
    )
