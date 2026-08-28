import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.m15_confirmation import (
    CandleCombination,
    CandlePatternKind,
    ConfirmationPolicy,
    CrossoverTiming,
    DisplacementHistory,
    DojiSemantics,
    EmaInitialization,
    EngulfingEquality,
    MedianConvention,
    TouchBarConfirmation,
    confirmation_time_eligible_for_touch,
    m15_candle_events,
    m15_confirmation_events,
    m15_ema_events,
)
from xau_trader.multitimeframe import MultiTimeframeDataError
from xau_trader.supply_demand import ImpulseDirection


UTC = timezone.utc
BASE = datetime(2026, 1, 5, tzinfo=UTC)
M15 = timedelta(minutes=15)


def policy(**changes):
    values = {
        "ema_period": 3,
        "ema_initialization": EmaInitialization.FIRST_CLOSE_SEED,
        "crossover_timing": CrossoverTiming.SAME_BAR_EMA,
        "engulfing_equality": EngulfingEquality.INCLUSIVE,
        "displacement_lookback_bars": 2,
        "displacement_median": MedianConvention.MEAN_OF_MIDDLE_TWO,
        "displacement_history": DisplacementHistory.PREVIOUS_BARS_ONLY,
        "displacement_body_multiple": 1.0,
        "doji_semantics": DojiSemantics.NEITHER_DIRECTION,
        "candle_combination": CandleCombination.ENGULFING_OR_DISPLACEMENT,
        "touch_bar_confirmation": TouchBarConfirmation.TOUCH_BAR_ALLOWED,
        "confirmation_expiry_bars": 2,
    }
    values.update(changes)
    return ConfirmationPolicy(**values)


def bar(
    index,
    *,
    open_price,
    close_price,
    start_base=BASE,
    duration=M15,
    available_at=None,
    availability_basis=AvailabilityBasis.SYNTHETIC,
):
    start = start_base + index * M15
    end = start + duration
    return QuoteBar.from_mid(
        start_time=start,
        timestamp=end,
        available_at=end if available_at is None else available_at,
        availability_basis=availability_basis,
        mid_open=open_price,
        mid_high=max(open_price, close_price) + 0.5,
        mid_low=min(open_price, close_price) - 0.5,
        mid_close=close_price,
        spread=0.2,
        volume=1.0,
    )


def body_bars(body_sizes):
    return tuple(
        bar(index, open_price=100.0, close_price=100.0 + body)
        for index, body in enumerate(body_sizes)
    )


def two_bar_bullish_reversal():
    return (
        bar(0, open_price=12.0, close_price=10.0),
        bar(1, open_price=9.0, close_price=13.0),
    )


