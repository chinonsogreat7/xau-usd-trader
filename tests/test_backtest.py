import unittest
from datetime import datetime, timedelta, timezone

from xau_trader.backtest import BacktestConfig, Backtester
from xau_trader.domain import AvailabilityBasis, QuoteBar, TargetPosition
from xau_trader.risk import RiskEngine, RiskPolicy


def constant_bars(count=3, bid=99.0, ask=101.0):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(
        QuoteBar(
            start_time=start + timedelta(hours=index),
            timestamp=start + timedelta(hours=index + 1),
            availability_basis=AvailabilityBasis.SYNTHETIC,
            available_at=start + timedelta(hours=index + 1),
            bid_open=bid,
            bid_high=bid,
            bid_low=bid,
            bid_close=bid,
            ask_open=ask,
            ask_high=ask,
            ask_low=ask,
            ask_close=ask,
        )
        for index in range(count)
    )


class ScriptedStrategy:
    name = "scripted"

    def __init__(self, targets):
        self.targets = tuple(targets)
        self.history_lengths = []

    def target(self, history):
        self.history_lengths.append(len(history))
        return self.targets[len(history) - 1]


class BacktesterTests(unittest.TestCase):
    def test_signal_fills_on_next_bar_and_pays_spread(self):
        strategy = ScriptedStrategy((TargetPosition.LONG, TargetPosition.FLAT))
        result = Backtester(BacktestConfig(initial_cash=1_000.0)).run(
            constant_bars(), strategy
        )

        self.assertEqual(strategy.history_lengths, [1, 2])
        self.assertEqual(len(result.fills), 2)
        self.assertEqual(result.fills[0].timestamp, constant_bars()[1].start_time)
        self.assertEqual(result.fills[0].price, 101.0)
        self.assertEqual(result.fills[1].price, 99.0)
        self.assertAlmostEqual(result.net_pnl, -2.0)
        self.assertEqual(result.closed_positions, 1)

    def test_slippage_and_commission_are_adverse(self):
        strategy = ScriptedStrategy((TargetPosition.LONG, TargetPosition.FLAT))
        result = Backtester(
            BacktestConfig(
                initial_cash=1_000.0,
                slippage_bps=10.0,
                commission_per_unit=0.5,
            )
        ).run(constant_bars(), strategy)
        self.assertAlmostEqual(result.net_pnl, -3.2)
        self.assertAlmostEqual(result.total_spread_cost, 2.0)
        self.assertAlmostEqual(result.total_slippage_cost, 0.2)
        self.assertAlmostEqual(result.total_commission, 1.0)

    def test_open_position_is_liquidated_at_last_executable_close(self):
        strategy = ScriptedStrategy((TargetPosition.LONG,))
        result = Backtester(BacktestConfig(initial_cash=1_000.0)).run(
            constant_bars(2), strategy
        )
        self.assertEqual(result.fills[-1].reason, "forced end-of-test liquidation")
        self.assertEqual(result.fills[-1].timestamp, constant_bars(2)[-1].timestamp)
        self.assertEqual(result.equity_curve[-1].position, 0.0)
        self.assertAlmostEqual(result.net_pnl, -2.0)

    def test_duplicate_timestamp_is_rejected(self):
        bars = constant_bars(2)
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            Backtester().run((bars[0], bars[0]), ScriptedStrategy((TargetPosition.FLAT,)))

    def test_risk_engine_caps_requested_units(self):
        strategy = ScriptedStrategy((TargetPosition.LONG,))
        backtester = Backtester(
            BacktestConfig(initial_cash=1_000.0, units=2.0),
            RiskEngine(RiskPolicy(max_units=1.0, max_spread_bps=500.0)),
        )
        result = backtester.run(constant_bars(2), strategy)
        self.assertEqual(len(result.fills), 0)
        self.assertEqual(result.risk_events[0].reason, "requested units exceed policy limit")

    def test_repeated_backtests_start_with_clean_risk_state(self):
        strategy_one = ScriptedStrategy((TargetPosition.FLAT,))
        strategy_two = ScriptedStrategy((TargetPosition.FLAT,))
        backtester = Backtester(
            BacktestConfig(initial_cash=1_000.0),
            RiskEngine(RiskPolicy(max_spread_bps=500.0)),
        )
        first = backtester.run(constant_bars(2), strategy_one)
        second = backtester.run(constant_bars(2), strategy_two)
        self.assertEqual(first.summary(), second.summary())

    def test_next_open_spread_is_rechecked_before_fill(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        narrow = QuoteBar(
            start_time=start,
            timestamp=start + timedelta(hours=1),
            availability_basis=AvailabilityBasis.SYNTHETIC,
            available_at=start + timedelta(hours=1),
            bid_open=99.95,
            bid_high=99.95,
            bid_low=99.95,
            bid_close=99.95,
            ask_open=100.05,
            ask_high=100.05,
            ask_low=100.05,
            ask_close=100.05,
        )
        wide_open = QuoteBar(
            start_time=start + timedelta(hours=1),
            timestamp=start + timedelta(hours=2),
            availability_basis=AvailabilityBasis.SYNTHETIC,
            available_at=start + timedelta(hours=2),
            bid_open=90.0,
            bid_high=99.95,
            bid_low=90.0,
            bid_close=99.95,
            ask_open=110.0,
            ask_high=110.0,
            ask_low=100.05,
            ask_close=100.05,
        )
        result = Backtester(
            BacktestConfig(initial_cash=1_000.0),
            RiskEngine(RiskPolicy(max_spread_bps=50.0)),
        ).run((narrow, wide_open), ScriptedStrategy((TargetPosition.LONG,)))
        self.assertEqual(len(result.fills), 0)
        self.assertEqual(result.risk_events[-1].reason, "open spread exceeds policy limit")

    def test_intrabar_drawdown_is_reported_separately(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = QuoteBar(
            start_time=start,
            timestamp=start + timedelta(hours=1),
            availability_basis=AvailabilityBasis.SYNTHETIC,
            available_at=start + timedelta(hours=1),
            bid_open=99.5,
            bid_high=99.5,
            bid_low=99.5,
            bid_close=99.5,
            ask_open=100.5,
            ask_high=100.5,
            ask_low=100.5,
            ask_close=100.5,
        )
        adverse = QuoteBar(
            start_time=start + timedelta(hours=1),
            timestamp=start + timedelta(hours=2),
            availability_basis=AvailabilityBasis.SYNTHETIC,
            available_at=start + timedelta(hours=2),
            bid_open=99.5,
            bid_high=100.0,
            bid_low=50.0,
            bid_close=99.5,
            ask_open=100.5,
            ask_high=101.0,
            ask_low=50.5,
            ask_close=100.5,
        )
        result = Backtester(BacktestConfig(initial_cash=100.0)).run(
            (first, adverse), ScriptedStrategy((TargetPosition.LONG,))
        )
        self.assertLess(result.max_drawdown_fraction, 0.02)
        self.assertGreater(result.max_intrabar_drawdown_fraction, 0.50)

    def test_reversal_flattens_before_opening_opposite_side(self):
        strategy = ScriptedStrategy(
            (TargetPosition.LONG, TargetPosition.SHORT, TargetPosition.SHORT)
        )
        result = Backtester(BacktestConfig(initial_cash=1_000.0)).run(
            constant_bars(4), strategy
        )
        self.assertEqual([fill.quantity for fill in result.fills[:3]], [1.0, -1.0, -1.0])
        self.assertNotIn(-2.0, [fill.quantity for fill in result.fills])


if __name__ == "__main__":
    unittest.main()
