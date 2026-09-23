"""Connect recomputed strategy evidence to a coherent synthetic quote replay.

This is an engineering integration, not a real-data backtest or broker runner.
The caller supplies every quote and all contract/cost assumptions. No price,
signal, timestamp, or profitable trade is manufactured by this adapter.
"""

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from enum import Enum
import hashlib
import json
from typing import Optional, Sequence

from .diagnostic_replay import DiagnosticReplayResult
from .paper_execution import (
    ExactOpenIntent, PaperInstrument, PaperLossLimits, PaperPolicy, PaperQuote,
    _arithmetic_context, run_paper_execution,
)
from .structural_exits import StructuralExitPolicy, plan_structural_exits


ADAPTER_VERSION = "synthetic-strategy-paper-adapter-v2"
SEMANTICS = (
    "synthetic_only_recomputed_candidate_and_structural_evidence",
    "complete_supplied_quote_tape_reconstructs_every_bid_ask_m15_ohlc",
    "quotes_belong_to_half_open_bar_and_arrive_no_later_than_bar_availability",
    "supplied_intrabar_order_is_a_fixture_assumption_not_inferred_market_history",
    "signal_created_at_original_candidate_availability_never_backdated",
    "first_quote_at_or_after_proposed_boundary_must_have_exact_boundary_source_time",
    "boundary_quote_receipt_strictly_after_signal_otherwise_reject_once",
    "receipt_expiry_at_proposed_boundary_plus_15_minutes_and_separate_quote_age_limit",
    "nearest_frozen_directional_target_at_adverse_rounded_entry_no_farther_rr_fallback",
    "optional_explicit_loss_limits_total_net_losing_closes_per_utc_day_and_utc_week_equity_peak",
    "configured_loss_limits_are_provisional_not_source_risk_parity_or_live_readiness",
)