class ConfirmationPolicyTests(unittest.TestCase):
    def test_policy_has_no_defaults_and_requires_typed_enum_choices(self):
        with self.assertRaises(TypeError):
            ConfirmationPolicy()

        selected = policy()
        enum_fields = {
            "ema_initialization": selected.ema_initialization.value,
            "crossover_timing": selected.crossover_timing.value,
            "engulfing_equality": selected.engulfing_equality.value,
            "displacement_median": selected.displacement_median.value,
            "displacement_history": selected.displacement_history.value,
            "doji_semantics": selected.doji_semantics.value,
            "candle_combination": selected.candle_combination.value,
            "touch_bar_confirmation": selected.touch_bar_confirmation.value,
        }
        for field_name, string_value in enum_fields.items():
            with self.subTest(field=field_name):
                with self.assertRaisesRegex(TypeError, field_name):
                    replace(selected, **{field_name: string_value})

    def test_numeric_policy_validation_fails_closed(self):
        selected = policy()
        for field_name in ("ema_period", "displacement_lookback_bars"):
            for invalid in (0, -1):
                with self.subTest(field=field_name, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, field_name):
                        replace(selected, **{field_name: invalid})
            for invalid in (True, 1.5):
                with self.subTest(field=field_name, invalid=invalid):
                    with self.assertRaisesRegex(TypeError, field_name):
                        replace(selected, **{field_name: invalid})

        for invalid in (-1.0, float("inf"), float("nan")):
            with self.subTest(body_multiple=invalid):
                with self.assertRaisesRegex(ValueError, "displacement_body_multiple"):
                    replace(selected, displacement_body_multiple=invalid)
        for invalid in (True, "1.2"):
            with self.subTest(body_multiple=invalid):
                with self.assertRaisesRegex(TypeError, "displacement_body_multiple"):
                    replace(selected, displacement_body_multiple=invalid)

        with self.assertRaisesRegex(ValueError, "confirmation_expiry_bars"):
            replace(selected, confirmation_expiry_bars=-1)
        with self.assertRaisesRegex(TypeError, "confirmation_expiry_bars"):
            replace(selected, confirmation_expiry_bars=True)
        with self.assertRaisesRegex(ValueError, "at least 1"):
            replace(
                selected,
                touch_bar_confirmation=TouchBarConfirmation.AFTER_TOUCH_BAR_ONLY,
                confirmation_expiry_bars=0,
            )

        huge = 10 ** 400
        for field_name in (
            "ema_period",
            "displacement_lookback_bars",
            "confirmation_expiry_bars",
        ):
            with self.subTest(field=field_name, invalid="huge integer"):
                with self.assertRaisesRegex(ValueError, field_name):
                    replace(selected, **{field_name: huge})
        with self.assertRaisesRegex(ValueError, "displacement_body_multiple"):
            replace(selected, displacement_body_multiple=huge)

    def test_fingerprint_is_versioned_complete_and_changes_for_every_field(self):
        selected = policy()
        expected_identity = ";".join(
            (
                "m15-confirmation-policy-v1",
                "ema_period=3",
                "ema_initialization=first_close_seed",
                "crossover_timing=same_bar_ema",
                "engulfing_equality=inclusive",
                "displacement_lookback_bars=2",
                "displacement_median=mean_of_middle_two",
                "displacement_history=previous_bars_only",
                "displacement_body_multiple=1e0",
                "doji_semantics=neither_direction",
                "candle_combination=engulfing_or_displacement",
                "touch_bar_confirmation=touch_bar_allowed",
                "confirmation_expiry_bars=2",
            )
        )
        self.assertEqual(selected.canonical_identity, expected_identity)
        self.assertEqual(len(selected.fingerprint), 64)
        self.assertTrue(
            all(character in "0123456789abcdef" for character in selected.fingerprint)
        )

        alternatives = {
            "ema_period": 4,
            "ema_initialization": EmaInitialization.SMA_PERIOD_SEED,
            "crossover_timing": CrossoverTiming.CURRENT_CLOSE_VS_PRIOR_EMA,
            "engulfing_equality": EngulfingEquality.STRICT_BOTH_BOUNDARIES,
            "displacement_lookback_bars": 3,
            "displacement_median": MedianConvention.LOWER_MIDDLE,
            "displacement_history": DisplacementHistory.CURRENT_BAR_INCLUSIVE,
            "displacement_body_multiple": 1.2,
            "doji_semantics": DojiSemantics.BOTH_DIRECTIONS,
            "candle_combination": CandleCombination.ENGULFING_AND_DISPLACEMENT,
            "touch_bar_confirmation": TouchBarConfirmation.AFTER_TOUCH_BAR_ONLY,
            "confirmation_expiry_bars": 3,
        }
        for field_name, alternative in alternatives.items():
            with self.subTest(field=field_name):
                changed = replace(selected, **{field_name: alternative})
                self.assertNotEqual(changed.fingerprint, selected.fingerprint)


