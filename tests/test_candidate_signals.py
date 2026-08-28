import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from xau_trader.candidate_signals import (
    CandidateAction,
    CandidateDataError,
    CandidatePolicy,
    CandidateReason,
    EntryTiming,
    RegimeAlignment,
    RetestValidity,
    paper_candidate_decisions,
)
from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.m15_confirmation import (
    CandleCombination,
    CandlePatternKind,
    ConfirmationEvent,
    ConfirmationPolicy,
    CrossoverTiming,
    DisplacementHistory,
    DojiSemantics,
    EmaInitialization,
    EngulfingEquality,
    MedianConvention,
    TouchBarConfirmation,
)
from xau_trader.market_regime import (
    InsufficientEvidencePolicy,
    MarketRegime,
    MixedStructurePolicy,
    RegimePolicy,
    StructureRule,
    h1_regime_events,
)
from xau_trader.multitimeframe import H1PivotEvent, PivotKind
from xau_trader.supply_demand import (
    ImpulseDirection,
    OriginSelectionPolicy,
    ZoneEvent,
    ZoneKind,
)
from xau_trader.zone_lifecycle import (
    CompletedBarObservation,
    EqualTimeOrder,
    ObservationTimeframe,
    OverlapSelection,
    RetestPolicy,
    TouchEpisodePolicy,
    ZoneLifecyclePolicy,
    ZonePriceBasis,
    evolve_zone_book,
)


UTC = timezone.utc
BASE = datetime(2026, 1, 5, tzinfo=UTC)
FORMED = BASE + timedelta(hours=5)
IMPULSE_FINGERPRINT = "1" * 64


def lifecycle_policy(
    order=EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH,
    selection=OverlapSelection.NEWEST_ORIGIN_THEN_ZONE_KEY,
):
    return ZoneLifecyclePolicy(
        retest_policy=RetestPolicy.FIRST_TOUCH_ONLY,
        touch_episode=TouchEpisodePolicy.CONTIGUOUS_INCLUSIVE_OVERLAP,
        equal_time_order=order,
        overlap_selection=selection,
        price_basis=ZonePriceBasis.MID,
    )


def regime_policy():
    return RegimePolicy(
        structure_rule=StructureRule.STRICT_LAST_TWO_CONFIRMED_HIGH_LOW,
        insufficient_evidence=InsufficientEvidencePolicy.UNKNOWN,
        mixed_structure=MixedStructurePolicy.RANGE,
    )


def confirmation_policy(
    *,
    touch=TouchBarConfirmation.TOUCH_BAR_ALLOWED,
    expiry=2,
):
    return ConfirmationPolicy(
        ema_period=8,
        ema_initialization=EmaInitialization.FIRST_CLOSE_SEED,
        crossover_timing=CrossoverTiming.SAME_BAR_EMA,
        engulfing_equality=EngulfingEquality.INCLUSIVE,
        displacement_lookback_bars=2,
        displacement_median=MedianConvention.MEAN_OF_MIDDLE_TWO,
        displacement_history=DisplacementHistory.PREVIOUS_BARS_ONLY,
        displacement_body_multiple=1.2,
        doji_semantics=DojiSemantics.NEITHER_DIRECTION,
        candle_combination=CandleCombination.ENGULFING_ONLY,
        touch_bar_confirmation=touch,
        confirmation_expiry_bars=expiry,
    )


def candidate_policy():
    return CandidatePolicy(
        expected_impulse_policy_fingerprint=IMPULSE_FINGERPRINT,
        expected_origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
        expected_origin_lookback_bars=20,
        entry_timing=EntryTiming.NEXT_M15_OPEN_ONLY,
        regime_alignment=RegimeAlignment.STRICT_DIRECTIONAL,
        retest_validity=RetestValidity.ZONE_VALID_AT_CONFIRMATION_STEP,
    )


