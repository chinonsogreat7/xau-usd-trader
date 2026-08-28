"""Deterministic baseline strategies; none are claims of profitability."""

from dataclasses import dataclass
from typing import Protocol, Sequence

from .domain import QuoteBar, TargetPosition


class Strategy(Protocol):
    name: str

    def target(self, history: Sequence[QuoteBar]) -> TargetPosition:
        """Return a close-of-bar target using only the supplied history."""


@dataclass(frozen=True)
class FlatStrategy:
    name: str = "flat-baseline"

    def target(self, history: Sequence[QuoteBar]) -> TargetPosition:
        return TargetPosition.FLAT


@dataclass(frozen=True)
class BuyAndHoldStrategy:
    name: str = "long-baseline"

    def target(self, history: Sequence[QuoteBar]) -> TargetPosition:
        return TargetPosition.LONG if history else TargetPosition.FLAT


@dataclass(frozen=True)
class SmaCrossStrategy:
    """Simple moving-average crossover used only as an engineering baseline."""

    fast_window: int = 12
    slow_window: int = 48
    long_only: bool = False
    name: str = "sma-cross-baseline"

    def __post_init__(self) -> None:
        if self.fast_window < 1:
            raise ValueError("fast_window must be positive")
        if self.slow_window <= self.fast_window:
            raise ValueError("slow_window must be greater than fast_window")

    def target(self, history: Sequence[QuoteBar]) -> TargetPosition:
        if len(history) < self.slow_window:
            return TargetPosition.FLAT
        closes = [bar.mid_close for bar in history]
        fast = sum(closes[-self.fast_window :]) / self.fast_window
        slow = sum(closes[-self.slow_window :]) / self.slow_window
        if fast > slow:
            return TargetPosition.LONG
        if fast < slow and not self.long_only:
            return TargetPosition.SHORT
        return TargetPosition.FLAT