class EmaEventTests(unittest.TestCase):
    def test_first_close_seed_uses_standard_recursive_alpha(self):
        bars = tuple(
            bar(index, open_price=close, close_price=close)
            for index, close in enumerate((10.0, 12.0, 14.0))
        )

        events = m15_ema_events(bars, policy=policy(ema_period=3))

        self.assertEqual(tuple(event.value for event in events), (10.0, 11.0, 12.5))
        self.assertEqual(tuple(event.close for event in events), (10.0, 12.0, 14.0))
        self.assertTrue(all(event.seed_bar_start == BASE for event in events))
        self.assertTrue(all(event.policy_fingerprint == policy().fingerprint for event in events))
        with self.assertRaises(FrozenInstanceError):
            events[-1].value = 99.0

    def test_sma_period_seed_emits_only_after_complete_seed_window(self):
        bars = tuple(
            bar(index, open_price=close, close_price=close)
            for index, close in enumerate((10.0, 12.0, 14.0, 16.0))
        )
        selected = policy(
            ema_period=3,
            ema_initialization=EmaInitialization.SMA_PERIOD_SEED,
        )

        self.assertEqual(m15_ema_events(bars[:2], policy=selected), ())
        events = m15_ema_events(bars, policy=selected)

        self.assertEqual(tuple(event.bar_start for event in events), (BASE + 2 * M15, BASE + 3 * M15))
        self.assertEqual(tuple(event.value for event in events), (12.0, 14.0))
        self.assertEqual(events[0].seed_bar_start, BASE)

    def test_recursive_ema_keeps_exact_seed_to_current_availability_chain(self):
        delayed_at = BASE + timedelta(hours=2)
        bars = (
            bar(
                0,
                open_price=10.0,
                close_price=10.0,
                available_at=delayed_at,
                availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
            ),
            bar(1, open_price=10.0, close_price=12.0),
            bar(2, open_price=12.0, close_price=14.0),
        )

        events = m15_ema_events(bars, policy=policy())

        self.assertTrue(all(event.available_at == delayed_at for event in events))
        self.assertTrue(
            all(
                event.availability_basis == AvailabilityBasis.OBSERVED_RECEIPT
                for event in events
            )
        )
        self.assertEqual(
            m15_ema_events(
                bars, policy=policy(), as_of=delayed_at - timedelta(microseconds=1)
            ),
            (),
        )
        self.assertEqual(m15_ema_events(bars, policy=policy(), as_of=delayed_at), events)

    def test_key_binds_seed_anchor_and_every_stored_ema_value(self):
        short = (
            bar(0, open_price=10.0, close_price=10.0),
            bar(1, open_price=12.0, close_price=12.0),
        )
        earlier_base = BASE - M15
        extended = (
            bar(
                0,
                open_price=20.0,
                close_price=20.0,
                start_base=earlier_base,
            ),
            bar(
                1,
                open_price=10.0,
                close_price=10.0,
                start_base=earlier_base,
            ),
            bar(
                2,
                open_price=12.0,
                close_price=12.0,
                start_base=earlier_base,
            ),
        )
        selected = policy(ema_period=3)

        short_event = m15_ema_events(short, policy=selected)[-1]
        extended_event = m15_ema_events(extended, policy=selected)[-1]

        self.assertEqual(short_event.bar_start, extended_event.bar_start)
        self.assertNotEqual(short_event.seed_bar_start, extended_event.seed_bar_start)
        self.assertNotEqual(short_event.value, extended_event.value)
        self.assertNotEqual(short_event.key, extended_event.key)
        self.assertEqual(
            short_event.key,
            (
                short_event.policy_fingerprint,
                short_event.bar_start,
                short_event.bar_end,
                short_event.available_at,
                short_event.availability_basis,
                short_event.period,
                short_event.initialization,
                short_event.seed_bar_start,
                short_event.close,
                short_event.value,
            ),
        )


class EventEvidenceKeyTests(unittest.TestCase):
    def test_candle_and_confirmation_keys_bind_all_stored_proof_fields(self):
        bars = two_bar_bullish_reversal()
        selected = policy(
            ema_period=2,
            displacement_lookback_bars=1,
            displacement_body_multiple=1.2,
        )
        candle = tuple(
            event
            for event in m15_candle_events(bars, policy=selected)
            if event.kind == CandlePatternKind.ENGULFING
        )[0]
        confirmation = m15_confirmation_events(bars, policy=selected)[0]

        self.assertEqual(
            candle.key,
            (
                candle.policy_fingerprint,
                candle.direction,
                candle.kind,
                candle.bar_start,
                candle.bar_end,
                candle.confirmed_at,
                candle.available_at,
                candle.availability_basis,
                candle.body_size,
                candle.reference_value,
                candle.required_body_size,
                candle.reference_window_start,
                candle.reference_window_end,
            ),
        )
        self.assertEqual(
            confirmation.key,
            (
                confirmation.policy_fingerprint,
                confirmation.direction,
                confirmation.bar_start,
                confirmation.bar_end,
                confirmation.confirmed_at,
                confirmation.available_at,
                confirmation.availability_basis,
                confirmation.ema_period,
                confirmation.ema_initialization,
                confirmation.crossover_timing,
                confirmation.prior_close,
                confirmation.prior_ema,
                confirmation.current_close,
                confirmation.current_ema,
                confirmation.current_crossover_reference,
                confirmation.proving_patterns,
                confirmation.candle_combination,
                confirmation.touch_bar_confirmation,
                confirmation.confirmation_expiry_bars,
            ),
        )
        self.assertNotEqual(
            candle.key,
            replace(candle, reference_value=candle.reference_value + 0.25).key,
        )
        self.assertNotEqual(
            confirmation.key,
            replace(
                confirmation,
                current_ema=confirmation.current_ema + 0.25,
            ).key,
        )