def zone(
    *,
    kind=ZoneKind.DEMAND,
    origin_hour=0,
    lower=100.0,
    upper=110.0,
    fingerprint=IMPULSE_FINGERPRINT,
):
    direction = (
        ImpulseDirection.BULLISH
        if kind == ZoneKind.DEMAND
        else ImpulseDirection.BEARISH
    )
    origin_start = BASE + timedelta(hours=origin_hour)
    return ZoneEvent(
        kind=kind,
        origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
        origin_lookback_bars=20,
        origin_bar_start=origin_start,
        origin_bar_end=origin_start + timedelta(hours=1),
        impulse_window_start=FORMED - timedelta(hours=4),
        impulse_window_end=FORMED,
        formed_at=FORMED,
        available_at=FORMED,
        availability_basis=AvailabilityBasis.SYNTHETIC,
        lower_price=lower,
        upper_price=upper,
        impulse_key=(
            fingerprint,
            direction,
            FORMED - timedelta(hours=4),
            FORMED,
        ),
    )


def quote_bar(start, duration, *, low, high, close=None, available_at=None):
    midpoint = (low + high) / 2.0 if close is None else close
    end = start + duration
    return QuoteBar.from_mid(
        start_time=start,
        timestamp=end,
        available_at=end if available_at is None else available_at,
        availability_basis=AvailabilityBasis.SYNTHETIC,
        mid_open=midpoint,
        mid_high=high,
        mid_low=low,
        mid_close=midpoint,
        spread=0.2,
        volume=1.0,
    )


def m15(index, *, low, high, close=None, available_at=None):
    return quote_bar(
        FORMED + index * timedelta(minutes=15),
        timedelta(minutes=15),
        low=low,
        high=high,
        close=close,
        available_at=available_at,
    )


def h1(index, *, low, high, close, available_at=None):
    return quote_bar(
        FORMED + index * timedelta(hours=1),
        timedelta(hours=1),
        low=low,
        high=high,
        close=close,
        available_at=available_at,
    )


def observation(timeframe, bar):
    return CompletedBarObservation(timeframe=timeframe, bar=bar)


def lifecycle(zones, observations, selected_policy=None):
    return evolve_zone_book(
        zones,
        observations,
        lifecycle_policy() if selected_policy is None else selected_policy,
        h1_start=FORMED,
        m15_start=FORMED,
    )


def pivot(kind, start, price, available_at):
    end = start + timedelta(hours=1)
    return H1PivotEvent(
        kind=kind,
        pivot_bar_start=start,
        pivot_bar_end=end,
        confirmed_at=end + timedelta(hours=3),
        available_at=available_at,
        availability_basis=AvailabilityBasis.SYNTHETIC,
        bid_price=price - 0.1,
        ask_price=price + 0.1,
        mid_price=price,
    )


def regime_event(regime, available_at, *, generation=0):
    anchor = BASE - timedelta(days=3) + generation * timedelta(hours=8)
    if regime == MarketRegime.BULLISH:
        values = ((PivotKind.LOW, 80.0), (PivotKind.HIGH, 100.0),
                  (PivotKind.LOW, 90.0), (PivotKind.HIGH, 110.0))
    elif regime == MarketRegime.BEARISH:
        values = ((PivotKind.LOW, 90.0), (PivotKind.HIGH, 110.0),
                  (PivotKind.LOW, 80.0), (PivotKind.HIGH, 100.0))
    elif regime == MarketRegime.RANGE:
        values = ((PivotKind.LOW, 90.0), (PivotKind.HIGH, 100.0),
                  (PivotKind.LOW, 80.0), (PivotKind.HIGH, 110.0))
    elif regime == MarketRegime.UNKNOWN:
        values = ((PivotKind.HIGH, 100.0),)
    else:
        raise ValueError("unsupported test regime")
    pivots = tuple(
        pivot(kind, anchor + index * timedelta(hours=1), price, available_at)
        for index, (kind, price) in enumerate(values)
    )
    events = h1_regime_events(pivots, policy=regime_policy())
    self_event = events[-1]
    if self_event.regime != regime:
        raise AssertionError("test regime helper produced the wrong classification")
    return self_event


