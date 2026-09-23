"""Deterministic offline bracket-order simulation, with no broker interface.

This is a quote-event engineering harness, not a strategy backtest or a live
execution service. Every price, cost and contract specification is supplied by
the caller. USD linear P&L is modeled; margin, liquidity and latency are not.
"""

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timezone
from decimal import (
    Context, Decimal, DivisionByZero, InvalidOperation, Overflow,
    ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext,
)
import hashlib
import json
from typing import Any, Dict, Optional, Sequence, Union


ENGINE_VERSION = "offline-bracket-quote-engine-v3"
LOSS_LIMITS_VERSION = "provisional-utc-paper-loss-limits-v1"
ZERO = Decimal("0")
ONE = Decimal("1")


def _arithmetic_context() -> Context:
    # Neither caller rounding nor caller traps may affect an auditable run.
    return Context(prec=160, rounding=ROUND_HALF_EVEN, Emin=-999999,
                   Emax=999999, capitals=1, clamp=0, flags=[],
                   traps=[InvalidOperation, DivisionByZero, Overflow])


def _number(value: Decimal, name: str, *, zero: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("{} must be a finite Decimal".format(name))
    if value < ZERO or (not zero and value == ZERO):
        raise ValueError("{} must be {}".format(name, "non-negative" if zero else "positive"))
    # Bounds keep malicious/extreme Decimal exponents out of arithmetic/JSON.
    if len(value.as_tuple().digits) > 40 or abs(value.as_tuple().exponent) > 40:
        raise ValueError("{} exceeds supported Decimal precision/range".format(name))


def _time(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("{} must be a timezone-aware datetime".format(name))
    return value.astimezone(timezone.utc)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("{} must be a nonempty string of at most 256 characters".format(name))
    return value.strip()


def _integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 10**12:
        raise ValueError("{} must be a positive bounded integer".format(name))


def _on_grid(value: Decimal, step: Decimal) -> bool:
    with localcontext(_arithmetic_context()):
        return value % step == ZERO


@dataclass(frozen=True)
class PaperInstrument:
    symbol: str
    price_tick: Decimal
    contract_size: Decimal
    min_lots: Decimal
    max_lots: Decimal
    lot_step: Decimal
    minimum_stop_distance: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        if self.symbol != "XAU_USD":
            raise ValueError("only normalized XAU_USD with USD linear P&L is supported")
        for name in ("price_tick", "contract_size", "min_lots", "max_lots", "lot_step"):
            _number(getattr(self, name), name)
        _number(self.minimum_stop_distance, "minimum_stop_distance", zero=True)
        if self.min_lots > self.max_lots:
            raise ValueError("min_lots cannot exceed max_lots")
        if not _on_grid(self.min_lots, self.lot_step) or not _on_grid(self.max_lots, self.lot_step):
            raise ValueError("lot bounds must be multiples of lot_step")


@dataclass(frozen=True)
class PaperPolicy:
    initial_balance: Decimal
    risk_fraction: Decimal
    max_daily_loss_fraction: Decimal
    max_drawdown_fraction: Decimal
    max_spread: Decimal
    slippage: Decimal
    commission_per_lot_per_side: Decimal
    financing_per_lot_per_utc_day: Decimal
    max_quote_age_ms: int
    max_trades_per_day: int
    max_holding_seconds: int
    force_flat_at_end: bool
    min_reward_risk: Decimal
    halt_at: Optional[datetime]

    def __post_init__(self) -> None:
        for name in ("initial_balance", "max_spread", "min_reward_risk"):
            _number(getattr(self, name), name)
        for name in ("risk_fraction", "max_daily_loss_fraction", "max_drawdown_fraction"):
            _number(getattr(self, name), name)
            if getattr(self, name) >= ONE:
                raise ValueError("{} must be less than one".format(name))
        for name in ("slippage", "commission_per_lot_per_side", "financing_per_lot_per_utc_day"):
            _number(getattr(self, name), name, zero=True)
        for name in ("max_quote_age_ms", "max_trades_per_day", "max_holding_seconds"):
            _integer(getattr(self, name), name)
        if not isinstance(self.force_flat_at_end, bool):
            raise ValueError("force_flat_at_end must be boolean")
        if self.halt_at is not None:
            object.__setattr__(self, "halt_at", _time(self.halt_at, "halt_at"))


@dataclass(frozen=True)
class PaperLossLimits:
    """Explicit provisional research rules, with no persisted/live state.

    A day counts total net-losing closes, not consecutive losing trades. The
    weekly reference is liquidation-equity high water, not starting balance.
    These choices resolve research ambiguities, not broker/strategy approval.
    """

    max_losses_per_day: int
    max_weekly_drawdown_fraction: Decimal

    def __post_init__(self) -> None:
        _integer(self.max_losses_per_day, "max_losses_per_day")
        _number(self.max_weekly_drawdown_fraction, "max_weekly_drawdown_fraction")
        if self.max_weekly_drawdown_fraction >= ONE:
            raise ValueError("max_weekly_drawdown_fraction must be less than one")

    def as_dict(self) -> Dict[str, Any]:
        payload = dict(
            enabled=True, version=LOSS_LIMITS_VERSION, provisional=True,
            state_persisted=False, max_losses_per_day=self.max_losses_per_day,
            max_weekly_drawdown_fraction=self.max_weekly_drawdown_fraction,
            semantics=dict(
                loss="closed-trade net_pnl strictly below zero after both commissions and accrued financing; total not consecutive; winners and breakeven do not reset count",
                loss_date="UTC exit quote receipt date; reset on first received quote of a new UTC date",
                week="UTC ISO week-year and week number; Monday boundary, including ISO year rollover",
                weekly_equity="liquidation equity including unrealized P&L, adverse exit slippage and exit commission, and accrued financing; resting-target cap applies",
                weekly_peak="initial balance in first observed week; later observed weeks start from previous observed liquidation equity before current financing and mark; then update high water from current mark; no invented midnight quote",
                weekly_halt="at or beyond weekly drawdown threshold; latched until first received quote in a different ISO week, even if equity recovers or daily controls reset",
                evaluation="before exits/entries, after closes and entry costs, and after final forced close; stale marks may halt but cannot fill",
                priority="operator/global halt, daily equity loss, daily loss count, weekly drawdown, then bracket/holding exits",
                sizing="cap modeled stop risk by remaining weekly high-water allowance as well as existing limits; gaps and future financing may exceed caps",
                persistence="none; each offline run starts fresh and cannot serve as account-wide live protection",
            ),
        )
        payload["fingerprint"] = _digest(payload)
        return _json_value(payload)

    @property
    def fingerprint(self) -> str:
        return self.as_dict()["fingerprint"]


@dataclass(frozen=True)
class PaperQuote:
    timestamp: datetime
    available_at: datetime
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        for name in ("timestamp", "available_at"):
            object.__setattr__(self, name, _time(getattr(self, name), name))
        if self.timestamp > self.available_at:
            raise ValueError("quote timestamp cannot exceed available_at")
        _number(self.bid, "bid")
        _number(self.ask, "ask")
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")


@dataclass(frozen=True)
class BracketIntent:
    intent_id: str
    side: str
    created_at: datetime
    not_before: datetime
    expires_at: datetime
    stop_loss: Decimal
    take_profit: Decimal
    source_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _text(self.intent_id, "intent_id"))
        if not isinstance(self.side, str) or self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        for name in ("created_at", "not_before", "expires_at"):
            object.__setattr__(self, name, _time(getattr(self, name), name))
        if not self.created_at <= self.not_before < self.expires_at:
            raise ValueError("intent requires created_at <= not_before < expires_at")
        for name in ("stop_loss", "take_profit"):
            _number(getattr(self, name), name)
        if not isinstance(self.source_fingerprint, str):
            raise ValueError("source_fingerprint must be SHA-256 hexadecimal")
        digest = self.source_fingerprint.lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("source_fingerprint must be SHA-256 hexadecimal")
        object.__setattr__(self, "source_fingerprint", digest)


@dataclass(frozen=True)
class ExactOpenIntent:
    """One boundary quote attempt with decision-time frozen target levels.

    A quote at ``not_before`` must be received strictly after ``created_at``.
    Equal-time ordering cannot be inferred; it is rejected rather than retried.
    The nearest directional target is selected only from the supplied frozen
    levels after the executable, adverse-rounded entry is known. These timing
    rules are a research contract, not an assertion of broker/source parity.
    """

    intent_id: str
    side: str
    created_at: datetime
    not_before: datetime
    expires_at: datetime
    stop_loss: Decimal
    source_fingerprint: str
    target_levels: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _text(self.intent_id, "intent_id"))
        if not isinstance(self.side, str) or self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        for name in ("created_at", "not_before", "expires_at"):
            object.__setattr__(self, name, _time(getattr(self, name), name))
        if not self.created_at <= self.not_before < self.expires_at:
            raise ValueError("intent requires created_at <= not_before < expires_at")
        _number(self.stop_loss, "stop_loss")
        if not isinstance(self.source_fingerprint, str):
            raise ValueError("source_fingerprint must be SHA-256 hexadecimal")
        digest = self.source_fingerprint.lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("source_fingerprint must be SHA-256 hexadecimal")
        object.__setattr__(self, "source_fingerprint", digest)
        if not isinstance(self.target_levels, tuple) or not self.target_levels:
            raise ValueError("target_levels must be a nonempty immutable tuple")
        for level in self.target_levels:
            _number(level, "target_levels")
        if any(left >= right for left, right in zip(self.target_levels, self.target_levels[1:])):
            raise ValueError("target_levels must be unique and strictly ascending")


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PaperExecutionResult:
    """Immutable serialized result; callers receive independent mapping copies."""

    _payload_json: str

    def as_dict(self) -> Dict[str, Any]:
        return json.loads(self._payload_json)

    @property
    def fingerprint(self) -> str:
        return self.as_dict()["fingerprint"]


