"""Strict, synthetic-only scenarios for the offline bracket execution kernel.

This is not a broker transport or a supply/demand strategy adapter. Scenario
intent IDs and fingerprints identify supplied fixtures, not verified signals.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Optional, Tuple

from .paper_execution import (
    BracketIntent, PaperInstrument, PaperLossLimits, PaperPolicy, PaperQuote, run_paper_execution,
)


SCHEMA = "xau-paper-execution-scenario"
MAX_BYTES = 2 * 1024 * 1024
MAX_QUOTES = 10000
MAX_INTENTS = 2000
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,12})?\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z")
_POLICY_DECIMALS = (
    "initial_balance", "risk_fraction", "max_daily_loss_fraction",
    "max_drawdown_fraction", "max_spread", "slippage",
    "commission_per_lot_per_side", "financing_per_lot_per_utc_day",
    "min_reward_risk",
)
_POLICY_INTS = ("max_quote_age_ms", "max_trades_per_day", "max_holding_seconds")
_INSTRUMENT_DECIMALS = (
    "price_tick", "contract_size", "min_lots", "max_lots", "lot_step",
    "minimum_stop_distance",
)


@dataclass(frozen=True)
class PaperScenario:
    name: str
    instrument: PaperInstrument
    policy: PaperPolicy
    quotes: Tuple[PaperQuote, ...]
    intents: Tuple[BracketIntent, ...]
    canonical_json: str
    input_sha256: str
    loss_limits: Optional[PaperLossLimits] = None


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("{} must have exactly these fields: {}".format(label, ", ".join(sorted(keys))))
    return value


def _decimal(value, label):
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ValueError("{} must be a bounded decimal string (no exponent)".format(label))
    return Decimal(value)


def _time(value, label):
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ValueError("{} must be an ISO UTC timestamp ending in Z or +00:00".format(label))
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _text(value, label, limit=128):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("{} must be nonempty text of at most {} characters".format(label, limit))
    if any(ord(char) < 32 for char in value):
        raise ValueError("{} cannot contain control characters".format(label))
    return value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def _constant(value):
    raise ValueError("nonfinite JSON numbers are forbidden: {}".format(value))


def _json_integer(value):
    if len(value) > 10:
        raise ValueError("JSON integer exceeds the scenario size bound")
    return int(value)


def _json_float(value):
    raise ValueError("financial decimal values must be strings, not JSON floating-point numbers")


def parse_paper_scenario(raw: bytes) -> PaperScenario:
    """Parse one exact bounded UTF-8 snapshot; no network or executable rules."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError("scenario must be nonempty UTF-8 bytes of at most {} bytes".format(MAX_BYTES))
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant,
            parse_int=_json_integer, parse_float=_json_float,
        )
    except (UnicodeError, RecursionError) as error:
        raise ValueError("scenario must be bounded UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("scenario must be a JSON object")
    version = value.get("version")
    if value.get("schema") != SCHEMA or type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported paper scenario schema/version")
    keys = ("schema", "version", "data_basis", "name", "instrument", "policy", "quotes", "intents")
    _object(value, keys + (("loss_limits",) if version == 2 else ()), "scenario")
    if value["data_basis"] != "synthetic":
        raise ValueError("paper scenarios accept synthetic fixtures only; they do not verify broker data")
    loss_limits = None
    if version == 2:
        limits = _object(value["loss_limits"], ("max_losses_per_day", "max_weekly_drawdown_fraction"), "loss_limits")
        loss_limits = PaperLossLimits(
            max_losses_per_day=limits["max_losses_per_day"],
            max_weekly_drawdown_fraction=_decimal(limits["max_weekly_drawdown_fraction"],
                                                  "loss_limits.max_weekly_drawdown_fraction"),
        )
    name = _text(value["name"], "name")
    instrument = _object(value["instrument"], ("symbol",) + _INSTRUMENT_DECIMALS, "instrument")
    instrument = PaperInstrument(
        symbol=_text(instrument["symbol"], "instrument.symbol"),
        **{key: _decimal(instrument[key], "instrument." + key) for key in _INSTRUMENT_DECIMALS}
    )
    policy = _object(value["policy"], _POLICY_DECIMALS + _POLICY_INTS + ("force_flat_at_end", "halt_at"), "policy")
    converted_policy = {key: _decimal(policy[key], "policy." + key) for key in _POLICY_DECIMALS}
    for key in _POLICY_INTS:
        if type(policy[key]) is not int or not 0 < policy[key] <= 1000000000:
            raise ValueError("policy.{} must be a bounded positive integer".format(key))
        converted_policy[key] = policy[key]
    if type(policy["force_flat_at_end"]) is not bool:
        raise ValueError("force_flat_at_end must be a boolean")
    converted_policy["force_flat_at_end"] = policy["force_flat_at_end"]
    converted_policy["halt_at"] = None if policy["halt_at"] is None else _time(policy["halt_at"], "halt_at")
    policy = PaperPolicy(**converted_policy)
    if not isinstance(value["quotes"], list) or not 1 <= len(value["quotes"]) <= MAX_QUOTES:
        raise ValueError("quotes must contain between 1 and {} events".format(MAX_QUOTES))
    if not isinstance(value["intents"], list) or len(value["intents"]) > MAX_INTENTS:
        raise ValueError("intents must contain at most {} events".format(MAX_INTENTS))
    quotes = []
    for row in value["quotes"]:
        _object(row, ("timestamp", "available_at", "bid", "ask"), "quote")
        quotes.append(PaperQuote(
            timestamp=_time(row["timestamp"], "quote.timestamp"),
            available_at=_time(row["available_at"], "quote.available_at"),
            bid=_decimal(row["bid"], "quote.bid"), ask=_decimal(row["ask"], "quote.ask"),
        ))
    intents = []
    for row in value["intents"]:
        _object(row, ("intent_id", "side", "created_at", "not_before", "expires_at", "stop_loss", "take_profit", "source_fingerprint"), "intent")
        intents.append(BracketIntent(
            intent_id=_text(row["intent_id"], "intent_id"),
            side=_text(row["side"], "side"),
            created_at=_time(row["created_at"], "intent.created_at"),
            not_before=_time(row["not_before"], "intent.not_before"),
            expires_at=_time(row["expires_at"], "intent.expires_at"),
            stop_loss=_decimal(row["stop_loss"], "intent.stop_loss"),
            take_profit=_decimal(row["take_profit"], "intent.take_profit"),
            source_fingerprint=_text(row["source_fingerprint"], "source_fingerprint"),
        ))
    return PaperScenario(
        name, instrument, policy, tuple(quotes), tuple(intents),
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False),
        hashlib.sha256(raw).hexdigest(),
        loss_limits,
    )