class CrossoverAndConfirmationTests(unittest.TestCase):
    def test_prior_equality_counts_but_current_equality_does_not(self):
        bullish = (
            bar(0, open_price=10.0, close_price=10.0),
            bar(1, open_price=10.0, close_price=12.0),
        )
        selected = policy(
            ema_period=3,
            displacement_lookback_bars=1,
            displacement_body_multiple=0.0,
            candle_combination=CandleCombination.DISPLACEMENT_ONLY,
        )
        bullish_events = m15_confirmation_events(bullish, policy=selected)
        self.assertEqual(len(bullish_events), 1)
        self.assertEqual(bullish_events[0].direction, ImpulseDirection.BULLISH)
        self.assertEqual(bullish_events[0].prior_close, bullish_events[0].prior_ema)

        current_equal = (
            bar(0, open_price=10.0, close_price=10.0),
            bar(1, open_price=10.0, close_price=8.0),
            bar(2, open_price=8.0, close_price=9.0),
        )
        events = m15_confirmation_events(current_equal, policy=selected)
        bullish_final = tuple(
            event
            for event in events
            if event.direction == ImpulseDirection.BULLISH
            and event.bar_start == BASE + 2 * M15
        )
        self.assertEqual(bullish_final, ())
        final_ema = m15_ema_events(current_equal, policy=selected)[-1]
        self.assertEqual(final_ema.close, final_ema.value)

    def test_bearish_crossover_is_the_exact_mirror(self):
        bars = (
            bar(0, open_price=10.0, close_price=10.0),
            bar(1, open_price=10.0, close_price=8.0),
        )
        selected = policy(
            displacement_lookback_bars=1,
            displacement_body_multiple=0.0,
            candle_combination=CandleCombination.DISPLACEMENT_ONLY,
        )

        events = m15_confirmation_events(bars, policy=selected)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].direction, ImpulseDirection.BEARISH)
        self.assertEqual(events[0].prior_close, events[0].prior_ema)
        self.assertLess(events[0].current_close, events[0].current_ema)

    def test_crossover_reference_policy_is_observable_at_period_one(self):
        bars = (
            bar(0, open_price=10.0, close_price=10.0),
            bar(1, open_price=10.0, close_price=12.0),
        )
        common = {
            "ema_period": 1,
            "displacement_lookback_bars": 1,
            "displacement_body_multiple": 0.0,
            "candle_combination": CandleCombination.DISPLACEMENT_ONLY,
        }
        same_bar = policy(crossover_timing=CrossoverTiming.SAME_BAR_EMA, **common)
        prior_bar = policy(
            crossover_timing=CrossoverTiming.CURRENT_CLOSE_VS_PRIOR_EMA, **common
        )

        self.assertEqual(m15_confirmation_events(bars, policy=same_bar), ())
        events = m15_confirmation_events(bars, policy=prior_bar)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].current_ema, events[0].current_close)
        self.assertEqual(events[0].current_crossover_reference, events[0].prior_ema)


