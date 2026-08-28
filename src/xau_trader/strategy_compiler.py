"""Deterministic lowering and pure rule evaluation for validated StrategySpec v1 data.

The compiled plan is diagnostic-only. It is deliberately not adapted to the current P&L
backtester because that interface cannot express v1 stops, targets, sizing, quote event order,
or broker state. No function in this module evaluates source text or dynamically imports code.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .domain import QuoteBar
from .strategy_schema import (
    StrategySpecValidationError,
    ValidatedStrategySpecV1,
    validate_strategy_spec_v1,
)


COMPILER_VERSION = "strategy-compiler-v1"
FEATURE_PROFILE_ID = "builtin-close-features-v1"
_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)
FEATURE_IMPLEMENTATION_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_FEATURE_MANIFEST = {
    "profile_id": FEATURE_PROFILE_ID,
    "implementation_source_sha256": FEATURE_IMPLEMENTATION_HASH,
    "numeric_type": "decimal128-style-local-context-prec28-half-even",
    "input_conversion": "Decimal(str(value))",
    "mid_bar_mapping": "arithmetic mean of matching bid/ask aggregate fields",
    "features": {
        "sma": {"inputs": 1, "params": ["period"], "seed": "window_mean"},
        "ema": {"inputs": 1, "params": ["period"], "seed": "sma", "alpha": "2/(period+1)"},
        "rsi": {"inputs": 1, "params": ["period"], "algorithm": "wilder", "flat": "50"},
        "atr": {
            "inputs": ["bar.high", "bar.low", "bar.close"],
            "params": ["period"],
            "algorithm": "wilder_true_range",
        },
        "rolling_high": {"inputs": 1, "params": ["period"]},
        "rolling_low": {"inputs": 1, "params": ["period"]},
    },
}
FEATURE_MANIFEST_JSON = json.dumps(
    _FEATURE_MANIFEST,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
)
FEATURE_MANIFEST_HASH = hashlib.sha256(FEATURE_MANIFEST_JSON.encode("utf-8")).hexdigest()


class StrategyCompilationError(ValueError):
    def __init__(self, code: str, pointer: str, message: str) -> None:
        self.code = code
        self.pointer = pointer
        self.message = message
        super().__init__("{} at {}: {}".format(code, pointer, message))


class TruthValue(str, Enum):
    FALSE = "false"
    TRUE = "true"
    UNKNOWN = "unknown"


class SignalAction(str, Enum):
    NO_SIGNAL = "no_signal"
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CompiledOperand:
    kind: str
    value: Any

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in {
            "ref",
            "param",
            "decimal",
            "integer",
            "boolean",
            "enum",
        }:
            raise ValueError("compiled operand kind is invalid")
        if self.kind in {"ref", "param", "enum"} and type(self.value) is not str:
            raise ValueError("compiled string operand must contain an immutable string")
        if self.kind == "decimal" and (
            type(self.value) is not Decimal or not self.value.is_finite()
        ):
            raise ValueError("compiled decimal operand must contain a finite Decimal")
        if self.kind == "integer" and type(self.value) is not int:
            raise ValueError("compiled integer operand must contain an integer")
        if self.kind == "boolean" and type(self.value) is not bool:
            raise ValueError("compiled boolean operand must contain a boolean")


@dataclass(frozen=True)
class CompiledCondition:
    op: str
    args: Tuple[Any, ...]

    def __post_init__(self) -> None:
        if type(self.op) is not str or self.op not in {
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
        }:
            raise ValueError("compiled condition operator is invalid")
        if type(self.args) is not tuple:
            raise ValueError("compiled condition args must be an immutable tuple")
        if self.op in {"all", "any"}:
            valid = bool(self.args) and all(type(item) is CompiledCondition for item in self.args)
        elif self.op == "not":
            valid = len(self.args) == 1 and type(self.args[0]) is CompiledCondition
        else:
            valid = len(self.args) == 2 and all(type(item) is CompiledOperand for item in self.args)
        if not valid:
            raise ValueError("compiled condition args do not match the operator")


@dataclass(frozen=True)
class CompiledFeature:
    feature_id: str
    kind: str
    inputs: Tuple[str, ...]
    period: int
    declared_warmup_bars: int

    def __post_init__(self) -> None:
        if type(self.feature_id) is not str or type(self.kind) is not str:
            raise ValueError("compiled feature identifiers must be strings")
        if self.kind not in {"sma", "ema", "rsi", "atr", "rolling_high", "rolling_low"}:
            raise ValueError("compiled feature kind is invalid")
        if type(self.inputs) is not tuple or not all(type(item) is str for item in self.inputs):
            raise ValueError("compiled feature inputs must be an immutable string tuple")
        expected_inputs = 3 if self.kind == "atr" else 1
        if len(self.inputs) != expected_inputs:
            raise ValueError("compiled feature input arity is invalid")
        if type(self.period) is not int or not 1 <= self.period <= 10_000:
            raise ValueError("compiled feature period is outside the supported range")
        if type(self.declared_warmup_bars) is not int or self.declared_warmup_bars < 1:
            raise ValueError("compiled feature warm-up must be a positive integer")


@dataclass(frozen=True)
class PositionContext:
    """Explicit state needed by rule evaluation; it is never inferred from future fills."""

    side: str = "flat"
    unrealized_r: Optional[Decimal] = None
    bars_since_last_entry: Optional[int] = None
    bars_in_position: Optional[int] = None
    as_of: Optional[datetime] = None

    def __post_init__(self) -> None:
        if type(self.side) is not str or self.side not in {"flat", "long", "short"}:
            raise ValueError("side must be flat, long, or short")
        for name in ("bars_since_last_entry", "bars_in_position"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("{} must be a non-negative integer or None".format(name))
        if self.unrealized_r is not None:
            if type(self.unrealized_r) is not Decimal or not self.unrealized_r.is_finite():
                raise ValueError("unrealized_r must be a finite Decimal or None")
        if self.as_of is not None:
            if (
                type(self.as_of) is not datetime
                or self.as_of.tzinfo is None
                or self.as_of.utcoffset() is None
            ):
                raise ValueError("as_of must be a timezone-aware datetime or None")
            try:
                normalized_as_of = self.as_of.astimezone(timezone.utc)
            except (OverflowError, ValueError):
                raise ValueError("as_of must be a valid timezone-aware datetime")
            object.__setattr__(self, "as_of", normalized_as_of)
        if self.side == "flat":
            if self.bars_in_position is not None or self.unrealized_r is not None:
                raise ValueError("flat position context cannot contain open-position state")
        elif self.bars_in_position is None:
            raise ValueError("positioned context requires bars_in_position")
        if (
            self.side != "flat"
            or self.bars_since_last_entry is not None
            or self.bars_in_position is not None
            or self.unrealized_r is not None
        ) and self.as_of is None:
            raise ValueError("stateful position context requires an as_of timestamp")


@dataclass(frozen=True)
class RuleEvaluation:
    action: SignalAction
    reason: str
    long_state: TruthValue
    short_state: TruthValue
    input_sha256: str
    decision_time: datetime


@dataclass(frozen=True)
class CompiledStrategyPlan:
    """Immutable, hash-bound diagnostic rule plan produced by the closed compiler."""

    spec_hash: str
    strategy_id: str
    revision: int
    name: str
    bar_duration: str
    parameters: Tuple[Tuple[str, str, Any], ...]
    features: Tuple[CompiledFeature, ...]
    entry_long: Optional[CompiledCondition]
    entry_short: Optional[CompiledCondition]
    exit_long: Optional[CompiledCondition]
    exit_short: Optional[CompiledCondition]
    confirmation_bars: int
    cooldown_bars: int
    schedule_days: Tuple[str, ...]
    schedule_windows: Tuple[Tuple[str, str], ...]
    exclude_dates: Tuple[str, ...]
    close_before_weekend: Tuple[bool, Optional[str]]
    max_holding_bars: Optional[int]
    close_on_data_stale: bool
    canonical_plan_json: str
    plan_hash: str
    compiler_version: str = COMPILER_VERSION
    feature_manifest_hash: str = FEATURE_MANIFEST_HASH
    profile_id: str = FEATURE_PROFILE_ID
    diagnostic_only: bool = True

    def __post_init__(self) -> None:
        immutable_string_fields = (
            self.spec_hash,
            self.strategy_id,
            self.name,
            self.bar_duration,
            self.canonical_plan_json,
            self.plan_hash,
            self.compiler_version,
            self.feature_manifest_hash,
            self.profile_id,
        )
        if not all(type(value) is str for value in immutable_string_fields):
            raise ValueError("compiled string fields must be immutable strings")
        if (
            len(self.spec_hash) != 64
            or len(self.plan_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.spec_hash)
            or any(character not in "0123456789abcdef" for character in self.plan_hash)
        ):
            raise ValueError("compiled hashes must be SHA-256 digests")
        if (
            self.compiler_version != COMPILER_VERSION
            or self.feature_manifest_hash != FEATURE_MANIFEST_HASH
            or self.profile_id != FEATURE_PROFILE_ID
        ):
            raise ValueError("compiled plan is incompatible with the current evaluator runtime")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("compiled revision must be a positive integer")
        if type(self.parameters) is not tuple:
            raise ValueError("compiled parameters must be an immutable tuple")
        for parameter in self.parameters:
            if type(parameter) is not tuple or len(parameter) != 3:
                raise ValueError("compiled parameter rows must be immutable triples")
            parameter_id, parameter_type, parameter_value = parameter
            if type(parameter_id) is not str or type(parameter_type) is not str or parameter_type not in {
                "integer",
                "decimal",
                "boolean",
                "enum",
            }:
                raise ValueError("compiled parameter metadata is invalid")
            expected_type = {
                "integer": int,
                "decimal": Decimal,
                "boolean": bool,
                "enum": str,
            }[parameter_type]
            if type(parameter_value) is not expected_type:
                raise ValueError("compiled parameter value type is invalid")
            if type(parameter_value) is Decimal and not parameter_value.is_finite():
                raise ValueError("compiled decimal parameter must be finite")
        if type(self.features) is not tuple or not all(
            type(feature) is CompiledFeature for feature in self.features
        ):
            raise ValueError("compiled features must be an immutable feature tuple")
        if any(
            condition is not None and type(condition) is not CompiledCondition
            for condition in (self.entry_long, self.entry_short, self.exit_long, self.exit_short)
        ):
            raise ValueError("compiled conditions must use validated immutable nodes")
        if type(self.confirmation_bars) is not int or self.confirmation_bars < 1:
            raise ValueError("compiled confirmation_bars is invalid")
        if type(self.cooldown_bars) is not int or self.cooldown_bars < 0:
            raise ValueError("compiled cooldown_bars is invalid")
        if type(self.schedule_days) is not tuple or not all(
            type(value) is str for value in self.schedule_days
        ):
            raise ValueError("compiled schedule days must be an immutable tuple")
        if type(self.schedule_windows) is not tuple or not all(
            type(window) is tuple
            and len(window) == 2
            and all(type(value) is str for value in window)
            for window in self.schedule_windows
        ):
            raise ValueError("compiled schedule windows must be immutable pairs")
        if type(self.exclude_dates) is not tuple or not all(
            type(value) is str for value in self.exclude_dates
        ):
            raise ValueError("compiled excluded dates must be an immutable tuple")
        if (
            type(self.close_before_weekend) is not tuple
            or len(self.close_before_weekend) != 2
            or type(self.close_before_weekend[0]) is not bool
            or (
                self.close_before_weekend[1] is not None
                and type(self.close_before_weekend[1]) is not str
            )
        ):
            raise ValueError("compiled weekend close must be an immutable policy pair")
        if self.max_holding_bars is not None and (
            type(self.max_holding_bars) is not int or self.max_holding_bars < 1
        ):
            raise ValueError("compiled max_holding_bars must be a positive integer or None")
        if type(self.close_on_data_stale) is not bool or type(self.diagnostic_only) is not bool:
            raise ValueError("compiled policy flags must be booleans")
        try:
            payload = json.loads(self.canonical_plan_json)
        except json.JSONDecodeError as exc:
            raise ValueError("canonical_plan_json is invalid") from exc
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        if canonical != self.canonical_plan_json:
            raise ValueError("canonical_plan_json is not canonical")
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != self.plan_hash:
            raise ValueError("plan_hash does not match canonical_plan_json")
        if (
            payload.get("spec_hash") != self.spec_hash
            or payload.get("compiler_version") != self.compiler_version
            or payload.get("feature_manifest_hash") != self.feature_manifest_hash
            or payload.get("profile_id") != self.profile_id
            or payload.get("diagnostic_only") is not True
        ):
            raise ValueError("canonical plan bindings do not match compiled fields")
        if not self.diagnostic_only:
            raise ValueError("compiled plan must remain diagnostic-only")
        expected_schedule = {
            "days": list(self.schedule_days),
            "windows": [
                {"start_utc": start, "end_utc": end}
                for start, end in self.schedule_windows
            ],
            "exclude_dates": list(self.exclude_dates),
            "close_before_weekend": {
                "enabled": self.close_before_weekend[0],
                "cutoff_utc": self.close_before_weekend[1],
            },
        }
        if (
            payload.get("strategy_id") != self.strategy_id
            or payload.get("revision") != self.revision
            or payload.get("name") != self.name
            or payload.get("bar_duration") != self.bar_duration
            or payload.get("parameters") != _json_plan_value(self.parameters)
            or payload.get("features") != _json_plan_value(self.features)
            or payload.get("entry")
            != {
                "long": _json_plan_value(self.entry_long),
                "short": _json_plan_value(self.entry_short),
                "confirmation_bars": self.confirmation_bars,
                "cooldown_bars": self.cooldown_bars,
            }
            or payload.get("exit_signal")
            != {
                "long": _json_plan_value(self.exit_long),
                "short": _json_plan_value(self.exit_short),
            }
            or payload.get("schedule") != expected_schedule
            or payload.get("max_holding_bars") != self.max_holding_bars
            or payload.get("bound_policies", {}).get("risk", {}).get("close_on_data_stale")
            != self.close_on_data_stale
        ):
            raise ValueError("compiled evaluator fields do not match the hash-bound plan")

    @property
    def required_warmup_bars(self) -> int:
        return max((feature.declared_warmup_bars for feature in self.features), default=1)

    def evaluate(
        self,
        bars: Sequence[QuoteBar],
        context: Optional[PositionContext] = None,
        index: Optional[int] = None,
    ) -> RuleEvaluation:
        """Evaluate signal rules at one prefix; price-triggered stop/target fills are out of scope."""

        self.__post_init__()  # recheck persisted/unpickled plans against this runtime
        actual_context = PositionContext() if context is None else context
        if type(actual_context) is not PositionContext:
            raise StrategyCompilationError(
                "invalid_context", "/context", "context must be a PositionContext"
            )
        return _evaluate_plan(self, bars, actual_context, index)

    def feature_snapshot(
        self,
        bars: Sequence[QuoteBar],
        index: Optional[int] = None,
    ) -> Tuple[Tuple[str, Optional[Decimal]], ...]:
        """Return an immutable diagnostic feature snapshot at one history prefix."""

        self.__post_init__()  # recheck persisted/unpickled plans against this runtime
        prefix, _ = _validated_prefix(bars, index)
        values = _feature_series(self, prefix)
        return tuple((feature.feature_id, values[feature.feature_id][-1]) for feature in self.features)


def _compile_operand(value: Mapping[str, Any]) -> CompiledOperand:
    key = next(iter(value))
    raw = value[key]
    if key == "decimal":
        return CompiledOperand(key, Decimal(raw))
    return CompiledOperand(key, raw)


def _compile_condition(value: Optional[Mapping[str, Any]]) -> Optional[CompiledCondition]:
    if value is None:
        return None
    operation = value["op"]
    if operation in {"all", "any", "not"}:
        return CompiledCondition(
            operation,
            tuple(_compile_condition(item) for item in value["args"]),
        )
    return CompiledCondition(operation, tuple(_compile_operand(item) for item in value["args"]))


def _condition_uses_reference(
    condition: Optional[CompiledCondition],
    reference: str,
) -> bool:
    if condition is None:
        return False
    if condition.op in {"all", "any", "not"}:
        return any(_condition_uses_reference(child, reference) for child in condition.args)
    return any(
        operand.kind == "ref" and operand.value == reference
        for operand in condition.args
    )


def _condition_uses_position(condition: Optional[CompiledCondition]) -> bool:
    return any(
        _condition_uses_reference(condition, reference)
        for reference in ("position.side", "position.unrealized_r")
    )


def _topological_features(features: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    by_id = {feature["id"]: feature for feature in features}
    dependencies: Dict[str, Set[str]] = {
        feature_id: {
            item["ref"].split(".", 1)[1]
            for item in feature["inputs"]
            if item["ref"].startswith("features.")
        }
        for feature_id, feature in by_id.items()
    }
    ready = sorted(feature_id for feature_id, deps in dependencies.items() if not deps)
    result: List[Mapping[str, Any]] = []
    while ready:
        feature_id = ready.pop(0)
        result.append(by_id[feature_id])
        for candidate in sorted(dependencies):
            if feature_id in dependencies[candidate]:
                dependencies[candidate].remove(feature_id)
                if not dependencies[candidate] and candidate not in {
                    item["id"] for item in result
                } and candidate not in ready:
                    ready.append(candidate)
                    ready.sort()
    if len(result) != len(features):
        raise StrategyCompilationError("feature_cycle", "/features", "feature graph is cyclic")
    return result


def _resolved_period(feature: Mapping[str, Any], parameters: Mapping[str, Any]) -> int:
    period = feature["params"]["period"]
    if "integer" in period:
        return period["integer"]
    value = parameters[period["param"]]["value"]
    if value is None:
        raise StrategyCompilationError(
            "unresolved_parameter",
            "/parameters/{}".format(period["param"]),
            "feature period is unresolved",
        )
    return value


def _json_plan_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        return [_json_plan_value(item) for item in value]
    if isinstance(value, CompiledFeature):
        return {
            "id": value.feature_id,
            "kind": value.kind,
            "inputs": list(value.inputs),
            "period": value.period,
            "declared_warmup_bars": value.declared_warmup_bars,
        }
    if isinstance(value, CompiledOperand):
        return {"kind": value.kind, "value": _json_plan_value(value.value)}
    if isinstance(value, CompiledCondition):
        return {"op": value.op, "args": [_json_plan_value(item) for item in value.args]}
    return value


def compile_strategy_spec_v1(
    validated: ValidatedStrategySpecV1,
    *,
    require_frozen: bool = True,
) -> CompiledStrategyPlan:
    """Lower a validated document into data-only nodes; never generated Python code."""

    if not isinstance(validated, ValidatedStrategySpecV1):
        raise StrategyCompilationError(
            "unvalidated_input", "/", "compiler accepts only ValidatedStrategySpecV1 artifacts"
        )
    try:
        revalidated = validate_strategy_spec_v1(validated.as_dict())
    except StrategySpecValidationError as exc:
        raise StrategyCompilationError(
            "invalid_validated_artifact", exc.pointer, exc.message
        ) from exc
    if revalidated.spec_hash != validated.spec_hash:
        raise StrategyCompilationError(
            "invalid_validated_artifact", "/", "canonical spec hash changed during revalidation"
        )
    if (
        revalidated.compilation_ready != validated.compilation_ready
        or revalidated.promotion_ready != validated.promotion_ready
    ):
        raise StrategyCompilationError(
            "invalid_validated_artifact", "/", "readiness flags do not match canonical content"
        )
    validated = revalidated
    document = validated.as_dict()
    if not validated.compilation_ready:
        raise StrategyCompilationError(
            "not_compilation_ready",
            "/",
            "unresolved parameters or items prevent compilation",
        )
    if require_frozen and not validated.promotion_ready:
        if document["frozen_at"] is None:
            raise StrategyCompilationError(
                "not_frozen", "/frozen_at", "compiler requires a frozen spec"
            )
        raise StrategyCompilationError(
            "not_promotion_ready", "/", "compiler requires a promotion-ready spec"
        )
    parameters = document["parameters"]
    compiled_parameters: List[Tuple[str, str, Any]] = []
    for parameter_id in sorted(parameters):
        parameter = parameters[parameter_id]
        value = parameter["value"]
        if parameter["type"] == "decimal":
            value = Decimal(value)
        compiled_parameters.append((parameter_id, parameter["type"], value))

    compiled_features = tuple(
        CompiledFeature(
            feature_id=feature["id"],
            kind=feature["kind"],
            inputs=tuple(item["ref"] for item in feature["inputs"]),
            period=_resolved_period(feature, parameters),
            declared_warmup_bars=feature["warmup_bars"],
        )
        for feature in _topological_features(document["features"])
    )
    entry_long = _compile_condition(document["entry"]["long"])
    entry_short = _compile_condition(document["entry"]["short"])
    exit_long = _compile_condition(document["exit"]["signal"]["long"])
    exit_short = _compile_condition(document["exit"]["signal"]["short"])
    schedule = document["schedule"]
    close = schedule["close_before_weekend"]

    payload = {
        "compiler_version": COMPILER_VERSION,
        "feature_manifest_hash": FEATURE_MANIFEST_HASH,
        "profile_id": FEATURE_PROFILE_ID,
        "spec_hash": validated.spec_hash,
        "strategy_id": document["strategy_id"],
        "revision": document["revision"],
        "name": document["name"],
        "bar_duration": document["market"]["bar_duration"],
        "parameters": _json_plan_value(tuple(compiled_parameters)),
        "features": _json_plan_value(compiled_features),
        "entry": {
            "long": _json_plan_value(entry_long),
            "short": _json_plan_value(entry_short),
            "confirmation_bars": document["entry"]["confirmation_bars"],
            "cooldown_bars": document["entry"]["cooldown_bars"],
        },
        "exit_signal": {
            "long": _json_plan_value(exit_long),
            "short": _json_plan_value(exit_short),
        },
        "schedule": schedule,
        "max_holding_bars": document["exit"]["max_holding_bars"],
        "bound_policies": {
            "initial_stop": document["exit"]["initial_stop"],
            "take_profit": document["exit"]["take_profit"],
            "trailing_stop": document["exit"]["trailing_stop"],
            "sizing": document["sizing"],
            "execution": document["execution"],
            "risk": document["risk"],
        },
        "diagnostic_only": True,
    }
    canonical_plan_json = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    plan_hash = hashlib.sha256(canonical_plan_json.encode("utf-8")).hexdigest()
    return CompiledStrategyPlan(
        spec_hash=validated.spec_hash,
        strategy_id=document["strategy_id"],
        revision=document["revision"],
        name=document["name"],
        bar_duration=document["market"]["bar_duration"],
        parameters=tuple(compiled_parameters),
        features=compiled_features,
        entry_long=entry_long,
        entry_short=entry_short,
        exit_long=exit_long,
        exit_short=exit_short,
        confirmation_bars=document["entry"]["confirmation_bars"],
        cooldown_bars=document["entry"]["cooldown_bars"],
        schedule_days=tuple(schedule["days"]),
        schedule_windows=tuple(
            (window["start_utc"], window["end_utc"]) for window in schedule["windows"]
        ),
        exclude_dates=tuple(schedule["exclude_dates"]),
        close_before_weekend=(close["enabled"], close["cutoff_utc"]),
        max_holding_bars=document["exit"]["max_holding_bars"],
        close_on_data_stale=document["risk"]["close_on_data_stale"],
        canonical_plan_json=canonical_plan_json,
        plan_hash=plan_hash,
    )


def _duration(value: str) -> timedelta:
    mapping = {
        "PT5M": timedelta(minutes=5),
        "PT15M": timedelta(minutes=15),
        "PT30M": timedelta(minutes=30),
        "PT1H": timedelta(hours=1),
        "PT4H": timedelta(hours=4),
    }
    return mapping[value]


def _validated_prefix(
    raw_bars: Sequence[QuoteBar],
    raw_index: Optional[int],
) -> Tuple[Tuple[QuoteBar, ...], int]:
    try:
        length = len(raw_bars)
    except TypeError as exc:
        raise StrategyCompilationError(
            "invalid_data", "/bars", "bars must be an indexable finite sequence"
        ) from exc
    if length == 0:
        raise StrategyCompilationError("missing_data", "/bars", "at least one bar is required")
    index = length - 1 if raw_index is None else raw_index
    if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= length:
        raise StrategyCompilationError("invalid_index", "/index", "evaluation index is out of range")
    try:
        prefix = tuple(raw_bars[item] for item in range(index + 1))
    except (IndexError, KeyError, TypeError) as exc:
        raise StrategyCompilationError(
            "invalid_data", "/bars", "bars must support stable integer indexing"
        ) from exc
    if any(not isinstance(bar, QuoteBar) for bar in prefix):
        raise StrategyCompilationError("invalid_data", "/bars", "all prefix values must be QuoteBar")
    for previous, current in zip(prefix, prefix[1:]):
        if current.timestamp <= previous.timestamp or current.start_time < previous.timestamp:
            raise StrategyCompilationError(
                "invalid_data_order", "/bars", "bars must be strictly ordered and non-overlapping"
            )
    return prefix, index


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _bar_value(bar: QuoteBar, field_name: str) -> Decimal:
    with localcontext(_DECIMAL_CONTEXT):
        bid = getattr(bar, "bid_{}".format(field_name))
        ask = getattr(bar, "ask_{}".format(field_name))
        return (_decimal(bid) + _decimal(ask)) / Decimal(2)


def _bar_is_usable(bar: QuoteBar, expected_duration: timedelta) -> bool:
    return bar.available_at == bar.timestamp and bar.timestamp - bar.start_time == expected_duration


def _contiguous(previous: QuoteBar, current: QuoteBar) -> bool:
    return current.start_time == previous.timestamp


def _usable_contiguous_suffix(
    bars: Sequence[QuoteBar],
    expected_duration: timedelta,
) -> int:
    count = 0
    for index in range(len(bars) - 1, -1, -1):
        if not _bar_is_usable(bars[index], expected_duration):
            break
        if index < len(bars) - 1 and not _contiguous(bars[index], bars[index + 1]):
            break
        count += 1
    return count


def _input_series(
    reference: str,
    bars: Sequence[QuoteBar],
    feature_values: Mapping[str, Sequence[Optional[Decimal]]],
    expected_duration: timedelta,
) -> List[Optional[Decimal]]:
    if reference.startswith("features."):
        return list(feature_values[reference.split(".", 1)[1]])
    field_name = reference.split(".", 1)[1]
    return [
        _bar_value(bar, field_name) if _bar_is_usable(bar, expected_duration) else None
        for bar in bars
    ]


def _simple_window(
    values: Sequence[Optional[Decimal]],
    bars: Sequence[QuoteBar],
    period: int,
    operation: str,
) -> List[Optional[Decimal]]:
    output: List[Optional[Decimal]] = [None] * len(values)
    buffer: List[Decimal] = []
    for index, value in enumerate(values):
        if value is None or (index and not _contiguous(bars[index - 1], bars[index])):
            buffer = []
        if value is None:
            continue
        buffer.append(value)
        if len(buffer) > period:
            buffer.pop(0)
        if len(buffer) == period:
            if operation == "sma":
                output[index] = sum(buffer, Decimal(0)) / Decimal(period)
            elif operation == "rolling_high":
                output[index] = max(buffer)
            else:
                output[index] = min(buffer)
    return output


def _ema(
    values: Sequence[Optional[Decimal]],
    bars: Sequence[QuoteBar],
    period: int,
) -> List[Optional[Decimal]]:
    output: List[Optional[Decimal]] = [None] * len(values)
    buffer: List[Decimal] = []
    previous_ema: Optional[Decimal] = None
    alpha = Decimal(2) / Decimal(period + 1)
    for index, value in enumerate(values):
        if value is None or (index and not _contiguous(bars[index - 1], bars[index])):
            buffer = []
            previous_ema = None
        if value is None:
            continue
        if previous_ema is None:
            buffer.append(value)
            if len(buffer) == period:
                previous_ema = sum(buffer, Decimal(0)) / Decimal(period)
                output[index] = previous_ema
        else:
            previous_ema = alpha * value + (Decimal(1) - alpha) * previous_ema
            output[index] = previous_ema
    return output


def _rsi(
    values: Sequence[Optional[Decimal]],
    bars: Sequence[QuoteBar],
    period: int,
) -> List[Optional[Decimal]]:
    output: List[Optional[Decimal]] = [None] * len(values)
    previous_value: Optional[Decimal] = None
    gains: List[Decimal] = []
    losses: List[Decimal] = []
    average_gain: Optional[Decimal] = None
    average_loss: Optional[Decimal] = None
    for index, value in enumerate(values):
        if value is None or (index and not _contiguous(bars[index - 1], bars[index])):
            previous_value = None
            gains = []
            losses = []
            average_gain = None
            average_loss = None
        if value is None:
            continue
        if previous_value is None:
            previous_value = value
            continue
        change = value - previous_value
        previous_value = value
        gain = max(change, Decimal(0))
        loss = max(-change, Decimal(0))
        if average_gain is None or average_loss is None:
            gains.append(gain)
            losses.append(loss)
            if len(gains) < period:
                continue
            average_gain = sum(gains, Decimal(0)) / Decimal(period)
            average_loss = sum(losses, Decimal(0)) / Decimal(period)
        else:
            average_gain = (average_gain * Decimal(period - 1) + gain) / Decimal(period)
            average_loss = (average_loss * Decimal(period - 1) + loss) / Decimal(period)
        if average_gain == 0 and average_loss == 0:
            output[index] = Decimal(50)
        elif average_loss == 0:
            output[index] = Decimal(100)
        else:
            relative_strength = average_gain / average_loss
            output[index] = Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)
    return output


def _atr(
    high_values: Sequence[Optional[Decimal]],
    low_values: Sequence[Optional[Decimal]],
    close_values: Sequence[Optional[Decimal]],
    bars: Sequence[QuoteBar],
    period: int,
) -> List[Optional[Decimal]]:
    output: List[Optional[Decimal]] = [None] * len(bars)
    previous_close: Optional[Decimal] = None
    true_ranges: List[Decimal] = []
    average: Optional[Decimal] = None
    for index, (high, low, close) in enumerate(zip(high_values, low_values, close_values)):
        if (
            high is None
            or low is None
            or close is None
            or (index and not _contiguous(bars[index - 1], bars[index]))
        ):
            previous_close = None
            true_ranges = []
            average = None
        if high is None or low is None or close is None:
            continue
        if previous_close is None:
            previous_close = close
            continue
        true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
        previous_close = close
        if average is None:
            true_ranges.append(true_range)
            if len(true_ranges) < period:
                continue
            average = sum(true_ranges, Decimal(0)) / Decimal(period)
        else:
            average = (average * Decimal(period - 1) + true_range) / Decimal(period)
        output[index] = average
    return output


def _feature_series(
    plan: CompiledStrategyPlan,
    bars: Sequence[QuoteBar],
) -> Dict[str, List[Optional[Decimal]]]:
    expected_duration = _duration(plan.bar_duration)
    values: Dict[str, List[Optional[Decimal]]] = {}
    with localcontext(_DECIMAL_CONTEXT):
        for feature in plan.features:
            inputs = [
                _input_series(reference, bars, values, expected_duration)
                for reference in feature.inputs
            ]
            if feature.kind in {"sma", "rolling_high", "rolling_low"}:
                values[feature.feature_id] = _simple_window(
                    inputs[0], bars, feature.period, feature.kind
                )
            elif feature.kind == "ema":
                values[feature.feature_id] = _ema(inputs[0], bars, feature.period)
            elif feature.kind == "rsi":
                values[feature.feature_id] = _rsi(inputs[0], bars, feature.period)
            elif feature.kind == "atr":
                values[feature.feature_id] = _atr(
                    inputs[0], inputs[1], inputs[2], bars, feature.period
                )
            else:  # validated and manifest-bound, retained as a fail-closed assertion
                raise StrategyCompilationError(
                    "unsupported_feature", "/features", "compiled feature is unsupported"
                )
    return values


def _parameter_map(plan: CompiledStrategyPlan) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for parameter_id, parameter_type, value in plan.parameters:
        if parameter_type == "integer":
            result[parameter_id] = Decimal(value)
        else:
            result[parameter_id] = value
    return result


def _operand_value(
    operand: CompiledOperand,
    bar_index: int,
    decision_index: int,
    bars: Sequence[QuoteBar],
    feature_values: Mapping[str, Sequence[Optional[Decimal]]],
    parameters: Mapping[str, Any],
    context: PositionContext,
    expected_duration: timedelta,
) -> Any:
    if operand.kind == "decimal":
        return operand.value
    if operand.kind == "integer":
        return Decimal(operand.value)
    if operand.kind in {"boolean", "enum"}:
        return operand.value
    if operand.kind == "param":
        return parameters[operand.value]
    if operand.kind != "ref":
        return None
    reference = operand.value
    bar = bars[bar_index]
    if reference.startswith("bar."):
        if not _bar_is_usable(bar, expected_duration):
            return None
        return _bar_value(bar, reference.split(".", 1)[1])
    if reference == "quote.bid":
        return _decimal(bar.bid_close) if _bar_is_usable(bar, expected_duration) else None
    if reference == "quote.ask":
        return _decimal(bar.ask_close) if _bar_is_usable(bar, expected_duration) else None
    if reference.startswith("features."):
        return feature_values[reference.split(".", 1)[1]][bar_index]
    if bar_index != decision_index:
        return None
    if reference == "position.side":
        return context.side
    if reference == "position.unrealized_r":
        return context.unrealized_r
    return None


def _truth(value: bool) -> TruthValue:
    return TruthValue.TRUE if value else TruthValue.FALSE


def _evaluate_condition(
    condition: Optional[CompiledCondition],
    bar_index: int,
    decision_index: int,
    bars: Sequence[QuoteBar],
    feature_values: Mapping[str, Sequence[Optional[Decimal]]],
    parameters: Mapping[str, Any],
    context: PositionContext,
    expected_duration: timedelta,
) -> TruthValue:
    if condition is None:
        return TruthValue.FALSE
    if condition.op in {"all", "any", "not"}:
        child_values = [
            _evaluate_condition(
                child,
                bar_index,
                decision_index,
                bars,
                feature_values,
                parameters,
                context,
                expected_duration,
            )
            for child in condition.args
        ]
        if condition.op == "not":
            if child_values[0] == TruthValue.UNKNOWN:
                return TruthValue.UNKNOWN
            return _truth(child_values[0] == TruthValue.FALSE)
        if condition.op == "all":
            if TruthValue.FALSE in child_values:
                return TruthValue.FALSE
            if TruthValue.UNKNOWN in child_values:
                return TruthValue.UNKNOWN
            return TruthValue.TRUE
        if TruthValue.TRUE in child_values:
            return TruthValue.TRUE
        if TruthValue.UNKNOWN in child_values:
            return TruthValue.UNKNOWN
        return TruthValue.FALSE

    left = condition.args[0]
    right = condition.args[1]
    if condition.op in {"crosses_above", "crosses_below"}:
        if bar_index < 1 or not _contiguous(bars[bar_index - 1], bars[bar_index]):
            return TruthValue.UNKNOWN
        current_left = _operand_value(
            left, bar_index, decision_index, bars, feature_values, parameters, context, expected_duration
        )
        current_right = _operand_value(
            right, bar_index, decision_index, bars, feature_values, parameters, context, expected_duration
        )
        prior_left = _operand_value(
            left, bar_index - 1, decision_index, bars, feature_values, parameters, context, expected_duration
        )
        prior_right = _operand_value(
            right, bar_index - 1, decision_index, bars, feature_values, parameters, context, expected_duration
        )
        if any(value is None for value in (current_left, current_right, prior_left, prior_right)):
            return TruthValue.UNKNOWN
        if condition.op == "crosses_above":
            return _truth(prior_left <= prior_right and current_left > current_right)
        return _truth(prior_left >= prior_right and current_left < current_right)

    left_value = _operand_value(
        left, bar_index, decision_index, bars, feature_values, parameters, context, expected_duration
    )
    right_value = _operand_value(
        right, bar_index, decision_index, bars, feature_values, parameters, context, expected_duration
    )
    if left_value is None or right_value is None:
        return TruthValue.UNKNOWN
    operations = {
        "gt": lambda: left_value > right_value,
        "gte": lambda: left_value >= right_value,
        "lt": lambda: left_value < right_value,
        "lte": lambda: left_value <= right_value,
        "eq": lambda: left_value == right_value,
        "neq": lambda: left_value != right_value,
    }
    return _truth(operations[condition.op]())


def _parse_clock(value: str) -> time:
    return time(int(value[:2]), int(value[3:]))


def _entry_schedule_open(plan: CompiledStrategyPlan, decision_time: datetime) -> bool:
    normalized = decision_time.astimezone(timezone.utc)
    weekday = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")[normalized.weekday()]
    if weekday not in plan.schedule_days or normalized.date().isoformat() in plan.exclude_dates:
        return False
    clock = normalized.time().replace(tzinfo=None)
    if not any(_parse_clock(start) <= clock < _parse_clock(end) for start, end in plan.schedule_windows):
        return False
    close_enabled, cutoff = plan.close_before_weekend
    if close_enabled and weekday == "FRI" and cutoff is not None and clock >= _parse_clock(cutoff):
        return False
    return True


def _weekend_close_due(plan: CompiledStrategyPlan, decision_time: datetime) -> bool:
    enabled, cutoff = plan.close_before_weekend
    if not enabled or cutoff is None:
        return False
    normalized = decision_time.astimezone(timezone.utc)
    return (
        normalized.weekday() == 4
        and normalized.time().replace(tzinfo=None) >= _parse_clock(cutoff)
    )


def _input_hash(
    plan: CompiledStrategyPlan,
    bars: Sequence[QuoteBar],
    context: PositionContext,
) -> str:
    payload = {
        "plan_hash": plan.plan_hash,
        "bars": [
            {
                "start_time": bar.start_time.isoformat(),
                "end_time": bar.timestamp.isoformat(),
                "available_at": bar.available_at.isoformat(),
                "availability_basis": bar.availability_basis.value,
                "bid": [
                    str(bar.bid_open),
                    str(bar.bid_high),
                    str(bar.bid_low),
                    str(bar.bid_close),
                ],
                "ask": [
                    str(bar.ask_open),
                    str(bar.ask_high),
                    str(bar.ask_low),
                    str(bar.ask_close),
                ],
                "volume": None if bar.volume is None else str(bar.volume),
            }
            for bar in bars
        ],
        "position": {
            "side": context.side,
            "unrealized_r": (
                None if context.unrealized_r is None else format(context.unrealized_r, "f")
            ),
            "bars_since_last_entry": context.bars_since_last_entry,
            "bars_in_position": context.bars_in_position,
            "as_of": None if context.as_of is None else context.as_of.isoformat(),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evaluate_plan(
    plan: CompiledStrategyPlan,
    raw_bars: Sequence[QuoteBar],
    context: PositionContext,
    raw_index: Optional[int],
) -> RuleEvaluation:
    prefix, index = _validated_prefix(raw_bars, raw_index)
    decision_time = prefix[-1].timestamp
    context_is_used = (
        context.side != "flat"
        or context.bars_since_last_entry is not None
        or context.bars_in_position is not None
        or context.unrealized_r is not None
        or any(
            _condition_uses_position(condition)
            for condition in (plan.entry_long, plan.entry_short, plan.exit_long, plan.exit_short)
        )
    )
    if context_is_used and context.as_of is None:
        raise StrategyCompilationError(
            "missing_context_time",
            "/context/as_of",
            "position-dependent evaluation requires context.as_of",
        )
    if context.as_of is not None and context.as_of != decision_time.astimezone(timezone.utc):
        raise StrategyCompilationError(
            "context_time_mismatch",
            "/context/as_of",
            "context.as_of must equal the evaluated bar decision time",
        )
    active_exit = (
        plan.exit_long
        if context.side == "long"
        else plan.exit_short
        if context.side == "short"
        else None
    )
    if (
        _condition_uses_reference(active_exit, "position.unrealized_r")
        and context.unrealized_r is None
    ):
        raise StrategyCompilationError(
            "missing_context_value",
            "/context/unrealized_r",
            "the active exit rule requires context.unrealized_r",
        )
    input_hash = _input_hash(plan, prefix, context)
    expected_duration = _duration(plan.bar_duration)
    feature_values = _feature_series(plan, prefix)
    parameters = _parameter_map(plan)

    def condition_state(condition: Optional[CompiledCondition], at_index: int) -> TruthValue:
        return _evaluate_condition(
            condition,
            at_index,
            index,
            prefix,
            feature_values,
            parameters,
            context,
            expected_duration,
        )

    current_long = condition_state(plan.entry_long, index)
    current_short = condition_state(plan.entry_short, index)

    if not _bar_is_usable(prefix[index], expected_duration):
        return RuleEvaluation(
            SignalAction.NO_SIGNAL,
            "decision bar is unavailable at close or has the wrong duration",
            current_long,
            current_short,
            input_hash,
            decision_time,
        )

    if context.side == "long":
        if _weekend_close_due(plan, decision_time):
            return RuleEvaluation(
                SignalAction.EXIT_LONG,
                "Friday close-before-weekend cutoff reached",
                current_long,
                current_short,
                input_hash,
                decision_time,
            )
        exit_state = condition_state(plan.exit_long, index)
        if exit_state == TruthValue.TRUE:
            return RuleEvaluation(
                SignalAction.EXIT_LONG,
                "long exit condition is true",
                current_long,
                current_short,
                input_hash,
                decision_time,
            )
        if (
            plan.max_holding_bars is not None
            and context.bars_in_position is not None
            and context.bars_in_position >= plan.max_holding_bars
        ):
            return RuleEvaluation(
                SignalAction.EXIT_LONG,
                "maximum holding period reached",
                current_long,
                current_short,
                input_hash,
                decision_time,
            )
        return RuleEvaluation(
            SignalAction.NO_SIGNAL,
            "entries are disabled while a long position remains open",
            current_long,
            current_short,
            input_hash,
            decision_time,
        )
    if context.side == "short":
        if _weekend_close_due(plan, decision_time):
            return RuleEvaluation(
                SignalAction.EXIT_SHORT,
                "Friday close-before-weekend cutoff reached",
                current_long,
                current_short,
                input_hash,
                decision_time,
            )
        exit_state = condition_state(plan.exit_short, index)
        if exit_state == TruthValue.TRUE:
            return RuleEvaluation(
                SignalAction.EXIT_SHORT,
                "short exit condition is true",
                current_long,
                current_short,
                input_hash,
                decision_time,
            )
        if (
            plan.max_holding_bars is not None
            and context.bars_in_position is not None
            and context.bars_in_position >= plan.max_holding_bars
        ):
            return RuleEvaluation(
                SignalAction.EXIT_SHORT,
                "maximum holding period reached",
                current_long,
                current_short,
                input_hash,
                decision_time,
            )
        return RuleEvaluation(
            SignalAction.NO_SIGNAL,
            "entries are disabled while a short position remains open",
            current_long,
            current_short,
            input_hash,
            decision_time,
        )

    if (
        len(prefix) < plan.required_warmup_bars
        or _usable_contiguous_suffix(prefix, expected_duration) < plan.required_warmup_bars
    ):
        return RuleEvaluation(
            SignalAction.NO_SIGNAL,
            "feature warm-up is incomplete",
            current_long,
            current_short,
            input_hash,
            decision_time,
        )
    if any(
        feature_values[feature.feature_id][index] is None
        for feature in plan.features
    ):
        return RuleEvaluation(
            SignalAction.NO_SIGNAL,
            "feature warm-up is incomplete after a continuity reset",
            current_long,
            current_short,
            input_hash,
            decision_time,
        )
    if not _entry_schedule_open(plan, decision_time):
        return RuleEvaluation(
            SignalAction.NO_SIGNAL,
            "entry schedule is closed",
            current_long,
            current_short,
            input_hash,
            decision_time,
        )
    if (
        context.bars_since_last_entry is not None
        and context.bars_since_last_entry <= plan.cooldown_bars
    ):
        return RuleEvaluation(
            SignalAction.NO_SIGNAL,
            "entry cooldown is active",
            current_long,
            current_short,
            input_hash,
            decision_time,
        )

    confirmation_start = index - plan.confirmation_bars + 1
    if confirmation_start < 0:
        confirmed_long = TruthValue.UNKNOWN
        confirmed_short = TruthValue.UNKNOWN
    elif any(
        not _bar_is_usable(prefix[item], expected_duration)
        for item in range(confirmation_start, index + 1)
    ) or any(
        not _contiguous(prefix[item - 1], prefix[item])
        for item in range(confirmation_start + 1, index + 1)
    ):
        confirmed_long = (
            TruthValue.UNKNOWN if plan.entry_long is not None else TruthValue.FALSE
        )
        confirmed_short = (
            TruthValue.UNKNOWN if plan.entry_short is not None else TruthValue.FALSE
        )
    else:
        long_states = [condition_state(plan.entry_long, item) for item in range(confirmation_start, index + 1)]
        short_states = [condition_state(plan.entry_short, item) for item in range(confirmation_start, index + 1)]
        confirmed_long = (
            TruthValue.TRUE
            if plan.entry_long is not None and all(item == TruthValue.TRUE for item in long_states)
            else TruthValue.UNKNOWN
            if any(item == TruthValue.UNKNOWN for item in long_states)
            else TruthValue.FALSE
        )
        confirmed_short = (
            TruthValue.TRUE
            if plan.entry_short is not None and all(item == TruthValue.TRUE for item in short_states)
            else TruthValue.UNKNOWN
            if any(item == TruthValue.UNKNOWN for item in short_states)
            else TruthValue.FALSE
        )
    if confirmed_long == TruthValue.TRUE and confirmed_short == TruthValue.TRUE:
        return RuleEvaluation(
            SignalAction.CONFLICT,
            "long and short entry conditions are simultaneously true",
            confirmed_long,
            confirmed_short,
            input_hash,
            decision_time,
        )
    if confirmed_long == TruthValue.TRUE:
        return RuleEvaluation(
            SignalAction.ENTER_LONG,
            "confirmed long entry condition",
            confirmed_long,
            confirmed_short,
            input_hash,
            decision_time,
        )
    if confirmed_short == TruthValue.TRUE:
        return RuleEvaluation(
            SignalAction.ENTER_SHORT,
            "confirmed short entry condition",
            confirmed_long,
            confirmed_short,
            input_hash,
            decision_time,
        )
    return RuleEvaluation(
        SignalAction.NO_SIGNAL,
        "entry conditions are false or unknown",
        confirmed_long,
        confirmed_short,
        input_hash,
        decision_time,
    )