def load_paper_scenario(path: Path) -> PaperScenario:
    with Path(path).open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    return parse_paper_scenario(raw)


def run_paper_scenario(scenario: PaperScenario) -> dict:
    # A Python caller can construct/replace a frozen dataclass. Do not execute
    # settings that disagree with the canonical inputs embedded in its report.
    if not isinstance(scenario, PaperScenario) or not isinstance(scenario.canonical_json, str):
        raise ValueError("scenario must be a parsed PaperScenario")
    checked = parse_paper_scenario(scenario.canonical_json.encode("utf-8"))
    for name in ("name", "instrument", "policy", "quotes", "intents", "canonical_json", "loss_limits"):
        if getattr(scenario, name) != getattr(checked, name):
            raise ValueError("scenario fields do not match the canonical input snapshot: " + name)
    if not isinstance(scenario.input_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", scenario.input_sha256):
        raise ValueError("scenario input_sha256 must be a lowercase SHA-256 digest")
    result = run_paper_execution(scenario.quotes, scenario.intents, scenario.instrument, scenario.policy,
                                 loss_limits=scenario.loss_limits)
    report = result.as_dict()
    report["execution_fingerprint"] = report.pop("fingerprint")
    report["scenario"] = {
        "name": scenario.name,
        "data_basis": "synthetic",
        "input_sha256": scenario.input_sha256,
        "canonical_sha256": hashlib.sha256(scenario.canonical_json.encode("utf-8")).hexdigest(),
        "inputs": json.loads(scenario.canonical_json),
        "strategy_generated": False,
        "source_fingerprints_independently_verified": False,
    }
    report["warning"] = (
        "Scripted synthetic intents and quotes test execution mechanics only. "
        "This is not a supply/demand strategy backtest, an Exness account result, "
        "a live runner, or evidence of profitability. No broker is contacted."
    )
    report["fingerprint"] = hashlib.sha256(json.dumps(
        report, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return report


def write_new_json(path: Path, value: dict) -> None:
    """Stage complete bytes and publish exclusively, even against dangling links."""
    payload = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".paper-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, str(path))
    finally:
        os.unlink(temporary)


def synthetic_execution_scenario() -> dict:
    """Hand-scripted engine exercise, deliberately unrelated to trading signals."""
    def time(second):
        return "2026-01-05T12:00:{:02d}Z".format(second)

    def quote(second, bid, ask):
        return {"timestamp": time(second), "available_at": time(second), "bid": bid, "ask": ask}

    def intent(name, side, created, stop, target, expiry=55):
        return {
            "intent_id": name, "side": side, "created_at": time(created),
            "not_before": time(created), "expires_at": time(expiry),
            "stop_loss": stop, "take_profit": target,
            "source_fingerprint": hashlib.sha256(("scripted-fixture:" + name).encode("ascii")).hexdigest(),
        }

    return {
        "schema": SCHEMA, "version": 1, "data_basis": "synthetic",
        "name": "Bracket mechanics only: target, gap stop, and rejected entries",
        "instrument": {
            "symbol": "XAU_USD", "price_tick": "0.01", "contract_size": "100",
            "min_lots": "0.01", "max_lots": "1", "lot_step": "0.01", "minimum_stop_distance": "0.10",
        },
        "policy": {
            "initial_balance": "500", "risk_fraction": "0.01", "max_daily_loss_fraction": "0.04",
            "max_drawdown_fraction": "0.10", "max_spread": "0.50", "slippage": "0.10",
            "commission_per_lot_per_side": "2", "financing_per_lot_per_utc_day": "1",
            "min_reward_risk": "2", "max_quote_age_ms": 1000, "max_trades_per_day": 3,
            "max_holding_seconds": 3600, "force_flat_at_end": True, "halt_at": None,
        },
        "quotes": [
            quote(0, "2000.00", "2000.20"), quote(2, "2000.00", "2000.20"),
            quote(4, "2005.40", "2005.60"), quote(6, "2005.00", "2005.20"),
            quote(8, "2008.00", "2008.20"), quote(10, "2000.00", "2002.00"),
            quote(12, "2000.00", "2000.20"), quote(20, "2000.00", "2000.20"),
        ],
        "intents": [
            intent("fixture-long-target", "buy", 1, "1998.00", "2005.30"),
            intent("fixture-short-gap-stop", "sell", 5, "2007.00", "1999.90"),
            intent("fixture-wide-spread", "buy", 9, "1998.00", "2007.00"),
            intent("fixture-conflict-buy", "buy", 11, "1998.00", "2007.00"),
            intent("fixture-conflict-sell", "sell", 11, "2003.00", "1993.00"),
            intent("fixture-expired", "buy", 13, "1998.00", "2007.00", expiry=15),
        ],
    }