class CandlePatternTests(unittest.TestCase):
    def test_engulfing_boundary_equality_is_an_explicit_policy(self):
        bars = (
            bar(0, open_price=12.0, close_price=10.0),
            bar(1, open_price=10.0, close_price=12.0),
        )
        inclusive = policy(engulfing_equality=EngulfingEquality.INCLUSIVE)
        strict = policy(
            engulfing_equality=EngulfingEquality.STRICT_BOTH_BOUNDARIES
        )

        inclusive_engulfing = tuple(
            event
            for event in m15_candle_events(bars, policy=inclusive)
            if event.kind == CandlePatternKind.ENGULFING
        )
        strict_engulfing = tuple(
            event
            for event in m15_candle_events(bars, policy=strict)
            if event.kind == CandlePatternKind.ENGULFING
        )

        self.assertEqual(len(inclusive_engulfing), 1)
        self.assertEqual(inclusive_engulfing[0].direction, ImpulseDirection.BULLISH)
        self.assertEqual(inclusive_engulfing[0].body_size, 2.0)
        self.assertEqual(inclusive_engulfing[0].reference_value, 2.0)
        self.assertEqual(strict_engulfing, ())

    def test_bearish_engulfing_is_mirrored(self):
        bars = (
            bar(0, open_price=10.0, close_price=12.0),
            bar(1, open_price=12.5, close_price=9.5),
        )
        events = tuple(
            event
            for event in m15_candle_events(bars, policy=policy())
            if event.kind == CandlePatternKind.ENGULFING
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].direction, ImpulseDirection.BEARISH)

    def test_even_median_conventions_and_threshold_equality_are_exact(self):
        bars = body_bars((1.0, 2.0, 9.0, 10.0, 5.5))
        common = {
            "displacement_lookback_bars": 4,
            "displacement_body_multiple": 1.0,
            "displacement_history": DisplacementHistory.PREVIOUS_BARS_ONLY,
        }
        mean = policy(displacement_median=MedianConvention.MEAN_OF_MIDDLE_TWO, **common)
        lower = policy(displacement_median=MedianConvention.LOWER_MIDDLE, **common)
        upper = policy(displacement_median=MedianConvention.UPPER_MIDDLE, **common)

        def current_displacement(selected):
            return tuple(
                event
                for event in m15_candle_events(bars, policy=selected)
                if event.kind == CandlePatternKind.DISPLACEMENT
                and event.bar_start == BASE + 4 * M15
            )

        mean_events = current_displacement(mean)
        lower_events = current_displacement(lower)
        upper_events = current_displacement(upper)

        self.assertEqual(len(mean_events), 1)
        self.assertEqual(mean_events[0].reference_value, 5.5)
        self.assertEqual(mean_events[0].required_body_size, 5.5)
        self.assertEqual(mean_events[0].body_size, 5.5)
        self.assertEqual(lower_events[0].reference_value, 2.0)
        self.assertEqual(upper_events, ())

    def test_current_bar_inclusion_changes_only_the_declared_reference_window(self):
        bars = body_bars((20.0, 1.0, 6.0))
        common = {
            "displacement_lookback_bars": 2,
            "displacement_body_multiple": 1.0,
        }
        previous_only = policy(
            displacement_history=DisplacementHistory.PREVIOUS_BARS_ONLY, **common
        )
        inclusive = policy(
            displacement_history=DisplacementHistory.CURRENT_BAR_INCLUSIVE,
            **common
        )

        def current_displacement(selected):
            return tuple(
                event
                for event in m15_candle_events(bars, policy=selected)
                if event.kind == CandlePatternKind.DISPLACEMENT
                and event.bar_start == BASE + 2 * M15
            )

        self.assertEqual(current_displacement(previous_only), ())
        inclusive_events = current_displacement(inclusive)
        self.assertEqual(len(inclusive_events), 1)
        self.assertEqual(inclusive_events[0].reference_value, 3.5)
        self.assertEqual(inclusive_events[0].reference_window_start, BASE + M15)
        self.assertEqual(inclusive_events[0].reference_window_end, BASE + 3 * M15)

    def test_doji_semantics_are_symmetric_and_explicit(self):
        bars = (
            bar(0, open_price=12.0, close_price=10.0),
            bar(1, open_price=11.0, close_price=11.0),
        )
        common = {
            "displacement_lookback_bars": 1,
            "displacement_body_multiple": 0.0,
        }
        neither = policy(doji_semantics=DojiSemantics.NEITHER_DIRECTION, **common)
        both = policy(doji_semantics=DojiSemantics.BOTH_DIRECTIONS, **common)

        neither_current = tuple(
            event
            for event in m15_candle_events(bars, policy=neither)
            if event.bar_start == BASE + M15
        )
        both_displacement = tuple(
            event
            for event in m15_candle_events(bars, policy=both)
            if event.kind == CandlePatternKind.DISPLACEMENT
            and event.bar_start == BASE + M15
        )

        self.assertEqual(neither_current, ())
        self.assertEqual(
            tuple(event.direction for event in both_displacement),
            (ImpulseDirection.BULLISH, ImpulseDirection.BEARISH),
        )

    def test_candle_availability_uses_only_the_pattern_evidence(self):
        delayed_at = BASE + timedelta(hours=2)
        bars = (
            bar(
                0,
                open_price=10.0,
                close_price=10.0,
                available_at=delayed_at,
                availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
            ),
            bar(1, open_price=10.0, close_price=12.0),
            bar(2, open_price=12.0, close_price=8.0),
            bar(3, open_price=8.0, close_price=12.0),
        )
        selected = policy(candle_combination=CandleCombination.ENGULFING_ONLY)

        event = tuple(
            item
            for item in m15_candle_events(bars, policy=selected)
            if item.kind == CandlePatternKind.ENGULFING
            and item.direction == ImpulseDirection.BULLISH
            and item.bar_start == BASE + 3 * M15
        )[0]

        self.assertEqual(event.available_at, BASE + timedelta(hours=1))
        self.assertEqual(event.availability_basis, AvailabilityBasis.SYNTHETIC)