def _json(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _json(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _canonical(value):
    return json.dumps(_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StrategyPaperResult:
    _payload_json: str

    def as_dict(self) -> dict:
        return json.loads(self._payload_json)

    @property
    def fingerprint(self) -> str:
        return self.as_dict()["fingerprint"]


def _validate_coherent_tape(bars, tape, instrument):
    """No OHLC-path inference: validate the complete caller-supplied path."""
    if not tape:
        raise ValueError("a complete synthetic quote tape is required")
    groups = [[] for _ in bars]
    bar_index = 0
    with localcontext(_arithmetic_context()):
        for index, quote in enumerate(tape):
            if not isinstance(quote, PaperQuote):
                raise ValueError("quotes must contain PaperQuote values")
            if index and (quote.timestamp < tape[index - 1].timestamp
                          or quote.available_at < tape[index - 1].available_at):
                raise ValueError("quote source and availability timestamps must be nondecreasing")
            if any(value % instrument.price_tick for value in (quote.bid, quote.ask)):
                raise ValueError("quote prices must be on the instrument price grid")
            while bar_index < len(bars) and quote.timestamp >= bars[bar_index].timestamp:
                bar_index += 1
            if bar_index == len(bars) or quote.timestamp < bars[bar_index].start_time:
                raise ValueError("quote falls outside the supplied M15 bars or inside a closure")
            bar = bars[bar_index]
            if quote.available_at > bar.available_at:
                raise ValueError("quote received after its completed bar became available")
            groups[bar_index].append(quote)
        for index, (bar, group) in enumerate(zip(bars, groups)):
            if not group:
                raise ValueError("missing quote group for M15 bar {}".format(index))
            for side in ("bid", "ask"):
                prices = [getattr(quote, side) for quote in group]
                observed = (prices[0], max(prices), min(prices), prices[-1])
                expected = tuple(Decimal(str(getattr(bar, side + "_" + part)))
                                 for part in ("open", "high", "low", "close"))
                if any(value % instrument.price_tick for value in expected):
                    raise ValueError("M15 OHLC prices must already be on the instrument price grid")
                if observed != expected:
                    raise ValueError("quote tape does not reconstruct {} OHLC for M15 bar {}".format(side, index))


def run_strategy_paper_replay(
    replay: DiagnosticReplayResult, *, quotes: Sequence[PaperQuote],
    instrument: PaperInstrument, execution_policy: PaperPolicy,
    structural_policy: StructuralExitPolicy,
    loss_limits: Optional[PaperLossLimits] = None,
) -> StrategyPaperResult:
    """Derive one-shot intents, preserving every original candidate in a trace.

    Public input is the diagnostic replay, not user-authored plans or intents.
    The structural planner revalidates it by recomputing upstream evidence.
    Complete tape/bar consistency is necessary but does not establish that any
    quote occurred in a real market or could have been traded at receipt time.
    """
    if not isinstance(instrument, PaperInstrument) or not isinstance(execution_policy, PaperPolicy):
        raise ValueError("instrument and execution_policy must be validated paper dataclasses")
    if loss_limits is not None and not isinstance(loss_limits, PaperLossLimits):
        raise ValueError("loss_limits must be PaperLossLimits or None")
    structural = plan_structural_exits(replay, policy=structural_policy, price_tick=instrument.price_tick)
    tape = tuple(quotes)
    _validate_coherent_tape(replay.m15_bars, tape, instrument)
    intents, rows, plans = [], [], {}
    for decision in structural.decisions:
        plan = decision.plan
        row = dict(candidate_key=decision.candidate_key,
                   candidate_fingerprint=decision.candidate_fingerprint,
                   action=decision.action, available_at=decision.available_at,
                   structural_status=decision.status, structural_reason=decision.reason,
                   plan_fingerprint=None, intent_id=None,
                   execution_status="skipped", execution_reason=decision.reason,
                   selected_target=None)
        if plan is not None:
            intent = ExactOpenIntent(
                intent_id="strategy-" + plan.fingerprint,
                side=plan.action.value, created_at=plan.decision_available_at,
                not_before=plan.proposed_entry_at,
                expires_at=plan.proposed_entry_at + timedelta(minutes=15),
                stop_loss=plan.stop_loss, source_fingerprint=plan.fingerprint,
                target_levels=tuple(sorted({level.price for level in plan.target_levels})),
            )
            intents.append(intent)
            plans[intent.intent_id] = plan
            row.update(plan_fingerprint=plan.fingerprint, intent_id=intent.intent_id)
        rows.append(row)
    intents.sort(key=lambda intent: (intent.created_at, intent.intent_id))
    execution = run_paper_execution(tape, intents, instrument, execution_policy,
                                    loss_limits=loss_limits).as_dict()
    events_by_intent = {}
    for event in execution["events"]:
        if "intent_id" in event:
            events_by_intent.setdefault(event["intent_id"], []).append(event)
    unobserved = {item["intent_id"]: item for item in execution["not_observed_intents"]}
    statuses = {"intent_accepted": "open", "intent_rejected": "rejected",
                "intent_cancelled": "cancelled", "position_closed": "closed"}
    for row in rows:
        intent_id = row["intent_id"]
        if intent_id is None:
            continue
        if intent_id in unobserved:
            row.update(execution_status="not_observed", execution_reason=unobserved[intent_id]["reason"])
            continue
        history = events_by_intent.get(intent_id, ())
        outcomes = [event for event in history if event["kind"] in statuses]
        if not outcomes:
            raise ValueError("execution did not account for a generated intent")
        final = outcomes[-1]
        row.update(execution_status=statuses[final["kind"]], execution_reason=final["reason"])
        accepted = next((event for event in history if event["kind"] == "intent_accepted"), None)
        if accepted is not None:
            # The planner retains duplicate-price pivot evidence in stable order.
            price = Decimal(accepted["take_profit"])
            row["selected_target"] = next(level for level in plans[intent_id].target_levels if level.price == price)
    counts = {name: sum(row["execution_status"] == name for row in rows)
              for name in ("skipped", "rejected", "cancelled", "not_observed", "open", "closed")}
    counts.update(decisions=len(rows), pre_roll=structural.pre_roll_count,
                  generated_intents=len(intents), quotes=len(tape), trades=execution["counts"]["trades"])
    payload = dict(
        status="synthetic_strategy_paper_replay_complete", adapter_version=ADAPTER_VERSION,
        data_basis="synthetic", strategy_generated=True, engine_connected=True,
        broker_connected=False, real_orders_submitted=0, promotion_eligible=False,
        hypothesis_unapproved=True, source_risk_policy_complete=False,
        loss_limits_configured=loss_limits is not None,
        loss_limits=execution["loss_limits"],
        semantics=SEMANTICS, replay_fingerprint=replay.fingerprint,
        dataset_fingerprint=replay.dataset_fingerprint,
        input_bars_fingerprint=replay.input_bars_fingerprint,
        quote_tape_fingerprint=_digest(tape), structural=structural.as_dict(),
        intents=intents, decisions=rows, counts=counts, execution=execution,
        input_fingerprint=_digest(dict(adapter_version=ADAPTER_VERSION, semantics=SEMANTICS,
                                       replay=replay.fingerprint, structural=structural.fingerprint,
                                       quotes=tape, instrument=instrument, execution_policy=execution_policy,
                                       loss_limits=execution["loss_limits"])),
        warnings=[
            "Supplied synthetic candles and intrabar quotes test integration, not historical strategy profitability.",
            "Exact-source-boundary receipt ordering is a provisional simulation contract, not a live fill guarantee.",
            ("Loss-count and weekly drawdown guards use explicit provisional UTC semantics; source risk parity is unapproved."
             if loss_limits is not None else
             "Daily loss-count and weekly drawdown guards are disabled because no loss-limit policy was supplied."),
            "Risk state is local to this replay and is not a persistent account-wide or live risk service.",
            "Instrument and cost specifications are caller-supplied fixture assumptions, not broker verification.",
            "No broker, account access, persistent runner, real market data, or real-money orders.",
        ],
    )
    payload["fingerprint"] = _digest(payload)
    return StrategyPaperResult(_canonical(payload))
