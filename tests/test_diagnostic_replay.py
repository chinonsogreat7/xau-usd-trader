import math
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from xau_trader.candidate_signals import CandidateReason
from xau_trader.diagnostic_replay import (
    DIAGNOSTIC_REPLAY_ENGINE_VERSION,
    DIAGNOSTIC_REPLAY_SCHEMA_VERSION,
    DiagnosticReplayDataError,
    ReplayHistoryBoundary,
    diagnostic_minimum_input_m15_bars,
    diagnostic_pre_roll_m15_bars,
    diagnostic_warmup_m15_bars,
    run_diagnostic_replay,
    validate_diagnostic_replay_result,
)
from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.m15_confirmation import (
    m15_candle_events,
    m15_confirmation_events,
    m15_ema_events,
)
from xau_trader.market_regime import h1_regime_events
from xau_trader.multitimeframe import (
    aggregate_m15_to_h1,
    confirmed_h1_pivots,
)
from xau_trader.research_baseline import provisional_diagnostic_baseline_v1
from xau_trader.supply_demand import (
    h1_atr_events,
    h1_impulse_events,
    supply_demand_zone_events,
)
from xau_trader.zone_lifecycle import EqualTimeOrder, ObservationTimeframe


UTC = timezone.utc
START = datetime(2026, 1, 5, tzinfo=UTC)
DATASET_FINGERPRINT = "a" * 64


def m15_series(
    count,
    *,
    receipt_delay=timedelta(0),
    availability_basis=AvailabilityBasis.SYNTHETIC,
):
    bars = []
    previous_close = 2_000.0
    for index in range(count):
        close = (
            2_000.0
            + 2.5 * math.sin(index / 5.0)
            + 0.7 * math.sin(index / 2.0)
            + 0.015 * index
        )
        open_price = previous_close
        padding = 0.45 + 0.08 * abs(math.sin(index / 3.0))
        bar_start = START + index * timedelta(minutes=15)
        bar_end = bar_start + timedelta(minutes=15)
        bars.append(
            QuoteBar.from_mid(
                start_time=bar_start,
                timestamp=bar_end,
                available_at=bar_end + receipt_delay,
                availability_basis=availability_basis,
                mid_open=open_price,
                mid_high=max(open_price, close) + padding,
                mid_low=min(open_price, close) - padding,
                mid_close=close,
                spread=0.2,
                volume=1_000.0 + index,
            )
        )
        previous_close = close
    return tuple(bars)