class EvidenceCombinationAndAvailabilityTests(unittest.TestCase):
    def test_or_and_policies_record_both_simultaneous_proof_paths(self):
        bars = two_bar_bullish_reversal()
        common = {
            "ema_period": 2,
            "displacement_lookback_bars": 1,
            "displacement_body_multiple": 1.2,
        }
        or_policy = policy(
            candle_combination=CandleCombination.ENGULFING_OR_DISPLACEMENT,
            **common
        )
        and_policy = policy(
            candle_combination=CandleCombination.ENGULFING_AND_DISPLACEMENT,
            **common
        )

        for selected in (or_policy, and_policy):
            with self.subTest(combination=selected.candle_combination):
                events = m15_confirmation_events(bars, policy=selected)
                self.assertEqual(len(events), 1)
                self.assertEqual(
                    events[0].proving_patterns,
                    (CandlePatternKind.ENGULFING, CandlePatternKind.DISPLACEMENT),
                )
                self.assertEqual(events[0].candle_combination, selected.candle_combination)

    def test_and_requires_both_while_or_accepts_one(self):
        bars = (
            bar(0, open_price=12.0, close_price=10.0),
            bar(1, open_price=10.0, close_price=12.0),
        )
        common = {
            "ema_period": 2,
            "displacement_lookback_bars": 1,
            "displacement_body_multiple": 1.2,
        }
        or_policy = policy(
            candle_combination=CandleCombination.ENGULFING_OR_DISPLACEMENT,
            **common
        )
        and_policy = policy(
            candle_combination=CandleCombination.ENGULFING_AND_DISPLACEMENT,
            **common
        )

        or_events = m15_confirmation_events(bars, policy=or_policy)

        self.assertEqual(len(or_events), 1)
        self.assertEqual(or_events[0].proving_patterns, (CandlePatternKind.ENGULFING,))
        self.assertEqual(m15_confirmation_events(bars, policy=and_policy), ())

    def test_delayed_old_ema_dependency_never_backdates_confirmation(self):
        delayed_at = BASE + timedelta(hours=2)
        bars = (
            bar(
                0,
                open_price=10.0,
                close_price=10.0,
                available_at=delayed_at,
                availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
            ),
            bar(1, open_price=10.0, close_price=12.0),
            bar(2, open_price=12.0, close_price=8.0),
            bar(3, open_price=8.0, close_price=12.0),
        )
        selected = policy(
            ema_period=3,
            candle_combination=CandleCombination.ENGULFING_ONLY,
        )

        unfiltered = m15_confirmation_events(bars, policy=selected)
        final = tuple(
            event
            for event in unfiltered
            if event.direction == ImpulseDirection.BULLISH
            and event.bar_start == BASE + 3 * M15
        )[0]

        self.assertEqual(final.confirmed_at, BASE + timedelta(hours=1))
        self.assertEqual(final.available_at, delayed_at)
        self.assertEqual(final.availability_basis, AvailabilityBasis.OBSERVED_RECEIPT)
        before = m15_confirmation_events(
            bars,
            policy=selected,
            as_of=delayed_at - timedelta(microseconds=1),
        )
        at = m15_confirmation_events(bars, policy=selected, as_of=delayed_at)
        self.assertNotIn(final, before)
        self.assertIn(final, at)

    def test_as_of_requires_timezone_and_normalizes_an_offset(self):
        bars = two_bar_bullish_reversal()
        selected = policy(
            ema_period=2,
            displacement_lookback_bars=1,
            displacement_body_multiple=1.2,
        )
        with self.assertRaisesRegex(ValueError, "timezone"):
            m15_confirmation_events(
                bars, policy=selected, as_of=datetime(2026, 1, 5, 1)
            )
        offset = timezone(timedelta(hours=1))
        local_as_of = (BASE + 2 * M15).astimezone(offset)
        self.assertEqual(
            m15_confirmation_events(bars, policy=selected, as_of=local_as_of),
            m15_confirmation_events(bars, policy=selected, as_of=BASE + 2 * M15),
        )


