"""XAU/USD strategy research and paper-trading foundations."""

from .backtest import BacktestConfig, BacktestResult, Backtester
from .domain import (
    AvailabilityBasis,
    ContentSource,
    ExtractedStrategyCandidate,
    QuoteBar,
    StrategySpec,
    TargetPosition,
)
from .risk import RiskEngine, RiskPolicy
from .learning import transcript_excerpt_sha256
from .strategy_compiler import CompiledStrategyPlan, compile_strategy_spec_v1
from .strategy_schema import ValidatedStrategySpecV1, validate_strategy_spec_v1

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "Backtester",
    "AvailabilityBasis",
    "ContentSource",
    "ExtractedStrategyCandidate",
    "QuoteBar",
    "RiskEngine",
    "RiskPolicy",
    "StrategySpec",
    "ValidatedStrategySpecV1",
    "CompiledStrategyPlan",
    "validate_strategy_spec_v1",
    "compile_strategy_spec_v1",
    "transcript_excerpt_sha256",
    "TargetPosition",
]

__version__ = "0.1.0"