def confirmation(bar, *, direction=ImpulseDirection.BULLISH, policy=None, available_at=None):
    selected = confirmation_policy() if policy is None else policy
    if direction == ImpulseDirection.BULLISH:
        prior_close, prior_ema = 99.0, 100.0
        current_close, current_ema = 102.0, 101.0
    else:
        prior_close, prior_ema = 102.0, 101.0
        current_close, current_ema = 99.0, 100.0
    return ConfirmationEvent(
        direction=direction,
        bar_start=bar.start_time,
        bar_end=bar.timestamp,
        confirmed_at=bar.timestamp,
        available_at=bar.timestamp if available_at is None else available_at,
        availability_basis=AvailabilityBasis.SYNTHETIC,
        policy_fingerprint=selected.fingerprint,
        ema_period=selected.ema_period,
        ema_initialization=selected.ema_initialization,
        crossover_timing=selected.crossover_timing,
        prior_close=prior_close,
        prior_ema=prior_ema,
        current_close=current_close,
        current_ema=current_ema,
        current_crossover_reference=current_ema,
        proving_patterns=(CandlePatternKind.ENGULFING,),
        candle_combination=selected.candle_combination,
        touch_bar_confirmation=selected.touch_bar_confirmation,
        confirmation_expiry_bars=selected.confirmation_expiry_bars,
    )


def decide(book_update, regimes, confirmations, *, cpolicy=None, as_of=None):
    selected_confirmation = confirmation_policy() if cpolicy is None else cpolicy
    return paper_candidate_decisions(
        book_update,
        regimes,
        confirmations,
        candidate_policy=candidate_policy(),
        regime_policy=regime_policy(),
        confirmation_policy=selected_confirmation,
        as_of=as_of,
    )


