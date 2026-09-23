"""Calendar feature diagnostics retain actual bars and all prior history."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.m15_confirmation import (
    EmaInitialization,
    m15_candle_events,
    m15_confirmation_events,
    m15_ema_events,
)
from xau_trader.multitimeframe import (
    MultiTimeframeDataError,
    PivotKind,
    aggregate_m15_to_h1,
    confirmed_h1_pivots,
)
from xau_trader.research_baseline import provisional_diagnostic_baseline_v1
from xau_trader.session_calendar import (
    ScheduledClosure,
    SessionCalendarArtifact,
    SessionCalendarError,
)
from xau_trader.supply_demand import (
    AtrMethod,
    h1_atr_events,
    h1_impulse_events,
    supply_demand_zone_events,
)


BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)
M15 = timedelta(minutes=15)
H1 = timedelta(hours=1)


def calendar_fixture(closure_start=23, closure_end=24, coverage_end=47):
    return SessionCalendarArtifact(
        calendar_id="synthetic-session",
        revision="fixture-v1",
        provider_name="Local deterministic generator",
        provider_legal_entity="Research project owner",
        instrument="XAU_USD",
        product_form="synthetic bid/ask spot-price series",
        coverage_start=BASE,
        coverage_end=BASE + coverage_end * H1,
        source_reference="local-generator://session-fixture-v1",
        retrieved_at=BASE,
        closures=(
            ScheduledClosure(
                BASE + closure_start * H1,
                BASE + closure_end * H1,
                "Synthetic maintenance interval",
            ),
        ),
    )


def quote(start, duration, price=100.0, high=None, low=None):
    return QuoteBar.from_mid(
        start_time=start,
        timestamp=start + duration,
        available_at=start + duration,
        availability_basis=AvailabilityBasis.SYNTHETIC,
        mid_open=price,
        mid_high=price + 1.0 if high is None else high,
        mid_low=price - 1.0 if low is None else low,
        mid_close=price,
        spread=0.2,
        volume=1.0,
    )


def session_m15():
    return tuple(
        quote(BASE + index * M15 + (H1 if index >= 92 else timedelta(0)), M15,
              price=100.0 + index % 7)
        for index in range(184)
    )


def ema_policy(initialization=EmaInitialization.FIRST_CLOSE_SEED):
    return replace(
        provisional_diagnostic_baseline_v1().confirmation_policy,
        ema_period=3,
        ema_initialization=initialization,
    )


class SessionIndicatorTests(unittest.TestCase):
    def test_92_plus_92_bars_produce_46_real_h1_bars(self):
        source = session_m15()
        h1 = aggregate_m15_to_h1(source, calendar=calendar_fixture())
        self.assertEqual(len(h1), 46)
        self.assertEqual(h1[22].timestamp, BASE + 23 * H1)
        self.assertEqual(h1[23].start_time, BASE + 24 * H1)
        self.assertEqual(h1[-1].timestamp, BASE + 47 * H1)
        self.assertTrue(all(bar.timestamp - bar.start_time == H1 for bar in h1))
        self.assertEqual(sum(bar.volume for bar in h1), 184.0)
        self.assertEqual(len(source), 184)

    def test_pivot_wings_and_availability_use_actual_h1_bars(self):
        calendar = calendar_fixture(4, 5, 10)
        source = tuple(
            quote(BASE + hour * H1, H1, high=high, low=90.0)
            for hour, high in zip((0, 1, 2, 3, 5, 6, 7), (102, 103, 104, 110, 104, 103, 102))
        )
        events = confirmed_h1_pivots(source, calendar=calendar)
        self.assertEqual(len(events), 1)
        pivot = events[0]
        self.assertEqual(pivot.kind, PivotKind.HIGH)
        self.assertEqual(pivot.pivot_bar_end, BASE + 4 * H1)
        self.assertEqual(pivot.confirmed_at, BASE + 8 * H1)
        self.assertEqual(pivot.available_at, BASE + 8 * H1)
        self.assertEqual(confirmed_h1_pivots(source, calendar=calendar, as_of=BASE + 7 * H1), ())
        self.assertEqual(confirmed_h1_pivots(source, calendar=calendar, as_of=BASE + 8 * H1), events)

        delayed = list(source)
        delayed[1] = replace(
            delayed[1],
            available_at=BASE + 9 * H1,
            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
        )
        self.assertEqual(confirmed_h1_pivots(delayed, calendar=calendar, as_of=BASE + 8 * H1), ())
        delayed_pivot = confirmed_h1_pivots(delayed, calendar=calendar, as_of=BASE + 9 * H1)[0]
        self.assertEqual(delayed_pivot.confirmed_at, BASE + 8 * H1)
        self.assertEqual(delayed_pivot.available_at, BASE + 9 * H1)

    def test_atr_retains_prior_close_and_wilder_history_across_closure(self):
        calendar = calendar_fixture(4, 5, 10)
        prices = (100.0, 102.0, 101.0, 103.0, 140.0, 139.0, 142.0, 137.0)
        contiguous = tuple(quote(BASE + index * H1, H1, price) for index, price in enumerate(prices))
        session = tuple(
            quote(BASE + (index + (index >= 4)) * H1, H1, price)
            for index, price in enumerate(prices)
        )
        for method in AtrMethod:
            with self.subTest(method=method):
                expected = h1_atr_events(contiguous, period=3, method=method)
                actual = h1_atr_events(session, period=3, method=method, calendar=calendar)
                self.assertEqual(
                    tuple((event.true_range, event.value) for event in actual),
                    tuple((event.true_range, event.value) for event in expected),
                )
                reopening = next(event for event in actual if event.bar_start == BASE + 5 * H1)
                self.assertAlmostEqual(reopening.true_range, 38.0)
                self.assertGreater(reopening.true_range, 2.0)
                self.assertEqual(actual[-1].bar_end, BASE + 9 * H1)

    def test_ema_retains_both_seed_conventions_and_actual_timestamps(self):
        session = session_m15()
        contiguous = tuple(
            replace(bar, start_time=BASE + index * M15,
                    timestamp=BASE + (index + 1) * M15,
                    available_at=BASE + (index + 1) * M15)
            for index, bar in enumerate(session)
        )
        for initialization in EmaInitialization:
            with self.subTest(initialization=initialization):
                policy = ema_policy(initialization)
                actual = m15_ema_events(session, policy=policy, calendar=calendar_fixture())
                expected = m15_ema_events(contiguous, policy=policy)
                self.assertEqual(tuple(event.value for event in actual), tuple(event.value for event in expected))
                self.assertTrue(all(event.seed_bar_start == BASE for event in actual))
                self.assertEqual(actual[-1].bar_end, BASE + 47 * H1)
                self.assertEqual(actual[-1].available_at, BASE + 47 * H1)

    def test_strict_defaults_still_reject_calendar_gaps(self):
        source = session_m15()
        h1 = aggregate_m15_to_h1(source, calendar=calendar_fixture())
        bundle = provisional_diagnostic_baseline_v1()
        calls = (
            lambda: aggregate_m15_to_h1(source),
            lambda: confirmed_h1_pivots(h1),
            lambda: h1_atr_events(h1, period=3, method=AtrMethod.WILDER_SMA_SEED),
            lambda: m15_ema_events(source, policy=ema_policy()),
            lambda: m15_candle_events(source, policy=ema_policy()),
            lambda: m15_confirmation_events(source, policy=ema_policy()),
            lambda: h1_impulse_events(h1, policy=bundle.impulse_policy),
            lambda: supply_demand_zone_events(
                h1, impulse_policy=bundle.impulse_policy,
                origin_policy=bundle.origin_policy,
                origin_lookback_bars=bundle.origin_lookback_bars,
            ),
        )
        for index, call in enumerate(calls):
            with self.subTest(api=index):
                with self.assertRaisesRegex(MultiTimeframeDataError, "not contiguous"):
                    call()

    def test_calendar_rejects_closed_slots_unknown_gaps_missing_open_bars_and_coverage(self):
        calendar = calendar_fixture()
        source = session_m15()
        bad_streams = (
            (quote(BASE + 23 * H1, M15),),
            source[:91] + source[92:],
            source[:10] + source[11:],
            (quote(BASE - M15, M15),),
            (quote(BASE + 47 * H1, M15),),
        )
        for index, source_bars in enumerate(bad_streams):
            with self.subTest(case=index):
                with self.assertRaises(SessionCalendarError):
                    m15_ema_events(source_bars, policy=ema_policy(), calendar=calendar)
        empty_closures = replace(calendar, closures=())
        with self.assertRaises(SessionCalendarError):
            aggregate_m15_to_h1(source, calendar=empty_closures)

    def test_all_feature_apis_validate_calendar_even_with_empty_input(self):
        partial_hour = calendar_fixture(23.25, 24)
        calls = (
            lambda calendar: aggregate_m15_to_h1((), calendar=calendar),
            lambda calendar: confirmed_h1_pivots((), calendar=calendar),
            lambda calendar: h1_atr_events((), period=3, method=AtrMethod.WILDER_SMA_SEED, calendar=calendar),
            lambda calendar: m15_ema_events((), policy=ema_policy(), calendar=calendar),
        )
        for index, call in enumerate(calls):
            with self.subTest(api=index):
                with self.assertRaisesRegex(TypeError, "SessionCalendarArtifact"):
                    call(object())
                with self.assertRaises(SessionCalendarError):
                    call(partial_hour)
                self.assertEqual(call(calendar_fixture()), ())

    def test_empty_closure_calendar_preserves_strict_contiguous_outputs(self):
        source = tuple(quote(BASE + index * M15, M15, price=100.0 + index % 7) for index in range(40))
        calendar = replace(calendar_fixture(), closures=())
        expected_h1 = aggregate_m15_to_h1(source)
        self.assertEqual(aggregate_m15_to_h1(source, calendar=calendar), expected_h1)
        self.assertEqual(confirmed_h1_pivots(expected_h1, calendar=calendar), confirmed_h1_pivots(expected_h1))
        self.assertEqual(
            h1_atr_events(expected_h1, period=3, method=AtrMethod.WILDER_SMA_SEED, calendar=calendar),
            h1_atr_events(expected_h1, period=3, method=AtrMethod.WILDER_SMA_SEED),
        )
        self.assertEqual(m15_ema_events(source, policy=ema_policy(), calendar=calendar), m15_ema_events(source, policy=ema_policy()))


if __name__ == "__main__":
    unittest.main()
