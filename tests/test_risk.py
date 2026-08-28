import unittest
from datetime import datetime, timedelta, timezone

from xau_trader.domain import AvailabilityBasis, QuoteBar, TargetPosition
from xau_trader.risk import RiskEngine, RiskPolicy


def bar(timestamp, bid=99.5, ask=100.5):
    return QuoteBar(
        timestamp=timestamp,
        start_time=timestamp - timedelta(hours=1),
        availability_basis=AvailabilityBasis.SYNTHETIC,
        available_at=timestamp,
        bid_open=bid,
        bid_high=bid,
        bid_low=bid,
        bid_close=bid,
        ask_open=ask,
        ask_high=ask,
        ask_low=ask,
        ask_close=ask,
    )


class RiskEngineTests(unittest.TestCase):
    def test_high_spread_blocks_opening_but_allows_closing(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        engine = RiskEngine(
            RiskPolicy(
                max_spread_bps=50.0,
                max_drawdown_fraction=0.05,
                max_daily_loss_fraction=0.02,
            )
        )
        opening = engine.review(TargetPosition.LONG, TargetPosition.FLAT, 1_000.0, bar(now))
        closing = engine.review(
            TargetPosition.FLAT, TargetPosition.LONG, 1_000.0, bar(now + timedelta(hours=1))
        )
        self.assertFalse(opening.approved)
        self.assertEqual(opening.target, TargetPosition.FLAT)
        self.assertTrue(closing.approved)

    def test_drawdown_halt_is_sticky_during_replay(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        engine = RiskEngine(
            RiskPolicy(
                max_spread_bps=200.0,
                max_drawdown_fraction=0.05,
                max_daily_loss_fraction=0.50,
            )
        )
        engine.review(TargetPosition.FLAT, TargetPosition.FLAT, 1_000.0, bar(now))
        breached = engine.review(
            TargetPosition.LONG, TargetPosition.FLAT, 940.0, bar(now + timedelta(hours=1))
        )
        recovered = engine.review(
            TargetPosition.LONG, TargetPosition.FLAT, 1_100.0, bar(now + timedelta(hours=2))
        )
        self.assertFalse(breached.approved)
        self.assertTrue(engine.halted)
        self.assertFalse(recovered.approved)
        self.assertEqual(recovered.target, TargetPosition.FLAT)

    def test_invalid_target_and_non_increasing_time_fail_closed(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        invalid_engine = RiskEngine(RiskPolicy(max_spread_bps=200.0))
        invalid = invalid_engine.review(5, TargetPosition.FLAT, 1_000.0, bar(now))
        self.assertFalse(invalid.approved)
        self.assertTrue(invalid_engine.halted)

        time_engine = RiskEngine(RiskPolicy(max_spread_bps=200.0))
        time_engine.review(TargetPosition.FLAT, TargetPosition.FLAT, 1_000.0, bar(now))
        repeated = time_engine.review(
            TargetPosition.FLAT, TargetPosition.FLAT, 1_000.0, bar(now)
        )
        self.assertFalse(repeated.approved)
        self.assertEqual(repeated.reason, "non-increasing risk event time")


if __name__ == "__main__":
    unittest.main()