class CandidateContractTests(unittest.TestCase):
    def test_emits_one_immutable_next_open_buy_and_one_no_trade_per_m15_step(self):
        first = m15(0, low=100.0, high=105.0)
        second = m15(1, low=100.0, high=105.0)
        update = lifecycle(
            (zone(),),
            (
                observation(ObservationTimeframe.M15, first),
                observation(ObservationTimeframe.M15, second),
            ),
        )
        result = decide(
            update,
            (regime_event(MarketRegime.BULLISH, FORMED),),
            (confirmation(first),),
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].action, CandidateAction.BUY)
        self.assertEqual(result[0].reasons, (CandidateReason.BUY_CRITERIA_MET,))
        self.assertEqual(result[0].proposed_entry_at, first.timestamp)
        self.assertEqual(result[0].next_m15_open, first.timestamp)
        self.assertLessEqual(result[0].available_at, result[0].proposed_entry_at)
        self.assertEqual(result[1].action, CandidateAction.NO_TRADE)
        self.assertEqual(result[1].reasons, (CandidateReason.NO_TIMELY_CONFIRMATION,))
        self.assertIsNone(result[1].proposed_entry_at)
        self.assertEqual(len(result[0].policy_fingerprint), 64)
        with self.assertRaises(Exception):
            result[0].action = CandidateAction.NO_TRADE

    def test_bearish_supply_alignment_emits_sell(self):
        bar = m15(0, low=105.0, high=115.0)
        update = lifecycle(
            (zone(kind=ZoneKind.SUPPLY),),
            (observation(ObservationTimeframe.M15, bar),),
        )
        result = decide(
            update,
            (regime_event(MarketRegime.BEARISH, FORMED),),
            (confirmation(bar, direction=ImpulseDirection.BEARISH),),
        )[0]

        self.assertEqual(result.action, CandidateAction.SELL)
        self.assertEqual(result.reasons, (CandidateReason.SELL_CRITERIA_MET,))
        self.assertEqual(result.selected_retests[0].zone.kind, ZoneKind.SUPPLY)

    def test_unknown_range_and_opposite_regimes_fail_closed(self):
        bar = m15(0, low=100.0, high=105.0)
        update = lifecycle(
            (zone(),), (observation(ObservationTimeframe.M15, bar),)
        )
        expected = (
            (MarketRegime.UNKNOWN, CandidateReason.H1_REGIME_UNKNOWN),
            (MarketRegime.RANGE, CandidateReason.H1_REGIME_RANGE),
            (
                MarketRegime.BEARISH,
                CandidateReason.H1_REGIME_DIRECTION_MISMATCH,
            ),
        )

        for value, reason in expected:
            with self.subTest(regime=value):
                decision = decide(
                    update,
                    (regime_event(value, FORMED),),
                    (confirmation(bar),),
                )[0]
                self.assertEqual(decision.action, CandidateAction.NO_TRADE)
                self.assertIn(reason, decision.reasons)
                self.assertIsNone(decision.proposed_entry_at)

    def test_confirmation_and_retest_direction_must_match(self):
        bar = m15(0, low=100.0, high=105.0)
        update = lifecycle(
            (zone(kind=ZoneKind.SUPPLY),),
            (observation(ObservationTimeframe.M15, bar),),
        )
        decision = decide(
            update,
            (regime_event(MarketRegime.BULLISH, FORMED),),
            (confirmation(bar),),
        )[0]

        self.assertEqual(decision.action, CandidateAction.NO_TRADE)
        self.assertIn(
            CandidateReason.NO_ELIGIBLE_DIRECTIONAL_RETEST,
            decision.reasons,
        )

    def test_lifecycle_overlap_selection_deterministically_selects_zone_evidence(self):
        bar = m15(0, low=100.0, high=105.0)
        older = zone(origin_hour=-1)
        newer = zone(origin_hour=0)
        regimes = (regime_event(MarketRegime.BULLISH, FORMED),)
        event = confirmation(bar)

        newest_update = lifecycle(
            (older, newer),
            (observation(ObservationTimeframe.M15, bar),),
            lifecycle_policy(
                selection=OverlapSelection.NEWEST_ORIGIN_THEN_ZONE_KEY
            ),
        )
        newest_decision = decide(newest_update, regimes, (event,))[0]
        self.assertEqual(newest_decision.action, CandidateAction.BUY)
        self.assertEqual(len(newest_decision.selected_retests), 1)
        self.assertEqual(
            newest_decision.selected_retests[0].zone.origin_bar_start,
            newer.origin_bar_start,
        )

        all_update = lifecycle(
            (older, newer),
            (observation(ObservationTimeframe.M15, bar),),
            lifecycle_policy(selection=OverlapSelection.ALL_ELIGIBLE),
        )
        all_decision = decide(all_update, regimes, (event,))[0]
        self.assertEqual(all_decision.action, CandidateAction.BUY)
        self.assertEqual(len(all_decision.selected_retests), 2)


