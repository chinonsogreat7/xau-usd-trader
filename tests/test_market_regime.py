import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from xau_trader.domain import AvailabilityBasis
from xau_trader.market_regime import (
    InsufficientEvidencePolicy,
    MarketRegime,
    MarketRegimeDataError,
    MixedStructurePolicy,
    RegimePolicy,
    StructureLeg,
    StructureRule,
    h1_regime_events,
)
from xau_trader.multitimeframe import H1PivotEvent, PivotKind


UTC = timezone.utc
BASE = datetime(2026, 1, 5, tzinfo=UTC)


def policy():
    return RegimePolicy(
        structure_rule=StructureRule.STRICT_LAST_TWO_CONFIRMED_HIGH_LOW,
        insufficient_evidence=InsufficientEvidencePolicy.UNKNOWN,
        mixed_structure=MixedStructurePolicy.RANGE,
    )


def pivot(
    sequence,
    kind,
    price,
    *,
    center=None,
    availability_delay=timedelta(0),
    availability_basis=AvailabilityBasis.SYNTHETIC,
):
    center_index = sequence if center is None else center
    start = BASE + timedelta(hours=4 * center_index)
    end = start + timedelta(hours=1)
    confirmed_at = end + timedelta(hours=3)
    return H1PivotEvent(
        kind=kind,
        pivot_bar_start=start,
        pivot_bar_end=end,
        confirmed_at=confirmed_at,
        available_at=confirmed_at + availability_delay,
        availability_basis=availability_basis,
        bid_price=price - 0.1,
        ask_price=price + 0.1,
        mid_price=price,
    )


def bullish_pivots():
    return (
        pivot(0, PivotKind.HIGH, 100.0),
        pivot(1, PivotKind.LOW, 80.0),
        pivot(2, PivotKind.HIGH, 110.0),
        pivot(3, PivotKind.LOW, 90.0),
    )


class RegimePolicyTests(unittest.TestCase):
    def test_policy_has_no_defaults_and_rejects_untyped_choices(self):
        with self.assertRaises(TypeError):
            RegimePolicy()
        with self.assertRaisesRegex(TypeError, "structure_rule"):
            RegimePolicy(
                structure_rule="strict_last_two_confirmed_high_low",
                insufficient_evidence=InsufficientEvidencePolicy.UNKNOWN,
                mixed_structure=MixedStructurePolicy.RANGE,
            )

    def test_policy_fingerprint_is_complete_versioned_sha256(self):
        selected = policy()
        self.assertEqual(
            selected.canonical_identity,
            ";".join(
                (
                    "h1-regime-policy-v1",
                    "structure_rule=strict_last_two_confirmed_high_low",
                    "insufficient_evidence=unknown",
                    "mixed_structure=range",
                )
            ),
        )
        self.assertEqual(len(selected.fingerprint), 64)
        self.assertTrue(
            all(character in "0123456789abcdef" for character in selected.fingerprint)
        )