class DiagnosticReplayTests(unittest.TestCase):
    def setUp(self):
        self.bundle = provisional_diagnostic_baseline_v1()
        self.bars = m15_series(100)

    def test_composes_every_stream_and_one_decision_per_m15_transition(self):
        result = run_diagnostic_replay(
            self.bars,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
        )

        self.assertEqual(result.m15_bars, self.bars)
        self.assertEqual(result.h1_bars, aggregate_m15_to_h1(self.bars))
        self.assertEqual(len(result.h1_bars), 25)
        self.assertEqual(
            result.h1_atr,
            h1_atr_events(
                result.h1_bars,
                period=self.bundle.impulse_policy.atr_period,
                method=self.bundle.impulse_policy.atr_method,
            ),
        )
        self.assertEqual(
            result.h1_pivots,
            confirmed_h1_pivots(result.h1_bars),
        )
        self.assertEqual(
            result.h1_regimes,
            h1_regime_events(
                result.h1_pivots,
                policy=self.bundle.regime_policy,
            ),
        )
        self.assertEqual(
            result.h1_impulses,
            h1_impulse_events(
                result.h1_bars,
                policy=self.bundle.impulse_policy,
            ),
        )
        self.assertEqual(
            result.zones,
            supply_demand_zone_events(
                result.h1_bars,
                impulse_policy=self.bundle.impulse_policy,
                origin_policy=self.bundle.origin_policy,
                origin_lookback_bars=self.bundle.origin_lookback_bars,
            ),
        )
        self.assertEqual(
            result.m15_ema,
            m15_ema_events(
                self.bars,
                policy=self.bundle.confirmation_policy,
            ),
        )
        self.assertEqual(
            result.m15_candles,
            m15_candle_events(
                self.bars,
                policy=self.bundle.confirmation_policy,
            ),
        )
        self.assertEqual(
            result.m15_confirmations,
            m15_confirmation_events(
                self.bars,
                policy=self.bundle.confirmation_policy,
            ),
        )

        m15_transitions = tuple(
            transition
            for transition in result.lifecycle.transitions
            if transition.observation.timeframe == ObservationTimeframe.M15
        )
        self.assertEqual(len(result.lifecycle.transitions), 125)
        self.assertEqual(len(m15_transitions), 100)
        self.assertEqual(len(result.decisions), len(m15_transitions))
        self.assertEqual(
            tuple(decision.m15_bar_start for decision in result.decisions),
            tuple(
                transition.observation.bar.start_time
                for transition in m15_transitions
            ),
        )
        self.assertEqual(result.policy_bundle_fingerprint, self.bundle.fingerprint)
        self.assertEqual(len(result.input_bars_fingerprint), 64)
        self.assertEqual(len(result.fingerprint), 64)

    def test_readiness_marks_pre_roll_and_empty_dataset_start_boundary(self):
        result = run_diagnostic_replay(
            self.bars,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
        )

        self.assertEqual(
            result.readiness.history_boundary,
            ReplayHistoryBoundary.EMPTY_STATE_AT_DATASET_START,
        )
        self.assertEqual(result.readiness.pre_roll_m15_bars, 96)
        self.assertEqual(result.readiness.minimum_input_m15_bars, 100)
        self.assertEqual(
            result.readiness.post_pre_roll_m15_start,
            START + timedelta(hours=24),
        )
        self.assertEqual(result.readiness.pre_roll_decision_count, 96)
        self.assertEqual(result.readiness.post_pre_roll_decision_count, 4)
        self.assertEqual(len(result.pre_roll_decisions), 96)
        self.assertEqual(len(result.post_pre_roll_decisions), 4)
        self.assertTrue(
            all(
                decision.m15_bar_start
                < result.readiness.post_pre_roll_m15_start
                for decision in result.pre_roll_decisions
            )
        )
        self.assertTrue(
            all(
                decision.m15_bar_start
                >= result.readiness.post_pre_roll_m15_start
                for decision in result.post_pre_roll_decisions
            )
        )

    def test_explicit_equal_time_group_uses_bound_h1_first_policy(self):
        result = run_diagnostic_replay(
            self.bars,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
        )
        first_hour = START + timedelta(hours=1)
        same_receipt = tuple(
            transition.observation.timeframe
            for transition in result.lifecycle.transitions
            if transition.observation.bar.available_at == first_hour
        )

        self.assertEqual(
            same_receipt,
            (ObservationTimeframe.H1, ObservationTimeframe.M15),
        )
        group = next(
            group
            for group in result.availability_groups
            if group.available_at == first_hour
        )
        self.assertEqual(group.sealed_through, first_hour)
        self.assertEqual(len(group.observations), 2)

    def test_detected_formations_are_admitted_at_their_exact_receipt_groups(self):
        impulse_policy = replace(
            self.bundle.impulse_policy,
            atr_period=2,
            minimum_directional_bars=1,
            movement_atr_multiple=0.0,
            body_atr_multiple=0.0,
        )
        candidate_policy = replace(
            self.bundle.candidate_policy,
            expected_impulse_policy_fingerprint=impulse_policy.fingerprint,
            expected_origin_lookback_bars=4,
        )
        bundle = replace(
            self.bundle,
            impulse_policy=impulse_policy,
            origin_lookback_bars=4,
            candidate_policy=candidate_policy,
        )

        result = run_diagnostic_replay(
            m15_series(40),
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=bundle,
        )

        self.assertTrue(result.h1_impulses)
        self.assertTrue(result.zones)
        grouped = tuple(
            zone
            for group in result.availability_groups
            for zone in group.formations
        )
        self.assertEqual(grouped, result.zones)
        self.assertEqual(
            {state.zone.key for state in result.lifecycle.book.states},
            {zone.key for zone in result.zones},
        )
        self.assertTrue(result.lifecycle.events)
        self.assertTrue(
            all(
                zone.impulse_key[0] == impulse_policy.fingerprint
                for zone in result.zones
            )
        )

    def test_explicit_equal_time_group_can_use_bound_m15_first_policy(self):
        lifecycle_policy = replace(
            self.bundle.lifecycle_policy,
            equal_time_order=EqualTimeOrder.M15_TOUCH_THEN_H1_INVALIDATION,
        )
        bundle = replace(self.bundle, lifecycle_policy=lifecycle_policy)

        result = run_diagnostic_replay(
            self.bars,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=bundle,
        )
        first_hour = START + timedelta(hours=1)
        same_receipt = tuple(
            transition.observation.timeframe
            for transition in result.lifecycle.transitions
            if transition.observation.bar.available_at == first_hour
        )

        self.assertEqual(
            same_receipt,
            (ObservationTimeframe.M15, ObservationTimeframe.H1),
        )

    def test_as_of_ignores_unavailable_future_suffix_exactly(self):
        extended = m15_series(116)
        cutoff = START + timedelta(hours=25)

        from_prefix = run_diagnostic_replay(
            extended[:100],
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
            as_of=cutoff,
        )
        from_future_suffix = run_diagnostic_replay(
            extended,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
            as_of=cutoff,
        )

        self.assertEqual(from_future_suffix, from_prefix)
        self.assertTrue(
            all(
                decision.transition_sealed_through <= cutoff
                for decision in from_future_suffix.decisions
            )
        )

    def test_late_receipts_cannot_be_backdated_to_next_open(self):
        with self.assertRaisesRegex(DiagnosticReplayDataError, "must equal its end"):
            run_diagnostic_replay(
                m15_series(100, receipt_delay=timedelta(minutes=5)),
                dataset_fingerprint=DATASET_FINGERPRINT,
                policy_bundle=self.bundle,
            )
        delayed = m15_series(
            100,
            receipt_delay=timedelta(minutes=5),
            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
        )
        result = run_diagnostic_replay(
            delayed,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
        )

        self.assertTrue(result.decisions)
        self.assertTrue(
            all(
                decision.reasons
                == (CandidateReason.M15_TRANSITION_LATE_FOR_NEXT_OPEN,)
                for decision in result.decisions
            )
        )
        self.assertTrue(
            all(decision.proposed_entry_at is None for decision in result.decisions)
        )

    def test_rejects_insufficient_history_gap_partial_hour_and_mixed_provenance(self):
        pre_roll = diagnostic_pre_roll_m15_bars(self.bundle)
        minimum = diagnostic_minimum_input_m15_bars(self.bundle)
        self.assertEqual(pre_roll, 96)
        self.assertEqual(diagnostic_warmup_m15_bars(self.bundle), pre_roll)
        self.assertEqual(minimum, 100)
        with self.assertRaisesRegex(DiagnosticReplayDataError, "insufficient history"):
            run_diagnostic_replay(
                m15_series(96),
                dataset_fingerprint=DATASET_FINGERPRINT,
                policy_bundle=self.bundle,
            )
        with self.assertRaisesRegex(DiagnosticReplayDataError, "contiguous"):
            run_diagnostic_replay(
                self.bars[:30] + self.bars[31:],
                dataset_fingerprint=DATASET_FINGERPRINT,
                policy_bundle=self.bundle,
            )
        with self.assertRaisesRegex(DiagnosticReplayDataError, "end on a UTC hour"):
            run_diagnostic_replay(
                m15_series(101),
                dataset_fingerprint=DATASET_FINGERPRINT,
                policy_bundle=self.bundle,
            )
        mixed = self.bars[:-1] + (
            replace(
                self.bars[-1],
                availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
            ),
        )
        with self.assertRaisesRegex(DiagnosticReplayDataError, "mixes"):
            run_diagnostic_replay(
                mixed,
                dataset_fingerprint=DATASET_FINGERPRINT,
                policy_bundle=self.bundle,
            )

    def test_rejects_visible_bar_behind_unavailable_receipt(self):
        cutoff = START + timedelta(hours=25)
        delayed_early = replace(
            self.bars[20],
            available_at=cutoff + timedelta(hours=1),
        )
        out_of_order_receipts = self.bars[:20] + (delayed_early,) + self.bars[21:]

        with self.assertRaisesRegex(DiagnosticReplayDataError, "chronological prefix"):
            run_diagnostic_replay(
                out_of_order_receipts,
                dataset_fingerprint=DATASET_FINGERPRINT,
                policy_bundle=self.bundle,
                as_of=cutoff,
            )

    def test_data_and_policy_identities_are_bound_and_result_is_immutable(self):
        result = run_diagnostic_replay(
            self.bars,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
        )
        volume_changed = self.bars[:-1] + (
            replace(self.bars[-1], volume=self.bars[-1].volume + 1.0),
        )
        changed = run_diagnostic_replay(
            volume_changed,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
        )

        self.assertNotEqual(
            result.input_bars_fingerprint,
            changed.input_bars_fingerprint,
        )
        self.assertNotEqual(result.fingerprint, changed.fingerprint)
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            run_diagnostic_replay(
                self.bars,
                dataset_fingerprint="not-a-hash",
                policy_bundle=self.bundle,
            )
        with self.assertRaises(FrozenInstanceError):
            result.dataset_fingerprint = "b" * 64

    def test_fingerprint_binds_derived_content_and_exact_validator_recomputes(self):
        result = run_diagnostic_replay(
            self.bars,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
        )
        changed_atr = replace(result.h1_atr[0], value=result.h1_atr[0].value + 0.01)
        forged = replace(
            result,
            h1_atr=(changed_atr,) + result.h1_atr[1:],
        )

        self.assertNotEqual(forged.evidence_fingerprint, result.evidence_fingerprint)
        self.assertNotEqual(forged.fingerprint, result.fingerprint)
        self.assertIn(DIAGNOSTIC_REPLAY_ENGINE_VERSION, result.canonical_identity)
        self.assertIn(
            "diagnostic_replay_schema={}".format(
                DIAGNOSTIC_REPLAY_SCHEMA_VERSION
            ),
            result.canonical_identity,
        )
        self.assertIsNone(validate_diagnostic_replay_result(result))
        with self.assertRaisesRegex(
            DiagnosticReplayDataError,
            "exact recomputation",
        ):
            validate_diagnostic_replay_result(forged)

    def test_as_of_and_provenance_validation_cover_derived_streams(self):
        cutoff = START + timedelta(hours=25)
        result = run_diagnostic_replay(
            self.bars,
            dataset_fingerprint=DATASET_FINGERPRINT,
            policy_bundle=self.bundle,
            as_of=cutoff,
        )
        future_atr = replace(
            result.h1_atr[0],
            available_at=cutoff + timedelta(microseconds=1),
        )
        with self.assertRaisesRegex(ValueError, "after as_of"):
            replace(
                result,
                h1_atr=(future_atr,) + result.h1_atr[1:],
            )

        mixed_atr = replace(
            result.h1_atr[0],
            availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
        )
        with self.assertRaisesRegex(DiagnosticReplayDataError, "mixes"):
            replace(
                result,
                h1_atr=(mixed_atr,) + result.h1_atr[1:],
            )


if __name__ == "__main__":
    unittest.main()