class TimingAndOrderingTests(unittest.TestCase):
    def test_late_sealed_m15_transition_cannot_propose_the_missed_open(self):
        ordinary = m15(0, low=100.0, high=105.0)
        delayed_bar = m15(
            0,
            low=100.0,
            high=105.0,
            available_at=ordinary.timestamp + timedelta(minutes=1),
        )
        update = lifecycle(
            (zone(),),
            (observation(ObservationTimeframe.M15, delayed_bar),),
        )
        decision = decide(
            update,
            (regime_event(MarketRegime.BULLISH, FORMED),),
            (),
        )[0]

        self.assertEqual(decision.action, CandidateAction.NO_TRADE)
        self.assertEqual(
            decision.reasons,
            (CandidateReason.M15_TRANSITION_LATE_FOR_NEXT_OPEN,),
        )
        self.assertIsNone(decision.proposed_entry_at)
        self.assertGreater(decision.available_at, decision.next_m15_open)

    def test_no_visible_regime_is_an_explicit_no_trade_reason(self):
        bar = m15(0, low=100.0, high=105.0)
        update = lifecycle(
            (zone(),), (observation(ObservationTimeframe.M15, bar),)
        )
        decision = decide(update, (), (confirmation(bar),))[0]

        self.assertEqual(decision.action, CandidateAction.NO_TRADE)
        self.assertIn(CandidateReason.NO_VISIBLE_H1_REGIME, decision.reasons)
        self.assertTrue(decision.selected_retests)

    def test_conflicting_timely_confirmations_fail_to_no_trade(self):
        bar = m15(0, low=100.0, high=105.0)
        update = lifecycle(
            (zone(),), (observation(ObservationTimeframe.M15, bar),)
        )
        bullish = confirmation(bar, direction=ImpulseDirection.BULLISH)
        bearish = confirmation(bar, direction=ImpulseDirection.BEARISH)
        decision = decide(update, (), (bullish, bearish))[0]

        self.assertEqual(decision.action, CandidateAction.NO_TRADE)
        self.assertEqual(
            decision.reasons,
            (CandidateReason.CONFLICTING_TIMELY_CONFIRMATIONS,),
        )
        self.assertIsNone(decision.direction)
        self.assertIsNone(decision.proposed_entry_at)

    def test_delayed_confirmation_never_backdates_or_rewrites_no_trade(self):
        bar = m15(0, low=100.0, high=105.0)
        update = lifecycle(
            (zone(),), (observation(ObservationTimeframe.M15, bar),)
        )
        regimes = (regime_event(MarketRegime.BULLISH, FORMED),)
        without_event = decide(update, regimes, ())
        delayed = confirmation(
            bar,
            available_at=bar.timestamp + timedelta(minutes=1),
        )
        with_late_event = decide(update, regimes, (delayed,))

        self.assertEqual(with_late_event, without_event)
        self.assertEqual(with_late_event[0].action, CandidateAction.NO_TRADE)
        self.assertEqual(
            with_late_event[0].reasons,
            (CandidateReason.NO_TIMELY_CONFIRMATION,),
        )
        self.assertIsNone(with_late_event[0].proposed_entry_at)

    def test_touch_bar_exclusion_and_expiry_are_explicit(self):
        touch_only = confirmation_policy(
            touch=TouchBarConfirmation.AFTER_TOUCH_BAR_ONLY,
            expiry=1,
        )
        first = m15(0, low=100.0, high=105.0)
        first_update = lifecycle(
            (zone(),), (observation(ObservationTimeframe.M15, first),)
        )
        excluded = decide(
            first_update,
            (regime_event(MarketRegime.BULLISH, FORMED),),
            (confirmation(first, policy=touch_only),),
            cpolicy=touch_only,
        )[0]
        self.assertEqual(excluded.action, CandidateAction.NO_TRADE)
        self.assertIn(
            CandidateReason.TOUCH_BAR_CONFIRMATION_EXCLUDED,
            excluded.reasons,
        )

        expiry_policy = confirmation_policy(expiry=1)
        bars = (
            m15(0, low=100.0, high=105.0),
            m15(1, low=90.0, high=99.0),
            m15(2, low=90.0, high=99.0),
        )
        expiry_update = lifecycle(
            (zone(),),
            tuple(observation(ObservationTimeframe.M15, bar) for bar in bars),
        )
        expired = decide(
            expiry_update,
            (regime_event(MarketRegime.BULLISH, FORMED),),
            (confirmation(bars[2], policy=expiry_policy),),
            cpolicy=expiry_policy,
        )[2]
        self.assertEqual(expired.action, CandidateAction.NO_TRADE)
        self.assertIn(CandidateReason.CONFIRMATION_WINDOW_EXPIRED, expired.reasons)

    def test_h1_first_invalidation_blocks_touch_but_m15_first_snapshot_can_buy(self):
        shared_availability = FORMED + timedelta(hours=1)
        bars = (
            m15(0, low=90.0, high=99.0),
            m15(1, low=90.0, high=99.0),
            m15(2, low=90.0, high=99.0),
            m15(3, low=100.0, high=105.0, available_at=shared_availability),
        )
        h1_bar = h1(
            0,
            low=90.0,
            high=105.0,
            close=99.0,
            available_at=shared_availability,
        )
        observations = tuple(
            observation(ObservationTimeframe.M15, bar) for bar in bars[:3]
        ) + (
            observation(ObservationTimeframe.H1, h1_bar),
            observation(ObservationTimeframe.M15, bars[3]),
        )
        regimes = (
            regime_event(
                MarketRegime.BULLISH,
                shared_availability - timedelta(minutes=1),
            ),
        )
        event = confirmation(bars[3])

        h1_first = lifecycle(
            (zone(),),
            observations,
            lifecycle_policy(
                order=EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH
            ),
        )
        blocked = decide(h1_first, regimes, (event,))[3]
        self.assertEqual(blocked.action, CandidateAction.NO_TRADE)
        self.assertIn(CandidateReason.ZONE_INVALID_AT_M15_STEP, blocked.reasons)

        m15_first = lifecycle(
            (zone(),),
            observations,
            lifecycle_policy(
                order=EqualTimeOrder.M15_TOUCH_THEN_H1_INVALIDATION
            ),
        )
        accepted = decide(m15_first, regimes, (event,))[3]
        self.assertEqual(accepted.action, CandidateAction.BUY)
        self.assertTrue(accepted.selected_retests)

    def test_equal_time_regime_is_visible_only_under_h1_first_policy(self):
        bar = m15(0, low=100.0, high=105.0)
        older = regime_event(
            MarketRegime.BEARISH,
            bar.timestamp - timedelta(minutes=1),
            generation=0,
        )
        tied = regime_event(
            MarketRegime.BULLISH,
            bar.available_at,
            generation=1,
        )
        regimes = (older, tied)
        event = confirmation(bar)

        h1_first = lifecycle(
            (zone(),),
            (observation(ObservationTimeframe.M15, bar),),
            lifecycle_policy(
                order=EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH
            ),
        )
        h1_decision = decide(h1_first, regimes, (event,))[0]
        self.assertEqual(h1_decision.action, CandidateAction.BUY)
        self.assertEqual(h1_decision.regime.key, tied.key)

        m15_first = lifecycle(
            (zone(),),
            (observation(ObservationTimeframe.M15, bar),),
            lifecycle_policy(
                order=EqualTimeOrder.M15_TOUCH_THEN_H1_INVALIDATION
            ),
        )
        m15_decision = decide(m15_first, regimes, (event,))[0]
        self.assertEqual(m15_decision.action, CandidateAction.NO_TRADE)
        self.assertEqual(m15_decision.regime.key, older.key)
        self.assertIn(
            CandidateReason.H1_REGIME_DIRECTION_MISMATCH,
            m15_decision.reasons,
        )

    def test_delayed_older_regime_cannot_roll_structure_backward(self):
        bar = m15(0, low=100.0, high=105.0)
        delayed_older = regime_event(
            MarketRegime.BEARISH,
            bar.timestamp - timedelta(minutes=1),
            generation=0,
        )
        newer_structure = regime_event(
            MarketRegime.BULLISH,
            bar.timestamp - timedelta(minutes=2),
            generation=1,
        )
        update = lifecycle(
            (zone(),), (observation(ObservationTimeframe.M15, bar),)
        )

        decision = decide(
            update,
            (delayed_older, newer_structure),
            (confirmation(bar),),
        )[0]

        self.assertEqual(decision.action, CandidateAction.BUY)
        self.assertEqual(decision.regime.key, newer_structure.key)

    def test_as_of_and_future_suffix_do_not_rewrite_prior_decision(self):
        first = m15(0, low=100.0, high=105.0)
        second = m15(1, low=100.0, high=105.0)
        regimes = (regime_event(MarketRegime.BULLISH, FORMED),)
        base = lifecycle(
            (zone(),), (observation(ObservationTimeframe.M15, first),)
        )
        extended = lifecycle(
            (zone(),),
            (
                observation(ObservationTimeframe.M15, first),
                observation(ObservationTimeframe.M15, second),
            ),
        )
        first_event = confirmation(first)
        second_event = confirmation(second)
        base_result = decide(base, regimes, (first_event,))
        extended_result = decide(
            extended, regimes, (first_event, second_event)
        )

        self.assertEqual(extended_result[:1], base_result)
        self.assertEqual(
            decide(
                extended,
                regimes,
                (first_event, second_event),
                as_of=first.timestamp,
            ),
            base_result,
        )