class H1RegimeEventTests(unittest.TestCase):
    def test_insufficient_then_strict_bullish_structure(self):
        events = h1_regime_events(bullish_pivots(), policy=policy())

        self.assertEqual(len(events), 4)
        self.assertEqual(
            tuple(event.regime for event in events[:3]),
            (MarketRegime.UNKNOWN,) * 3,
        )
        final = events[-1]
        self.assertEqual(final.regime, MarketRegime.BULLISH)
        self.assertEqual(final.high_structure, StructureLeg.RISING)
        self.assertEqual(final.low_structure, StructureLeg.RISING)
        self.assertEqual(
            tuple(item.mid_price for item in final.high_pivots), (100.0, 110.0)
        )
        self.assertEqual(
            tuple(item.mid_price for item in final.low_pivots), (80.0, 90.0)
        )
        self.assertEqual(final.policy_fingerprint, policy().fingerprint)
        self.assertEqual(final.key[0], policy().fingerprint)
        with self.assertRaises(FrozenInstanceError):
            final.regime = MarketRegime.RANGE

    def test_strict_bearish_structure_is_mirrored(self):
        pivots = (
            pivot(0, PivotKind.HIGH, 120.0),
            pivot(1, PivotKind.LOW, 100.0),
            pivot(2, PivotKind.HIGH, 110.0),
            pivot(3, PivotKind.LOW, 90.0),
        )
        final = h1_regime_events(pivots, policy=policy())[-1]
        self.assertEqual(final.regime, MarketRegime.BEARISH)
        self.assertEqual(final.high_structure, StructureLeg.FALLING)
        self.assertEqual(final.low_structure, StructureLeg.FALLING)

    def test_mixed_and_equal_complete_structure_are_range(self):
        mixed = (
            pivot(0, PivotKind.HIGH, 100.0),
            pivot(1, PivotKind.LOW, 90.0),
            pivot(2, PivotKind.HIGH, 110.0),
            pivot(3, PivotKind.LOW, 80.0),
        )
        equal = (
            pivot(0, PivotKind.HIGH, 100.0),
            pivot(1, PivotKind.LOW, 80.0),
            pivot(2, PivotKind.HIGH, 100.0),
            pivot(3, PivotKind.LOW, 90.0),
        )

        mixed_event = h1_regime_events(mixed, policy=policy())[-1]
        equal_event = h1_regime_events(equal, policy=policy())[-1]
        self.assertEqual(mixed_event.regime, MarketRegime.RANGE)
        self.assertEqual(equal_event.regime, MarketRegime.RANGE)
        self.assertEqual(equal_event.high_structure, StructureLeg.EQUAL)

    def test_only_the_last_two_pivots_of_each_kind_are_compared(self):
        pivots = (
            pivot(0, PivotKind.HIGH, 120.0),
            pivot(1, PivotKind.LOW, 100.0),
            pivot(2, PivotKind.HIGH, 90.0),
            pivot(3, PivotKind.LOW, 70.0),
            pivot(4, PivotKind.HIGH, 95.0),
            pivot(5, PivotKind.LOW, 75.0),
        )

        final = h1_regime_events(pivots, policy=policy())[-1]

        self.assertEqual(final.regime, MarketRegime.BULLISH)
        self.assertEqual(
            tuple(item.mid_price for item in final.high_pivots), (90.0, 95.0)
        )
        self.assertEqual(
            tuple(item.mid_price for item in final.low_pivots), (70.0, 75.0)
        )

    def test_event_key_binds_complete_pivot_evidence_and_price_revisions(self):
        bullish = h1_regime_events(bullish_pivots(), policy=policy())[-1]
        newest_high = bullish.high_pivots[-1]

        self.assertEqual(
            bullish.key[2][-1],
            (
                newest_high.kind,
                newest_high.pivot_bar_start,
                newest_high.pivot_bar_end,
                newest_high.confirmed_at,
                newest_high.available_at,
                newest_high.availability_basis,
                newest_high.bid_price,
                newest_high.ask_price,
                newest_high.mid_price,
                newest_high.left_wing,
                newest_high.right_wing,
            ),
        )

        revised_pivots = list(bullish_pivots())
        revised_pivots[2] = replace(
            revised_pivots[2],
            bid_price=89.9,
            ask_price=90.1,
            mid_price=90.0,
        )
        revised = h1_regime_events(revised_pivots, policy=policy())[-1]

        self.assertEqual(bullish.regime, MarketRegime.BULLISH)
        self.assertEqual(revised.regime, MarketRegime.RANGE)
        self.assertNotEqual(bullish.key, revised.key)

    def test_exact_delayed_evidence_gates_visibility_and_basis(self):
        pivots = list(bullish_pivots())
        delayed_at = BASE + timedelta(hours=30)
        pivots[0] = replace(
            pivots[0],
            available_at=delayed_at,
            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
        )

        unfiltered = h1_regime_events(pivots, policy=policy())
        final = unfiltered[-1]
        self.assertEqual(final.available_at, delayed_at)
        self.assertEqual(
            final.availability_basis, AvailabilityBasis.OBSERVED_RECEIPT
        )
        visible_before = h1_regime_events(
            pivots,
            policy=policy(),
            as_of=delayed_at - timedelta(microseconds=1),
        )
        visible_at = h1_regime_events(
            pivots, policy=policy(), as_of=delayed_at
        )
        self.assertNotIn(final, visible_before)
        self.assertIn(final, visible_at)

    def test_superseded_pivot_is_not_an_availability_dependency(self):
        first_high = pivot(
            0,
            PivotKind.HIGH,
            120.0,
            availability_delay=timedelta(hours=100),
            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
        )
        pivots = (
            first_high,
            pivot(1, PivotKind.LOW, 70.0),
            pivot(2, PivotKind.HIGH, 90.0),
            pivot(3, PivotKind.LOW, 75.0),
            pivot(4, PivotKind.HIGH, 95.0),
        )

        final = h1_regime_events(pivots, policy=policy())[-1]

        self.assertEqual(final.regime, MarketRegime.BULLISH)
        self.assertEqual(
            tuple(item.mid_price for item in final.high_pivots), (90.0, 95.0)
        )
        self.assertLess(final.available_at, first_high.available_at)

    def test_same_center_high_and_low_are_one_atomic_snapshot(self):
        pivots = (
            pivot(0, PivotKind.HIGH, 100.0),
            pivot(1, PivotKind.LOW, 80.0),
            pivot(2, PivotKind.HIGH, 110.0, center=2),
            pivot(3, PivotKind.LOW, 90.0, center=2),
        )

        events = h1_regime_events(pivots, policy=policy())

        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1].regime, MarketRegime.BULLISH)
        self.assertEqual(events[-1].structure_bar_start, BASE + timedelta(hours=8))

    def test_as_of_requires_timezone_and_empty_input_is_empty(self):
        self.assertEqual(h1_regime_events((), policy=policy()), ())
        with self.assertRaisesRegex(ValueError, "timezone"):
            h1_regime_events(
                bullish_pivots(), policy=policy(), as_of=datetime(2026, 1, 5)
            )

    def test_future_suffix_does_not_rewrite_prefix_events(self):
        prefix = bullish_pivots()
        original = h1_regime_events(prefix, policy=policy())
        suffix = (
            pivot(4, PivotKind.HIGH, 105.0),
            pivot(5, PivotKind.LOW, 85.0),
        )

        extended = h1_regime_events(prefix + suffix, policy=policy())

        self.assertEqual(extended[: len(original)], original)

    def test_duplicate_and_out_of_order_pivots_fail_closed(self):
        first = pivot(0, PivotKind.HIGH, 100.0)
        with self.assertRaisesRegex(MarketRegimeDataError, "duplicate"):
            h1_regime_events((first, first), policy=policy())
        with self.assertRaisesRegex(MarketRegimeDataError, "ordered"):
            h1_regime_events(
                (pivot(1, PivotKind.LOW, 80.0), first), policy=policy()
            )

    def test_low_before_high_on_same_center_fails_closed(self):
        same_center = 1
        with self.assertRaisesRegex(MarketRegimeDataError, "high before low"):
            h1_regime_events(
                (
                    pivot(0, PivotKind.HIGH, 100.0),
                    pivot(1, PivotKind.LOW, 80.0, center=same_center),
                    pivot(2, PivotKind.HIGH, 110.0, center=same_center),
                ),
                policy=policy(),
            )

    def test_malformed_confirmation_contract_fails_closed(self):
        malformed = replace(
            pivot(0, PivotKind.HIGH, 100.0),
            confirmed_at=BASE + timedelta(hours=2),
            available_at=BASE + timedelta(hours=2),
        )
        with self.assertRaisesRegex(MarketRegimeDataError, "confirmation time"):
            h1_regime_events((malformed,), policy=policy())


if __name__ == "__main__":
    unittest.main()
