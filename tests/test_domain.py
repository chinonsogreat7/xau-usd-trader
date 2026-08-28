import unittest
from datetime import datetime, timedelta, timezone

from xau_trader.domain import (
    AvailabilityBasis,
    QuoteBar,
    SourceCitation,
    StrategySpec,
    StrategyStatus,
)


class DomainTests(unittest.TestCase):
    def test_quote_bar_requires_timezone(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            QuoteBar.from_mid(
                datetime(2026, 1, 1),
                start_time=datetime(2025, 12, 31, 23, 0),
                availability_basis=AvailabilityBasis.SYNTHETIC,
                available_at=datetime(2026, 1, 1),
                mid_open=100.0,
                mid_high=101.0,
                mid_low=99.0,
                mid_close=100.5,
                spread=0.2,
            )

    def test_quote_bar_rejects_crossed_market(self):
        with self.assertRaisesRegex(ValueError, "bid_open"):
            QuoteBar(
                timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                start_time=datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc),
                availability_basis=AvailabilityBasis.SYNTHETIC,
                available_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                bid_open=101.0,
                bid_high=102.0,
                bid_low=100.0,
                bid_close=101.0,
                ask_open=100.0,
                ask_high=103.0,
                ask_low=99.0,
                ask_close=102.0,
            )

    def test_quote_bar_rejects_availability_before_completion(self):
        completed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "available_at"):
            QuoteBar.from_mid(
                completed,
                start_time=completed - timedelta(hours=1),
                availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
                mid_open=100.0,
                mid_high=101.0,
                mid_low=99.0,
                mid_close=100.5,
                spread=0.2,
                available_at=completed - timedelta(seconds=1),
            )

    def test_quote_bar_requires_start_before_end(self):
        completed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "start_time"):
            QuoteBar.from_mid(
                completed,
                start_time=completed,
                availability_basis=AvailabilityBasis.SYNTHETIC,
                available_at=completed,
                mid_open=100.0,
                mid_high=101.0,
                mid_low=99.0,
                mid_close=100.5,
                spread=0.2,
            )

    def test_quote_bar_requires_explicit_availability(self):
        completed = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaises(TypeError):
            QuoteBar.from_mid(
                completed,
                start_time=completed - timedelta(hours=1),
                availability_basis=AvailabilityBasis.SYNTHETIC,
                mid_open=100.0,
                mid_high=101.0,
                mid_low=99.0,
                mid_close=100.5,
                spread=0.2,
            )

    def test_frozen_strategy_cannot_keep_ambiguities(self):
        with self.assertRaisesRegex(ValueError, "unresolved ambiguities"):
            StrategySpec(
                strategy_id="candidate-1",
                version=1,
                source_id="source-1",
                instrument="XAU_USD",
                timeframe="1h",
                hypothesis="A falsifiable hypothesis.",
                entry_rules=("One exact entry rule",),
                exit_rules=("One exact exit rule",),
                risk_rules=("One exact risk rule",),
                citations=(SourceCitation(10.0, "The source claim"),),
                ambiguities=("The speaker did not define the session",),
                status=StrategyStatus.FROZEN,
            )

    def test_strategy_key_is_versioned(self):
        spec = StrategySpec(
            strategy_id="candidate-1",
            version=2,
            source_id="source-1",
            instrument="XAU_USD",
            timeframe="1h",
            hypothesis="A falsifiable hypothesis.",
            entry_rules=("One exact entry rule",),
            exit_rules=("One exact exit rule",),
            risk_rules=("One exact risk rule",),
            citations=(SourceCitation(10.0, "The source claim"),),
        )
        self.assertEqual(spec.key, "candidate-1:v2")


if __name__ == "__main__":
    unittest.main()
