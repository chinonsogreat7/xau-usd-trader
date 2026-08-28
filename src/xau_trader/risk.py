"""Fail-closed historical-replay controls, not a durable paper-execution service."""

from dataclasses import dataclass
from datetime import date, datetime
import math
from typing import Optional

from .domain import QuoteBar, TargetPosition


@dataclass(frozen=True)
class RiskPolicy:
    """Conservative defaults for the paper-only scaffold.

    These values are engineering defaults, not a recommendation to trade live.
    """

    max_drawdown_fraction: float = 0.05
    max_daily_loss_fraction: float = 0.02
    max_spread_bps: float = 25.0
    max_units: float = 1.0
    allow_short: bool = True

    def __post_init__(self) -> None:
        for name in ("max_drawdown_fraction", "max_daily_loss_fraction"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError("{} must be between 0 and 1".format(name))
        if not math.isfinite(self.max_spread_bps) or self.max_spread_bps <= 0:
            raise ValueError("max_spread_bps must be a finite positive number")
        if not math.isfinite(self.max_units) or self.max_units <= 0:
            raise ValueError("max_units must be a finite positive number")


@dataclass(frozen=True)
class RiskDecision:
    target: TargetPosition
    approved: bool
    reason: str

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "target", TargetPosition(self.target))
        except (TypeError, ValueError) as exc:
            raise ValueError("risk decision target is not supported") from exc
        if not self.reason.strip():
            raise ValueError("risk decision reason must not be empty")


class RiskEngine:
    """In-memory replay guard that cannot increase risk after a policy breach."""

    def __init__(self, policy: RiskPolicy) -> None:
        self.policy = policy
        self._peak_equity: Optional[float] = None
        self._session_start_equity: Optional[float] = None
        self._session_date: Optional[date] = None
        self._last_timestamp: Optional[datetime] = None
        self._halted = False
        self._halt_reason: Optional[str] = None

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    def review(
        self,
        proposed: TargetPosition,
        current: TargetPosition,
        equity: float,
        bar: QuoteBar,
        requested_units: float = 1.0,
    ) -> RiskDecision:
        try:
            proposed = TargetPosition(proposed)
            current = TargetPosition(current)
        except (TypeError, ValueError):
            return self._halt("invalid target position")
        if self._last_timestamp is not None and bar.timestamp <= self._last_timestamp:
            return self._halt("non-increasing risk event time")
        self._last_timestamp = bar.timestamp

        if not math.isfinite(equity) or equity <= 0:
            return self._halt("non-positive or invalid equity")

        current_date = bar.timestamp.date()
        if self._session_date != current_date:
            self._session_date = current_date
            self._session_start_equity = equity

        if self._peak_equity is None:
            self._peak_equity = equity
        else:
            self._peak_equity = max(self._peak_equity, equity)

        if self._halted:
            return RiskDecision(TargetPosition.FLAT, False, self._halt_reason or "risk halt")

        assert self._session_start_equity is not None
        drawdown = max(0.0, (self._peak_equity - equity) / self._peak_equity)
        daily_loss = max(
            0.0,
            (self._session_start_equity - equity) / self._session_start_equity,
        )
        if drawdown >= self.policy.max_drawdown_fraction:
            return self._halt("maximum drawdown reached")
        if daily_loss >= self.policy.max_daily_loss_fraction:
            return self._halt("maximum daily loss reached")

        if not math.isfinite(requested_units) or requested_units <= 0:
            return self._halt("invalid requested position size")
        if proposed != TargetPosition.FLAT and requested_units > self.policy.max_units:
            return RiskDecision(TargetPosition.FLAT, False, "requested units exceed policy limit")

        if proposed == TargetPosition.SHORT and not self.policy.allow_short:
            target = TargetPosition.FLAT if current == TargetPosition.LONG else current
            return RiskDecision(target, False, "short positions are disabled")

        if bar.spread_bps_at_close > self.policy.max_spread_bps:
            if proposed == TargetPosition.FLAT or proposed == current:
                return RiskDecision(proposed, True, "spread high; risk not increased")
            safe_target = TargetPosition.FLAT if current != TargetPosition.FLAT else current
            return RiskDecision(safe_target, False, "spread exceeds policy limit")

        return RiskDecision(proposed, True, "approved")

    def check_execution(
        self,
        proposed: TargetPosition,
        current: TargetPosition,
        equity: float,
        bar: QuoteBar,
        requested_units: float = 1.0,
    ) -> RiskDecision:
        """Revalidate a replay decision against the next executable open."""

        try:
            proposed = TargetPosition(proposed)
            current = TargetPosition(current)
        except (TypeError, ValueError):
            return self._halt("invalid target position")
        if self._halted:
            return RiskDecision(TargetPosition.FLAT, False, self._halt_reason or "risk halt")
        if not math.isfinite(equity) or equity <= 0:
            return self._halt("non-positive or invalid equity")
        if not math.isfinite(requested_units) or requested_units <= 0:
            return self._halt("invalid requested position size")
        if proposed != TargetPosition.FLAT and requested_units > self.policy.max_units:
            return RiskDecision(TargetPosition.FLAT, False, "requested units exceed policy limit")

        if self._peak_equity is not None:
            drawdown = max(0.0, (self._peak_equity - equity) / self._peak_equity)
            if drawdown >= self.policy.max_drawdown_fraction:
                return self._halt("maximum drawdown reached before execution")
        execution_date = bar.start_time.date()
        if self._session_date != execution_date:
            self._session_date = execution_date
            self._session_start_equity = equity
        if self._session_start_equity is not None:
            daily_loss = max(
                0.0,
                (self._session_start_equity - equity) / self._session_start_equity,
            )
            if daily_loss >= self.policy.max_daily_loss_fraction:
                return self._halt("maximum daily loss reached before execution")

        if bar.spread_bps_at_open > self.policy.max_spread_bps:
            if proposed == TargetPosition.FLAT or proposed == current:
                return RiskDecision(proposed, True, "open spread high; risk not increased")
            safe_target = TargetPosition.FLAT if current != TargetPosition.FLAT else current
            return RiskDecision(safe_target, False, "open spread exceeds policy limit")
        return RiskDecision(proposed, True, "approved for replay execution")

    def reset_for_replay(
        self,
        initial_equity: Optional[float] = None,
        session_date: Optional[date] = None,
    ) -> None:
        """Start an independent historical replay with clean risk state."""

        self._reset_state()
        if initial_equity is not None:
            if not math.isfinite(initial_equity) or initial_equity <= 0:
                raise ValueError("initial_equity must be a finite positive number")
            self._peak_equity = initial_equity
            self._session_start_equity = initial_equity
            self._session_date = session_date

    def _reset_state(self) -> None:

        self._peak_equity = None
        self._session_start_equity = None
        self._session_date = None
        self._last_timestamp = None
        self._halted = False
        self._halt_reason = None

    def _halt(self, reason: str) -> RiskDecision:
        self._halted = True
        self._halt_reason = reason
        return RiskDecision(TargetPosition.FLAT, False, reason)