class InputIntegrityAndInvarianceTests(unittest.TestCase):
    def test_gap_non_alignment_wrong_duration_and_non_utc_fail_closed(self):
        gap = (
            bar(0, open_price=10.0, close_price=10.0),
            bar(2, open_price=10.0, close_price=12.0),
        )
        with self.assertRaisesRegex(MultiTimeframeDataError, "not contiguous"):
            m15_ema_events(gap, policy=policy())

        non_aligned = (
            bar(
                0,
                open_price=10.0,
                close_price=10.0,
                start_base=BASE + timedelta(minutes=5),
            ),
        )
        with self.assertRaisesRegex(MultiTimeframeDataError, "not aligned"):
            m15_candle_events(non_aligned, policy=policy())

        wrong_duration = (
            bar(0, open_price=10.0, close_price=10.0, duration=timedelta(minutes=10)),
        )
        with self.assertRaisesRegex(MultiTimeframeDataError, "exactly 15 minutes"):
            m15_confirmation_events(wrong_duration, policy=policy())

        malformed = bar(0, open_price=10.0, close_price=10.0)
        plus_one = timezone(timedelta(hours=1))
        object.__setattr__(malformed, "start_time", malformed.start_time.astimezone(plus_one))
        object.__setattr__(malformed, "timestamp", malformed.timestamp.astimezone(plus_one))
        with self.assertRaisesRegex(MultiTimeframeDataError, "normalized to UTC"):
            m15_ema_events((malformed,), policy=policy())

    def test_offset_inputs_are_normalized_by_the_domain_before_detection(self):
        plus_one = timezone(timedelta(hours=1))
        local_base = BASE.astimezone(plus_one)
        normalized = bar(
            0,
            open_price=10.0,
            close_price=10.0,
            start_base=local_base,
        )
        self.assertEqual(normalized.start_time, BASE)
        self.assertIs(normalized.start_time.tzinfo, UTC)
        self.assertEqual(len(m15_ema_events((normalized,), policy=policy())), 1)

    def test_tampered_availability_and_quote_domain_fail_closed(self):
        backdated = bar(0, open_price=10.0, close_price=10.0)
        object.__setattr__(backdated, "available_at", backdated.start_time)
        for detector in (m15_ema_events, m15_candle_events, m15_confirmation_events):
            with self.subTest(detector=detector.__name__, defect="backdated"):
                with self.assertRaisesRegex(
                    MultiTimeframeDataError, "availability cannot precede"
                ):
                    detector((backdated,), policy=policy())

        plus_one = timezone(timedelta(hours=1))
        non_utc_availability = bar(0, open_price=10.0, close_price=10.0)
        object.__setattr__(
            non_utc_availability,
            "available_at",
            non_utc_availability.available_at.astimezone(plus_one),
        )
        with self.assertRaisesRegex(MultiTimeframeDataError, "available_at.*UTC"):
            m15_ema_events((non_utc_availability,), policy=policy())

        untyped_basis = bar(0, open_price=10.0, close_price=10.0)
        object.__setattr__(untyped_basis, "availability_basis", "synthetic")
        with self.assertRaisesRegex(TypeError, "availability_basis"):
            m15_ema_events((untyped_basis,), policy=policy())

        invalid_high = bar(0, open_price=10.0, close_price=10.0)
        object.__setattr__(invalid_high, "bid_high", invalid_high.bid_open - 0.1)
        with self.assertRaisesRegex(MultiTimeframeDataError, "bid_high"):
            m15_ema_events((invalid_high,), policy=policy())

        crossed_quote = bar(0, open_price=10.0, close_price=10.0)
        object.__setattr__(crossed_quote, "bid_open", crossed_quote.ask_open + 0.1)
        object.__setattr__(crossed_quote, "bid_high", crossed_quote.ask_high)
        with self.assertRaisesRegex(MultiTimeframeDataError, "bid_open.*ask_open"):
            m15_ema_events((crossed_quote,), policy=policy())

        non_finite = bar(0, open_price=10.0, close_price=10.0)
        object.__setattr__(non_finite, "ask_close", float("nan"))
        with self.assertRaisesRegex(MultiTimeframeDataError, "ask_close"):
            m15_ema_events((non_finite,), policy=policy())

        negative_volume = bar(0, open_price=10.0, close_price=10.0)
        object.__setattr__(negative_volume, "volume", -1.0)
        with self.assertRaisesRegex(MultiTimeframeDataError, "volume"):
            m15_ema_events((negative_volume,), policy=policy())

    def test_future_suffix_cannot_change_any_prior_event(self):
        prefix = two_bar_bullish_reversal()
        suffix = (
            bar(2, open_price=13.0, close_price=11.0),
            bar(3, open_price=11.0, close_price=14.0),
        )
        selected = policy(
            ema_period=2,
            displacement_lookback_bars=1,
            displacement_body_multiple=1.2,
        )

        detectors = (m15_ema_events, m15_candle_events, m15_confirmation_events)
        for detector in detectors:
            with self.subTest(detector=detector.__name__):
                original = detector(prefix, policy=selected)
                extended = detector(prefix + suffix, policy=selected)
                prior = tuple(
                    event for event in extended if event.bar_start < prefix[-1].timestamp
                )
                self.assertEqual(prior, original)