def run_paper_execution(
    quotes: Sequence[PaperQuote],
    intents: Sequence[Union[BracketIntent, ExactOpenIntent]],
    instrument: PaperInstrument,
    policy: PaperPolicy,
    *,
    loss_limits: Optional[PaperLossLimits] = None,
) -> PaperExecutionResult:
    """Consume an ordered quote tape and one-shot, causally available intents.

    No connection, submission, persistence, retry, or strategy evaluation occurs.
    A stale mark can report exposure, but can never execute a simulated fill.
    """
    if not isinstance(instrument, PaperInstrument) or not isinstance(policy, PaperPolicy):
        raise ValueError("instrument and policy must be validated paper dataclasses")
    if loss_limits is not None and not isinstance(loss_limits, PaperLossLimits):
        raise ValueError("loss_limits must be PaperLossLimits or None")
    tape, requests = tuple(quotes), tuple(intents)
    if not tape:
        raise ValueError("at least one quote is required")
    for index, quote in enumerate(tape):
        if not isinstance(quote, PaperQuote):
            raise ValueError("quotes must contain PaperQuote values")
        if index and (quote.timestamp < tape[index - 1].timestamp or quote.available_at < tape[index - 1].available_at):
            raise ValueError("quote source and availability timestamps must be nondecreasing")
        if not all(_on_grid(value, instrument.price_tick) for value in (quote.bid, quote.ask)):
            raise ValueError("observed quote prices must be on the instrument price grid")
    ids = set()
    for index, intent in enumerate(requests):
        if not isinstance(intent, (BracketIntent, ExactOpenIntent)):
            raise ValueError("intents must contain BracketIntent or ExactOpenIntent values")
        if intent.intent_id in ids:
            raise ValueError("intent ids must be unique")
        ids.add(intent.intent_id)
        if index and intent.created_at < requests[index - 1].created_at:
            raise ValueError("intents must be ordered by created_at")
        targets = intent.target_levels if isinstance(intent, ExactOpenIntent) else (intent.take_profit,)
        if not all(_on_grid(value, instrument.price_tick) for value in (intent.stop_loss,) + targets):
            raise ValueError("stop and target prices must be on the instrument price grid")
    with localcontext(_arithmetic_context()):
        return _run(tape, requests, instrument, policy, loss_limits)


