"""Coherent synthetic integration input; never an observed historical tick path."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext

from xau_trader.diagnostic_replay import run_diagnostic_replay
from xau_trader.paper_execution import PaperInstrument, PaperPolicy, PaperQuote
from xau_trader.research_baseline import provisional_diagnostic_baseline_v1
from xau_trader.structural_exits import StructuralExitPolicy
from structural_exit_fixture import structural_exit_m15_bars


D = Decimal
STRUCTURAL_POLICY = StructuralExitPolicy(3, 3, D("0.15"))


def rounded_bars(bars=None):
    with localcontext() as context:
        context.prec = 40
        return tuple(replace(bar, **{
            side + "_" + part: float(D(str(getattr(bar, side + "_" + part))).quantize(D("0.01")))
            for side in ("bid", "ask") for part in ("open", "high", "low", "close")
        }) for bar in (structural_exit_m15_bars() if bars is None else bars))


def coherent_quotes(bars):
    tape = []
    for bar in bars:
        for part, timestamp, received in (
            ("open", bar.start_time, bar.start_time + timedelta(microseconds=1)),
            ("low", bar.start_time + timedelta(minutes=5), bar.start_time + timedelta(minutes=5)),
            ("high", bar.start_time + timedelta(minutes=10), bar.start_time + timedelta(minutes=10)),
            ("close", bar.timestamp - timedelta(microseconds=1), bar.timestamp),
        ):
            tape.append(PaperQuote(timestamp, received,
                                   D(str(getattr(bar, "bid_" + part))),
                                   D(str(getattr(bar, "ask_" + part)))))
    return tuple(tape)


def replay(bars):
    return run_diagnostic_replay(bars, dataset_fingerprint="a" * 64,
                                 policy_bundle=provisional_diagnostic_baseline_v1())


def instrument():
    return PaperInstrument("XAU_USD", D("0.01"), D("100"), D("0.01"),
                           D("10"), D("0.01"), D("0.01"))


def execution_policy(**changes):
    return replace(PaperPolicy(
        initial_balance=D("500"), risk_fraction=D("0.005"),
        max_daily_loss_fraction=D("0.01"), max_drawdown_fraction=D("0.10"),
        max_spread=D("1"), slippage=D("0.01"),
        commission_per_lot_per_side=D("0.10"), financing_per_lot_per_utc_day=D("0"),
        max_quote_age_ms=1000, max_trades_per_day=5, max_holding_seconds=3600,
        force_flat_at_end=True, min_reward_risk=D("2"), halt_at=None,
    ), **changes)