class ProvenanceTests(unittest.TestCase):
    def test_mixed_formation_and_event_policy_fingerprints_are_rejected(self):
        bar = m15(0, low=100.0, high=105.0)
        regimes = (regime_event(MarketRegime.BULLISH, FORMED),)
        normal = lifecycle(
            (zone(),), (observation(ObservationTimeframe.M15, bar),)
        )

        with self.assertRaisesRegex(CandidateDataError, "confirmation event policy"):
            decide(
                normal,
                regimes,
                (replace(confirmation(bar), policy_fingerprint="2" * 64),),
            )
        with self.assertRaisesRegex(CandidateDataError, "regime event policy"):
            decide(
                normal,
                (replace(regimes[0], policy_fingerprint="2" * 64),),
                (confirmation(bar),),
            )

        mixed = lifecycle(
            (zone(fingerprint="2" * 64),),
            (observation(ObservationTimeframe.M15, bar),),
        )
        with self.assertRaisesRegex(CandidateDataError, "mixed or unexpected"):
            decide(mixed, regimes, (confirmation(bar),))

    def test_candidate_policy_has_no_defaults_and_binds_all_upstream_policies(self):
        with self.assertRaises(TypeError):
            CandidatePolicy()  # type: ignore
        bar = m15(0, low=100.0, high=105.0)
        regimes = (regime_event(MarketRegime.BULLISH, FORMED),)
        first_lifecycle = lifecycle(
            (zone(),),
            (observation(ObservationTimeframe.M15, bar),),
            lifecycle_policy(
                selection=OverlapSelection.NEWEST_ORIGIN_THEN_ZONE_KEY
            ),
        )
        second_lifecycle = lifecycle(
            (zone(),),
            (observation(ObservationTimeframe.M15, bar),),
            lifecycle_policy(selection=OverlapSelection.ALL_ELIGIBLE),
        )
        first = decide(first_lifecycle, regimes, (confirmation(bar),))[0]
        second = decide(second_lifecycle, regimes, (confirmation(bar),))[0]

        self.assertNotEqual(first.policy_fingerprint, second.policy_fingerprint)
        self.assertNotEqual(
            first.lifecycle_policy_fingerprint,
            second.lifecycle_policy_fingerprint,
        )

    def test_confirmation_without_exact_lifecycle_bar_is_rejected(self):
        first = m15(0, low=100.0, high=105.0)
        second = m15(1, low=100.0, high=105.0)
        update = lifecycle(
            (zone(),), (observation(ObservationTimeframe.M15, first),)
        )
        with self.assertRaisesRegex(CandidateDataError, "exact M15"):
            decide(
                update,
                (regime_event(MarketRegime.BULLISH, FORMED),),
                (confirmation(second),),
            )


if __name__ == "__main__":
    unittest.main()