def _run(tape, requests, instrument, policy, loss_limits):
    balance = policy.initial_balance
    peak = balance
    day_start = balance
    day = None
    daily_halt = False
    global_halt = None
    trades_today = 0
    position = None
    handled = set()
    fills, trades, events, curve = [], [], [], []
    total_commission = ZERO
    total_financing = ZERO
    max_drawdown = ZERO
    limits_payload = (loss_limits.as_dict() if loss_limits is not None else
                      dict(enabled=False, version=LOSS_LIMITS_VERSION,
                           reason="not_configured", provisional=True, state_persisted=False))
    losses_today = 0
    loss_count_halt = False
    week_id = None
    week_peak = balance
    weekly_halt = False
    last_observed_equity = balance
    maximum_weekly_drawdown = ZERO

    def loss_state():
        if loss_limits is None:
            return {}
        return dict(losses_today=losses_today, loss_count_halt=loss_count_halt,
                    week_id=week_id, week_peak=week_peak, weekly_halt=weekly_halt,
                    loss_limits_fingerprint=limits_payload["fingerprint"])

    def event(quote, kind, reason, **detail):
        events.append(dict(timestamp=quote.available_at, quote_timestamp=quote.timestamp,
                           kind=kind, reason=reason, **loss_state(), **detail))

    def adverse(value, buying):
        rounding = ROUND_CEILING if buying else ROUND_FLOOR
        return (value / instrument.price_tick).to_integral_value(rounding=rounding) * instrument.price_tick

    def market_exit(quote, side):
        value = quote.bid - policy.slippage if side == "buy" else quote.ask + policy.slippage
        return adverse(value, side == "sell")

    def marked(quote):
        if position is None:
            return balance
        price = market_exit(quote, position["side"])
        # A resting limit cannot realize a favorable gap beyond its target.
        # Do not let such a gap create a fictitious high-water equity mark.
        if ((position["side"] == "buy" and quote.bid >= position["take_profit"])
                or (position["side"] == "sell" and quote.ask <= position["take_profit"])):
            price = position["take_profit"]
        direction = ONE if position["side"] == "buy" else -ONE
        return balance + (price - position["entry_price"]) * direction * position["lots"] * instrument.contract_size - position["lots"] * policy.commission_per_lot_per_side

    def close(quote, reason, limit_price=None):
        nonlocal balance, position, total_commission, losses_today
        # A safety exit may not invent favorable execution past a standing
        # take-profit that this same quote has already triggered.
        if ((position["side"] == "buy" and quote.bid >= position["take_profit"])
                or (position["side"] == "sell" and quote.ask <= position["take_profit"])):
            limit_price = position["take_profit"]
        price = market_exit(quote, position["side"]) if limit_price is None else limit_price
        if price <= ZERO:
            raise ValueError("configured adverse exit fill is non-positive")
        direction = ONE if position["side"] == "buy" else -ONE
        commission = position["lots"] * policy.commission_per_lot_per_side
        gross = (price - position["entry_price"]) * direction * position["lots"] * instrument.contract_size
        balance += gross - commission
        total_commission += commission
        fills.append(dict(intent_id=position["intent_id"], timestamp=quote.available_at,
                          quote_timestamp=quote.timestamp, action="sell" if position["side"] == "buy" else "buy",
                          phase="exit", reason=reason, price=price, lots=position["lots"], commission=commission,
                          pricing="standing_target_limit" if limit_price is not None else "adverse_market_quote"))
        trades.append(dict(intent_id=position["intent_id"], side=position["side"],
                           source_fingerprint=position["source_fingerprint"],
                           entered_at=position["entered_at"], exited_at=quote.available_at,
                           lots=position["lots"], entry_price=position["entry_price"], exit_price=price,
                           stop_loss=position["stop_loss"], take_profit=position["take_profit"],
                           exit_reason=reason, gross_pnl=gross,
                           commission=position["entry_commission"] + commission,
                           financing=position["financing"],
                           net_pnl=gross - position["entry_commission"] - commission - position["financing"],
                           modeled_stop_risk=position["modeled_stop_risk"]))
        if loss_limits is not None:
            is_loss = trades[-1]["net_pnl"] < ZERO
            losses_today += int(is_loss)
            trades[-1].update(counted_daily_loss=is_loss,
                              loss_receipt_day=quote.available_at.date().isoformat())
        event(quote, "position_closed", reason, intent_id=position["intent_id"])
        position = None
        if loss_limits is not None:
            check_guards(quote, True)

    def reject(quote, intent, reason):
        handled.add(intent.intent_id)
        event(quote, "intent_rejected", reason, intent_id=intent.intent_id)

    def latch(quote, reason):
        nonlocal global_halt
        if global_halt is None:
            global_halt = reason
            event(quote, "global_halt", reason)

    def check_guards(quote, fresh):
        nonlocal peak, max_drawdown, daily_halt, week_peak, weekly_halt
        nonlocal loss_count_halt, maximum_weekly_drawdown
        equity = marked(quote)
        peak = max(peak, equity)
        drawdown = max(ZERO, (peak - equity) / peak)
        max_drawdown = max(max_drawdown, drawdown)
        if loss_limits is not None:
            week_peak = max(week_peak, equity)
        if policy.halt_at is not None and quote.available_at >= policy.halt_at:
            latch(quote, "operator_halt")
        if position is not None and not fresh:
            latch(quote, "stale_quote_with_open_position")
        if equity <= ZERO:
            latch(quote, "non_positive_equity")
        elif drawdown >= policy.max_drawdown_fraction:
            latch(quote, "maximum_drawdown")
        if day_start > ZERO and equity <= day_start * (ONE - policy.max_daily_loss_fraction) and not daily_halt:
            daily_halt = True
            event(quote, "daily_halt", "maximum_daily_loss")
        if loss_limits is not None:
            if losses_today >= loss_limits.max_losses_per_day and not loss_count_halt:
                loss_count_halt = True
                event(quote, "daily_loss_count_halt", "maximum_losses_per_day")
            if week_peak > ZERO:
                weekly_drawdown = max(ZERO, (week_peak - equity) / week_peak)
                maximum_weekly_drawdown = max(maximum_weekly_drawdown, weekly_drawdown)
                if weekly_drawdown >= loss_limits.max_weekly_drawdown_fraction and not weekly_halt:
                    weekly_halt = True
                    event(quote, "weekly_halt", "maximum_weekly_drawdown")
        return equity

    def safety_exit_reason():
        if global_halt:
            return global_halt
        if daily_halt:
            return "maximum_daily_loss"
        if loss_count_halt:
            return "maximum_losses_per_day"
        if weekly_halt:
            return "maximum_weekly_drawdown"
        return None

    for quote in tape:
        age = quote.available_at - quote.timestamp
        age_ms = (age.days * 86400 + age.seconds) * 1000 + Decimal(age.microseconds) / Decimal(1000)
        fresh = age_ms <= policy.max_quote_age_ms
        new_week = False
        if loss_limits is not None:
            if day != quote.available_at.date():
                losses_today = 0
                loss_count_halt = False
            iso_year, iso_week, _ = quote.available_at.isocalendar()
            received_week = "{:04d}-W{:02d}".format(iso_year, iso_week)
            if week_id != received_week:
                week_id = received_week
                week_peak = last_observed_equity
                weekly_halt = False
                new_week = True
        if position is not None:
            crossed = (quote.available_at.date() - position["financed_through"].date()).days
            if crossed:
                charge = position["lots"] * policy.financing_per_lot_per_utc_day * crossed
                balance -= charge
                total_financing += charge
                position["financing"] += charge
                position["financed_through"] = quote.available_at
                event(quote, "financing", "utc_date_crossings", amount=charge, utc_days=crossed, intent_id=position["intent_id"])
        equity = marked(quote)
        if day != quote.available_at.date():
            # The first replay day begins with initial cash; subsequent days use
            # the first received mark, not an invented midnight quote.
            day_start = balance if day is None and position is None else equity
            day = quote.available_at.date()
            daily_halt = False
            trades_today = 0
            event(quote, "day_started", "first_received_mark", baseline=day_start)
        if new_week:
            event(quote, "week_started", "previous_observed_equity", baseline=week_peak)
        check_guards(quote, fresh)

        exited = False
        if position is not None and fresh:
            reason, limit = None, None
            reason = safety_exit_reason()
            if reason is not None:
                pass
            elif position["side"] == "buy" and quote.bid <= position["stop_loss"]:
                reason = "stop_loss"
            elif position["side"] == "sell" and quote.ask >= position["stop_loss"]:
                reason = "stop_loss"
            elif position["side"] == "buy" and quote.bid >= position["take_profit"]:
                reason, limit = "take_profit", position["take_profit"]
            elif position["side"] == "sell" and quote.ask <= position["take_profit"]:
                reason, limit = "take_profit", position["take_profit"]
            else:
                elapsed = quote.available_at - position["entered_at"]
                if elapsed.days * 86400 + elapsed.seconds >= policy.max_holding_seconds:
                    reason = "maximum_holding_time"
            if reason:
                close(quote, reason, limit)
                exited = True

        due = []
        for intent in requests:
            if intent.intent_id in handled:
                continue
            if quote.available_at >= intent.expires_at:
                handled.add(intent.intent_id)
                event(quote, "intent_cancelled", "expired", intent_id=intent.intent_id)
            elif isinstance(intent, ExactOpenIntent):
                if quote.timestamp >= intent.not_before and quote.available_at >= intent.created_at:
                    if quote.timestamp != intent.not_before:
                        reject(quote, intent, "missing_exact_open_quote")
                    elif quote.available_at <= intent.created_at:
                        reject(quote, intent, "ambiguous_entry_ordering")
                    else:
                        due.append(intent)
            elif quote.available_at > intent.created_at and quote.timestamp >= intent.not_before and quote.available_at >= intent.not_before:
                due.append(intent)
        common_reason = None
        if exited:
            common_reason = "no_same_quote_reentry"
        elif position is not None:
            common_reason = "position_already_open"
        elif global_halt:
            common_reason = "global_halt_active"
        elif daily_halt:
            common_reason = "daily_halt_active"
        elif loss_count_halt:
            common_reason = "loss_count_halt_active"
        elif weekly_halt:
            common_reason = "weekly_halt_active"
        elif not fresh:
            common_reason = "stale_quote"
        elif len(due) > 1:
            common_reason = "conflicting_intents"
        elif trades_today >= policy.max_trades_per_day:
            common_reason = "maximum_trades_per_day"
        for intent in due:
            if common_reason:
                reject(quote, intent, common_reason)
                continue
            if quote.ask - quote.bid > policy.max_spread:
                reject(quote, intent, "spread_exceeds_limit")
                continue
            buy = intent.side == "buy"
            entry = adverse(quote.ask + policy.slippage if buy else quote.bid - policy.slippage, buy)
            trigger = quote.bid if buy else quote.ask
            if entry <= ZERO:
                reject(quote, intent, "non_positive_entry_fill")
                continue
            if isinstance(intent, ExactOpenIntent):
                directional = [level for level in intent.target_levels
                               if (level > entry if buy else level < entry)]
                if not directional:
                    reject(quote, intent, "no_directional_target")
                    continue
                take_profit = directional[0] if buy else directional[-1]
            else:
                take_profit = intent.take_profit
            if not ((intent.stop_loss < trigger and intent.stop_loss < entry < take_profit and trigger < take_profit) if buy else (take_profit < trigger and take_profit < entry < intent.stop_loss and trigger < intent.stop_loss)):
                reject(quote, intent, "invalid_or_already_crossed_bracket")
                continue
            if min(abs(trigger - intent.stop_loss), abs(trigger - take_profit)) < instrument.minimum_stop_distance:
                reject(quote, intent, "minimum_stop_distance")
                continue
            modeled_stop_fill = adverse(intent.stop_loss - policy.slippage if buy else intent.stop_loss + policy.slippage, not buy)
            if modeled_stop_fill <= ZERO:
                reject(quote, intent, "non_positive_modeled_stop_fill")
                continue
            cost_per_lot = abs(entry - modeled_stop_fill) * instrument.contract_size + 2 * policy.commission_per_lot_per_side
            reward_per_lot = abs(take_profit - entry) * instrument.contract_size - 2 * policy.commission_per_lot_per_side
            if reward_per_lot <= ZERO or reward_per_lot < cost_per_lot * policy.min_reward_risk:
                reject(quote, intent, "minimum_reward_risk")
                continue
            budget = min(balance * policy.risk_fraction,
                         balance - day_start * (ONE - policy.max_daily_loss_fraction),
                         balance - peak * (ONE - policy.max_drawdown_fraction))
            if loss_limits is not None:
                budget = min(budget, balance - week_peak * (ONE - loss_limits.max_weekly_drawdown_fraction))
            lots = (min(instrument.max_lots, max(ZERO, budget) / cost_per_lot) / instrument.lot_step).to_integral_value(rounding=ROUND_FLOOR) * instrument.lot_step
            if lots * cost_per_lot > budget:
                lots -= instrument.lot_step
            if lots < instrument.min_lots:
                reject(quote, intent, "below_minimum_lot")
                continue
            commission = lots * policy.commission_per_lot_per_side
            balance -= commission
            total_commission += commission
            trades_today += 1
            handled.add(intent.intent_id)
            position = dict(intent_id=intent.intent_id, side=intent.side,
                            source_fingerprint=intent.source_fingerprint,
                            entered_at=quote.available_at, entry_quote_timestamp=quote.timestamp,
                            lots=lots, entry_price=entry, stop_loss=intent.stop_loss,
                            take_profit=take_profit, entry_commission=commission,
                            financing=ZERO, financed_through=quote.available_at,
                            modeled_stop_risk=lots * cost_per_lot,
                            initial_risk_budget=budget, net_reward_risk=reward_per_lot / cost_per_lot)
            fills.append(dict(intent_id=intent.intent_id, timestamp=quote.available_at,
                              quote_timestamp=quote.timestamp, phase="entry", action=intent.side,
                              reason="bracket_accepted", lots=lots, price=entry, commission=commission))
            event(quote, "intent_accepted", "bracket_accepted", intent_id=intent.intent_id,
                  lots=lots, risk_budget=budget, modeled_stop_risk=lots * cost_per_lot,
                  net_reward_risk=reward_per_lot / cost_per_lot,
                  **({"take_profit": take_profit, "entry_contract": "exact_source_boundary_strict_receipt_order"}
                     if isinstance(intent, ExactOpenIntent) else {}))
        equity = marked(quote)
        if loss_limits is not None:
            equity = check_guards(quote, fresh)
            reason = safety_exit_reason()
            if position is not None and fresh and reason is not None:
                close(quote, reason)
                equity = marked(quote)
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, max(ZERO, (peak - equity) / peak))
        curve.append(dict(timestamp=quote.available_at, quote_timestamp=quote.timestamp,
                          balance=balance, equity=equity, mark_is_fresh=fresh,
                          position_lots=ZERO if position is None else position["lots"],
                          position_side=None if position is None else position["side"],
                          daily_halt=daily_halt, global_halt=global_halt, **loss_state()))
        last_observed_equity = equity

    last = tape[-1]
    not_observed = []
    for intent in requests:
        if intent.intent_id not in handled:
            if intent.created_at > last.available_at:
                not_observed.append(dict(intent_id=intent.intent_id, created_at=intent.created_at,
                                         reason="created_after_tape_end"))
            elif isinstance(intent, ExactOpenIntent) and intent.not_before > last.timestamp:
                not_observed.append(dict(intent_id=intent.intent_id, created_at=intent.created_at,
                                         reason="entry_boundary_after_tape_end"))
            else:
                event(last, "intent_cancelled", "end_of_data", intent_id=intent.intent_id)
    if position is not None and policy.force_flat_at_end:
        if curve[-1]["mark_is_fresh"]:
            close(last, "end_of_data")
            curve[-1].update(balance=balance, equity=balance, position_lots=ZERO, position_side=None,
                             daily_halt=daily_halt, global_halt=global_halt, **loss_state())
        else:
            event(last, "liquidation_deferred", "last_quote_stale", intent_id=position["intent_id"])
    equity = marked(last)
    payload = dict(
        status="offline_paper_simulation_complete", engine_version=ENGINE_VERSION,
        broker_connected=False, real_orders_submitted=0, promotion_eligible=False,
        input_fingerprint=_digest(dict(engine_version=ENGINE_VERSION, quotes=tape, intents=requests,
                                       instrument=instrument, policy=policy, loss_limits=limits_payload)),
        instrument=instrument, policy=policy, loss_limits=limits_payload,
        counts=dict(quotes=len(tape), intents=len(requests), fills=len(fills), trades=len(trades),
                    events=len(events), equity_points=len(curve),
                    accepted_intents=sum(e["kind"] == "intent_accepted" for e in events),
                    rejected_intents=sum(e["kind"] == "intent_rejected" for e in events),
                    cancelled_intents=sum(e["kind"] == "intent_cancelled" for e in events),
                    not_observed_intents=len(not_observed)),
        initial_balance=policy.initial_balance, final_balance=balance, final_equity=equity,
        net_pnl=equity - policy.initial_balance, realized_balance_change=balance - policy.initial_balance,
        unrealized_pnl_after_modeled_exit_costs=equity - balance,
        total_commission=total_commission, total_financing=total_financing,
        maximum_drawdown_fraction=max_drawdown, global_halt=global_halt, daily_halt=daily_halt,
        **(dict(maximum_weekly_drawdown_fraction=maximum_weekly_drawdown, **loss_state())
           if loss_limits is not None else {}),
        open_position=position, final_mark_is_fresh=curve[-1]["mark_is_fresh"],
        fills=fills, trades=trades, events=events, equity_curve=curve,
        not_observed_intents=not_observed,
        assumptions=dict(
            mode="offline hypothetical quote-event brackets, not strategy performance",
            pnl="USD linear: signed price movement times lots times contract_size",
            entry="first causally eligible received quote; no retry after rejection",
            exact_open_entry="first source quote at or after requested boundary must equal boundary; receipt must be strictly after creation; equal-time ambiguity or missing boundary rejects once",
            exact_open_target="nearest strictly directional decision-time frozen level after adverse entry rounding; never skip a nearer level to meet reward/risk",
            stops="stop-market at current quote with adverse slippage and tick rounding",
            targets="limit-style exactly at requested target when trigger quote crosses",
            equity="exit-side mark including modeled exit slippage and commission, capped at resting limit target",
            daily_baseline="initial cash on first day; first received marked equity on later UTC dates",
            financing="explicit flat per-lot charge for each elapsed UTC date crossed; not broker swaps",
            risk_budget="minimum of balance risk fraction and remaining daily/drawdown allowance; weekly high-water allowance also applies only when explicit loss_limits are enabled",
            margin_modeled=False, liquidity_modeled=False, latency_modeled=False,
            broker_contract_verified=False, strategy_evaluated=False,
        ),
        warnings=[
            "No broker connection or orders. Synthetic/hypothetical fills do not demonstrate profitability.",
            "Stop gaps can exceed modeled stop risk and loss limits; financing is excluded from initial stop-risk sizing.",
            "Margin, liquidation rules, liquidity, partial fills, requotes, network latency and broker-specific costs are unmodeled.",
            "Fixed slippage and UTC-date financing are explicit test assumptions, not Exness specifications.",
            "A stale final mark is not executable and cannot close an open position.",
            "No StrategySpec-v1 or supply/demand candidate runtime parity is asserted.",
        ],
    )
    payload["fingerprint"] = _digest(payload)
    return PaperExecutionResult(_canonical(payload))
