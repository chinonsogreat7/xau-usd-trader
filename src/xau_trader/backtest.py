"""A small event-driven backtester with next-bar execution semantics."""

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Dict, List, Optional, Sequence, Tuple

from .domain import AvailabilityBasis, EquityPoint, Fill, QuoteBar, TargetPosition
from .risk import RiskEngine
from .strategies import Strategy


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 10_000.0
    units: float = 1.0
    slippage_bps: float = 0.0
    commission_per_unit: float = 0.0
    financing_per_unit_per_bar: float = 0.0
    force_flat_at_end: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be a finite positive number")
        if not math.isfinite(self.units) or self.units <= 0:
            raise ValueError("units must be a finite positive number")
        for name in ("slippage_bps", "commission_per_unit"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError("{} must be a finite non-negative number".format(name))
        if self.slippage_bps >= 10_000:
            raise ValueError("slippage_bps must be less than 10,000")
        if not math.isfinite(self.financing_per_unit_per_bar):
            raise ValueError("financing_per_unit_per_bar must be finite")


@dataclass(frozen=True)
class RiskEvent:
    timestamp: datetime
    requested: TargetPosition
    approved_target: TargetPosition
    reason: str


@dataclass(frozen=True)
class BacktestResult:
    strategy_name: str
    config: BacktestConfig
    initial_cash: float
    final_equity: float
    net_pnl: float
    return_fraction: float
    max_drawdown_fraction: float
    max_intrabar_drawdown_fraction: float
    exposure_fraction: float
    total_spread_cost: float
    total_slippage_cost: float
    total_commission: float
    total_financing: float
    fills: Tuple[Fill, ...]
    closed_positions: int
    equity_curve: Tuple[EquityPoint, ...]
    risk_events: Tuple[RiskEvent, ...]

    def summary(self) -> Dict[str, object]:
        return {
            "strategy": self.strategy_name,
            "initial_cash": round(self.initial_cash, 6),
            "final_equity": round(self.final_equity, 6),
            "net_pnl": round(self.net_pnl, 6),
            "gross_pnl_before_modeled_costs": round(
                self.net_pnl
                + self.total_spread_cost
                + self.total_slippage_cost
                + self.total_commission
                + self.total_financing,
                6,
            ),
            "return_fraction": round(self.return_fraction, 8),
            "max_drawdown_fraction": round(self.max_drawdown_fraction, 8),
            "max_intrabar_drawdown_fraction": round(
                self.max_intrabar_drawdown_fraction, 8
            ),
            "exposure_fraction": round(self.exposure_fraction, 8),
            "costs": {
                "spread": round(self.total_spread_cost, 6),
                "slippage": round(self.total_slippage_cost, 6),
                "commission": round(self.total_commission, 6),
                "financing": round(self.total_financing, 6),
            },
            "assumptions": {
                "units": self.config.units,
                "slippage_bps": self.config.slippage_bps,
                "commission_per_unit": self.config.commission_per_unit,
                "financing_per_unit_per_bar": self.config.financing_per_unit_per_bar,
                "force_flat_at_end": self.config.force_flat_at_end,
                "margin_and_capacity_modeled": False,
                "broker_cost_model_complete": False,
            },
            "fill_count": len(self.fills),
            "closed_positions": self.closed_positions,
            "risk_event_count": len(self.risk_events),
            "evidence_label": "engineering diagnostic only",
            "promotion_eligible": False,
            "terminal_value_status": (
                "realized after forced liquidation"
                if self.config.force_flat_at_end
                else "unrealized executable mark; exit commission/slippage excluded"
            ),
            "warning": "Synthetic or historical results are hypothetical, not a profit forecast.",
        }


class Backtester:
    """Execute close-of-bar signals at the next bar's executable open price."""

    def __init__(
        self,
        config: Optional[BacktestConfig] = None,
        risk_engine: Optional[RiskEngine] = None,
    ) -> None:
        self.config = config or BacktestConfig()
        self.risk_engine = risk_engine

    def run(self, bars: Sequence[QuoteBar], strategy: Strategy) -> BacktestResult:
        series = tuple(bars)
        self._validate_series(series)
        if self.risk_engine is not None:
            self.risk_engine.reset_for_replay(
                initial_equity=self.config.initial_cash,
                session_date=series[0].timestamp.date(),
            )

        cash = self.config.initial_cash
        position = 0.0
        pending_target: Optional[TargetPosition] = None
        fills: List[Fill] = []
        equity_curve: List[EquityPoint] = []
        intrabar_worst_equities: List[float] = []
        risk_events: List[RiskEvent] = []
        closed_positions = 0
        exposed_bars = 0
        total_financing = 0.0

        for index, bar in enumerate(series):
            if pending_target is not None:
                current_target = self._target_from_position(position)
                if (
                    current_target != TargetPosition.FLAT
                    and pending_target != TargetPosition.FLAT
                    and pending_target != current_target
                ):
                    pending_target = TargetPosition.FLAT
                if self.risk_engine is not None:
                    requested = pending_target
                    execution_decision = self.risk_engine.check_execution(
                        requested,
                        current_target,
                        self._mark_to_market_open(cash, position, bar),
                        bar,
                        requested_units=self.config.units,
                    )
                    pending_target = execution_decision.target
                    if not execution_decision.approved:
                        risk_events.append(
                            RiskEvent(
                                timestamp=bar.start_time,
                                requested=requested,
                                approved_target=execution_decision.target,
                                reason=execution_decision.reason,
                            )
                        )
                cash, position, fill, closed = self._execute(
                    cash,
                    position,
                    pending_target,
                    bar,
                    at_close=False,
                    reason="prior close signal",
                )
                if fill is not None:
                    fills.append(fill)
                    closed_positions += closed

            if position != 0:
                financing = abs(position) * self.config.financing_per_unit_per_bar
                cash -= financing
                total_financing += financing
                exposed_bars += 1

            equity = self._mark_to_market(cash, position, bar)
            intrabar_worst = self._worst_intrabar_equity(cash, position, bar)
            intrabar_worst_equities.append(intrabar_worst)
            equity_curve.append(EquityPoint(bar.timestamp, equity, position))

            if index == len(series) - 1:
                continue

            raw_target = strategy.target(series[: index + 1])
            try:
                proposed = TargetPosition(raw_target)
            except (TypeError, ValueError) as exc:
                raise ValueError("strategy returned an invalid target: {!r}".format(raw_target)) from exc

            current = self._target_from_position(position)
            if self.risk_engine is None:
                pending_target = proposed
            else:
                decision = self.risk_engine.review(
                    proposed,
                    current,
                    min(equity, intrabar_worst),
                    bar,
                    requested_units=self.config.units,
                )
                pending_target = decision.target
                if not decision.approved:
                    risk_events.append(
                        RiskEvent(
                            timestamp=bar.timestamp,
                            requested=proposed,
                            approved_target=decision.target,
                            reason=decision.reason,
                        )
                    )

        if self.config.force_flat_at_end and position != 0:
            last_bar = series[-1]
            cash, position, fill, closed = self._execute(
                cash,
                position,
                TargetPosition.FLAT,
                last_bar,
                at_close=True,
                reason="forced end-of-test liquidation",
            )
            if fill is not None:
                fills.append(fill)
                closed_positions += closed
            equity_curve[-1] = EquityPoint(last_bar.timestamp, cash, position)
            intrabar_worst_equities[-1] = min(intrabar_worst_equities[-1], cash)

        final_equity = equity_curve[-1].equity
        total_spread_cost = sum(fill.spread_cost for fill in fills)
        total_slippage_cost = sum(fill.slippage_cost for fill in fills)
        total_commission = sum(fill.commission for fill in fills)
        return BacktestResult(
            strategy_name=getattr(strategy, "name", strategy.__class__.__name__),
            config=self.config,
            initial_cash=self.config.initial_cash,
            final_equity=final_equity,
            net_pnl=final_equity - self.config.initial_cash,
            return_fraction=(final_equity / self.config.initial_cash) - 1.0,
            max_drawdown_fraction=self._max_drawdown(equity_curve),
            max_intrabar_drawdown_fraction=self._max_intrabar_drawdown(
                equity_curve, intrabar_worst_equities
            ),
            exposure_fraction=exposed_bars / len(series),
            total_spread_cost=total_spread_cost,
            total_slippage_cost=total_slippage_cost,
            total_commission=total_commission,
            total_financing=total_financing,
            fills=tuple(fills),
            closed_positions=closed_positions,
            equity_curve=tuple(equity_curve),
            risk_events=tuple(risk_events),
        )

    def _execute(
        self,
        cash: float,
        position: float,
        target: TargetPosition,
        bar: QuoteBar,
        at_close: bool,
        reason: str,
    ) -> Tuple[float, float, Optional[Fill], int]:
        new_position = target.value * self.config.units
        quantity = new_position - position
        if quantity == 0:
            return cash, position, None, 0

        if quantity > 0:
            quote = bar.ask_close if at_close else bar.ask_open
            midpoint = (
                (bar.bid_close + bar.ask_close) / 2.0
                if at_close
                else (bar.bid_open + bar.ask_open) / 2.0
            )
            price = quote * (1.0 + self.config.slippage_bps / 10_000.0)
        else:
            quote = bar.bid_close if at_close else bar.bid_open
            midpoint = (
                (bar.bid_close + bar.ask_close) / 2.0
                if at_close
                else (bar.bid_open + bar.ask_open) / 2.0
            )
            price = quote * (1.0 - self.config.slippage_bps / 10_000.0)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("execution produced a non-positive or invalid fill price")
        commission = abs(quantity) * self.config.commission_per_unit
        spread_cost = abs(quantity) * abs(quote - midpoint)
        slippage_cost = abs(quantity) * abs(price - quote)
        new_cash = cash - quantity * price - commission
        if not math.isfinite(new_cash):
            raise ValueError("execution produced invalid cash")
        closed = int(position != 0 and (new_position == 0 or position * new_position < 0))
        return (
            new_cash,
            new_position,
            Fill(
                timestamp=bar.timestamp if at_close else bar.start_time,
                previous_position=position,
                new_position=new_position,
                quantity=quantity,
                price=price,
                commission=commission,
                spread_cost=spread_cost,
                slippage_cost=slippage_cost,
                reason=reason,
            ),
            closed,
        )

    @staticmethod
    def _mark_to_market(cash: float, position: float, bar: QuoteBar) -> float:
        if position > 0:
            liquidation_price = bar.bid_close
        elif position < 0:
            liquidation_price = bar.ask_close
        else:
            liquidation_price = bar.mid_close
        return cash + position * liquidation_price

    @staticmethod
    def _mark_to_market_open(cash: float, position: float, bar: QuoteBar) -> float:
        if position > 0:
            liquidation_price = bar.bid_open
        elif position < 0:
            liquidation_price = bar.ask_open
        else:
            liquidation_price = (bar.bid_open + bar.ask_open) / 2.0
        return cash + position * liquidation_price

    @staticmethod
    def _worst_intrabar_equity(cash: float, position: float, bar: QuoteBar) -> float:
        if position > 0:
            liquidation_price = bar.bid_low
        elif position < 0:
            liquidation_price = bar.ask_high
        else:
            return cash
        return cash + position * liquidation_price

    @staticmethod
    def _target_from_position(position: float) -> TargetPosition:
        if position > 0:
            return TargetPosition.LONG
        if position < 0:
            return TargetPosition.SHORT
        return TargetPosition.FLAT

    def _max_drawdown(self, points: Sequence[EquityPoint]) -> float:
        peak = self.config.initial_cash
        max_drawdown = 0.0
        for point in points:
            peak = max(peak, point.equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - point.equity) / peak)
        return max_drawdown

    def _max_intrabar_drawdown(
        self,
        points: Sequence[EquityPoint],
        worst_equities: Sequence[float],
    ) -> float:
        peak = self.config.initial_cash
        max_drawdown = 0.0
        for point, worst in zip(points, worst_equities):
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - worst) / peak)
            peak = max(peak, point.equity)
        return max(0.0, max_drawdown)

    @staticmethod
    def _validate_series(bars: Sequence[QuoteBar]) -> None:
        if not bars:
            raise ValueError("at least one quote bar is required")
        for previous, current in zip(bars, bars[1:]):
            if current.timestamp <= previous.timestamp:
                raise ValueError("quote bars must be strictly increasing with no duplicates")
            if current.start_time < previous.timestamp:
                raise ValueError("quote bars must not overlap")
        for bar in bars:
            if bar.availability_basis not in {
                AvailabilityBasis.SYNTHETIC,
                AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
            }:
                raise ValueError(
                    "the historical backtester rejects observed-receipt market data"
                )
            if bar.available_at != bar.timestamp:
                raise ValueError(
                    "the current backtester requires available_at to equal the completed bar "
                    "timestamp; delayed data needs an event-time execution model"
                )