class TouchWindowTests(unittest.TestCase):
    def _event(self, selected):
        events = m15_confirmation_events(two_bar_bullish_reversal(), policy=selected)
        self.assertEqual(len(events), 1)
        return events[0]

    def test_touch_bar_allowed_with_zero_expiry_is_touch_bar_only(self):
        selected = policy(
            ema_period=2,
            displacement_lookback_bars=1,
            displacement_body_multiple=1.2,
            touch_bar_confirmation=TouchBarConfirmation.TOUCH_BAR_ALLOWED,
            confirmation_expiry_bars=0,
        )
        event = self._event(selected)

        self.assertTrue(
            confirmation_time_eligible_for_touch(
                event,
                touch_bar_start=event.bar_start,
                touch_bar_end=event.bar_end,
                policy=selected,
            )
        )
        self.assertFalse(
            confirmation_time_eligible_for_touch(
                event,
                touch_bar_start=event.bar_start - M15,
                touch_bar_end=event.bar_end - M15,
                policy=selected,
            )
        )

    def test_after_touch_policy_accepts_only_bars_one_through_expiry(self):
        selected = policy(
            ema_period=2,
            displacement_lookback_bars=1,
            displacement_body_multiple=1.2,
            touch_bar_confirmation=TouchBarConfirmation.AFTER_TOUCH_BAR_ONLY,
            confirmation_expiry_bars=2,
        )
        event = self._event(selected)

        eligibility = []
        for intervals_before in (0, 1, 2, 3):
            eligibility.append(
                confirmation_time_eligible_for_touch(
                    event,
                    touch_bar_start=event.bar_start - intervals_before * M15,
                    touch_bar_end=event.bar_end - intervals_before * M15,
                    policy=selected,
                )
            )
        self.assertEqual(eligibility, [False, True, True, False])

    def test_touch_helper_rejects_policy_conflict_and_malformed_intervals(self):
        selected = policy(
            ema_period=2,
            displacement_lookback_bars=1,
            displacement_body_multiple=1.2,
        )
        event = self._event(selected)
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            confirmation_time_eligible_for_touch(
                event,
                touch_bar_start=event.bar_start,
                touch_bar_end=event.bar_end,
                policy=replace(selected, confirmation_expiry_bars=3),
            )

        with self.assertRaisesRegex(ValueError, "align"):
            confirmation_time_eligible_for_touch(
                replace(
                    event,
                    bar_start=event.bar_start + timedelta(minutes=5),
                    bar_end=event.bar_end + timedelta(minutes=5),
                ),
                touch_bar_start=event.bar_start,
                touch_bar_end=event.bar_end,
                policy=selected,
            )
        with self.assertRaisesRegex(ValueError, "exactly 15 minutes"):
            confirmation_time_eligible_for_touch(
                event,
                touch_bar_start=event.bar_start,
                touch_bar_end=event.bar_end + timedelta(minutes=5),
                policy=selected,
            )
        plus_one = timezone(timedelta(hours=1))
        with self.assertRaisesRegex(ValueError, "normalized to UTC"):
            confirmation_time_eligible_for_touch(
                event,
                touch_bar_start=event.bar_start.astimezone(plus_one),
                touch_bar_end=event.bar_end.astimezone(plus_one),
                policy=selected,
            )


if __name__ == "__main__":
    unittest.main()
