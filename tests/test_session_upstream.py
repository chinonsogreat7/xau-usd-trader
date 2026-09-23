"""Session-aware upstream detectors count actual evidence, never closed slots."""

import hashlib
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.market_regime import (
    InsufficientEvidencePolicy,
    MarketRegime,
    MarketRegimeDataError,
    MixedStructurePolicy,
    RegimePolicy,
    StructureRule,
    h1_regime_events,
)
from xau_trader.multitimeframe import (
    H1PivotEvent,
    MultiTimeframeDataError,
    PivotKind,
    confirmed_h1_pivots,
)
from xau_trader.session_calendar import (
    ScheduledClosure,
    SessionCalendarArtifact,
    SessionCalendarError,
)
from xau_trader.session_timing import SESSION_STRATEGY_SEMANTICS
from xau_trader.supply_demand import (
    AtrMethod,
    AtrTiming,
    ImpulseMovementPolicy,
    ImpulsePolicy,
    OriginSelectionPolicy,
    h1_impulse_events,
    supply_demand_zone_events,
)


BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)
H1 = timedelta(hours=1)


def calendar_fixture(start=3, end=51):
    return SessionCalendarArtifact(
        calendar_id="synthetic-upstream-session",
        revision="fixture-v1",
        provider_name="Local deterministic generator",
        provider_legal_entity="Research project owner",
        instrument="XAU_USD",
        product_form="synthetic bid/ask spot-price series",
        coverage_start=BASE,
        coverage_end=BASE + 100 * H1,
        source_reference="local-generator://session-upstream-fixture-v1",
        retrieved_at=BASE,
        closures=(ScheduledClosure(
            BASE + start * H1,
            BASE + end * H1,
            "Synthetic multi-day closure",
        ),),
    )


def impulse_policy(calendar=None):
    return ImpulsePolicy(
        movement=ImpulseMovementPolicy.NET_OPEN_TO_CLOSE,
        atr_method=AtrMethod.WILDER_SMA_SEED,
        atr_timing=AtrTiming.BEFORE_IMPULSE_WINDOW,
        atr_period=1,
        minimum_directional_bars=3,
        movement_atr_multiple=1.5,
        body_atr_multiple=0.8,
        calendar=calendar,
    )


def regime_policy(calendar=None):
    return RegimePolicy(
        structure_rule=StructureRule.STRICT_LAST_TWO_CONFIRMED_HIGH_LOW,
        insufficient_evidence=InsufficientEvidencePolicy.UNKNOWN,
        mixed_structure=MixedStructurePolicy.RANGE,
        calendar=calendar,
    )


def bullish_bars(calendar=None):
    prices = (
        (100.0, 101.0, 98.0, 99.0),
        (99.0, 101.5, 98.5, 101.0),
        (101.0, 103.5, 100.5, 103.0),
        (103.0, 103.5, 102.0, 102.5),
        (102.5, 105.5, 102.0, 105.0),
    )
    bars = []
    for index, (open_price, high, low, close) in enumerate(prices):
        start = BASE + index * H1
        if calendar is not None and start >= calendar.closures[0].start:
            start += calendar.closures[0].end - calendar.closures[0].start
        bars.append(QuoteBar.from_mid(
            start_time=start,
            timestamp=start + H1,
            available_at=start + H1,
            availability_basis=AvailabilityBasis.SYNTHETIC,
            mid_open=open_price,
            mid_high=high,
            mid_low=low,
            mid_close=close,
            spread=0.2,
            volume=10.0,
        ))
    return tuple(bars)


def zones(bars, policy, *, inclusive=False, lookback=1, as_of=None):
    return supply_demand_zone_events(
        bars,
        impulse_policy=policy,
        origin_policy=(
            OriginSelectionPolicy.LAST_OPPOSITE_AT_OR_BEFORE_WINDOW_END
            if inclusive else OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW
        ),
        origin_lookback_bars=lookback,
        as_of=as_of,
    )


def bullish_pivots(calendar=None):
    result = []
    for index, (kind, price) in enumerate((
        (PivotKind.HIGH, 100.0), (PivotKind.LOW, 80.0),
        (PivotKind.HIGH, 110.0), (PivotKind.LOW, 90.0),
    )):
        start = BASE + 4 * index * H1
        end = start + H1
        confirmation = end + 3 * H1
        if calendar is not None:
            closure = calendar.closures[0]
            duration = closure.end - closure.start
            if start >= closure.start:
                start += duration
                end += duration
            if confirmation > closure.start:
                confirmation += duration
        result.append(H1PivotEvent(
            kind=kind,
            pivot_bar_start=start,
            pivot_bar_end=end,
            confirmed_at=confirmation,
            available_at=confirmation,
            availability_basis=AvailabilityBasis.SYNTHETIC,
            bid_price=price - 0.1,
            ask_price=price + 0.1,
            mid_price=price,
        ))
    return tuple(result)


class SessionUpstreamTests(unittest.TestCase):
    def test_impulse_counts_four_actual_h1_bars_across_multi_day_closure(self):
        calendar = calendar_fixture()
        selected = impulse_policy(calendar)
        source = bullish_bars(calendar)
        actual = h1_impulse_events(source, policy=selected)
        expected = h1_impulse_events(bullish_bars(), policy=impulse_policy())
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0].window_bars, 4)
        self.assertEqual(actual[0].window_end - actual[0].window_start, 52 * H1)
        self.assertEqual(actual[0].confirmed_at, source[-1].timestamp)
        self.assertEqual(actual[0].available_at, source[-1].available_at)
        for field in ("atr_value", "directional_movement", "largest_body",
                      "directional_bar_count", "direction"):
            self.assertEqual(getattr(actual[0], field), getattr(expected[0], field))
        self.assertEqual(actual[0].policy_fingerprint, selected.fingerprint)
        self.assertNotEqual(actual[0].key[0], expected[0].key[0])
        with self.assertRaises(MultiTimeframeDataError):
            h1_impulse_events(source, policy=impulse_policy())

    def test_origin_lookback_uses_actual_bars_before_reopening(self):
        calendar = calendar_fixture(1, 49)
        source = bullish_bars(calendar)
        actual = zones(source, impulse_policy(calendar), lookback=1)
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0].origin_bar_start, BASE)
        self.assertEqual(actual[0].impulse_window_start, BASE + 49 * H1)
        self.assertEqual(actual[0].origin_lookback_bars, 1)
        self.assertIs(actual[0].calendar, calendar)
        self.assertEqual(actual[0].lower_price, 98.0)
        self.assertEqual(actual[0].upper_price, 100.0)

    def test_inclusive_origin_search_preserves_real_timestamp_and_latest_origin(self):
        calendar = calendar_fixture()
        source = bullish_bars(calendar)
        actual = zones(source, impulse_policy(calendar), inclusive=True)
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0].origin_bar_start, source[3].start_time)
        self.assertEqual(actual[0].origin_bar_start, BASE + 51 * H1)
        self.assertEqual(actual[0].available_at, source[-1].timestamp)

    def test_finite_origin_lookback_does_not_expand_during_a_closure(self):
        calendar = calendar_fixture(2, 50)
        original = bullish_bars()
        inserted = QuoteBar.from_mid(
            start_time=BASE + H1, timestamp=BASE + 2 * H1,
            available_at=BASE + 2 * H1,
            availability_basis=AvailabilityBasis.SYNTHETIC,
            mid_open=99.0, mid_high=100.0, mid_low=98.5, mid_close=99.5,
            spread=0.2, volume=1.0,
        )
        source = (original[0], inserted) + tuple(
            replace(bar, start_time=bar.start_time + 49 * H1,
                    timestamp=bar.timestamp + 49 * H1,
                    available_at=bar.available_at + 49 * H1)
            for bar in original[1:]
        )
        selected = impulse_policy(calendar)
        start = source[2].start_time
        self.assertTrue(any(event.window_start == start
                            for event in h1_impulse_events(source, policy=selected)))
        self.assertFalse(any(zone.impulse_window_start == start
                             for zone in zones(source, selected, lookback=1)))
        matching = tuple(zone for zone in zones(source, selected, lookback=2)
                         if zone.impulse_window_start == start)
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].origin_bar_start, BASE)

    def test_impulse_and_zone_as_of_never_backdate_delayed_evidence(self):
        calendar = calendar_fixture()
        source = list(bullish_bars(calendar))
        delayed_at = BASE + 60 * H1
        source[0] = replace(source[0], available_at=delayed_at,
                            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT)
        selected = impulse_policy(calendar)
        cutoff = source[-1].timestamp
        self.assertEqual(h1_impulse_events(source, policy=selected, as_of=cutoff), ())
        self.assertEqual(zones(source, selected, as_of=cutoff), ())
        event = h1_impulse_events(source, policy=selected, as_of=delayed_at)[0]
        zone = zones(source, selected, as_of=delayed_at)[0]
        self.assertEqual(event.available_at, delayed_at)
        self.assertEqual(zone.available_at, delayed_at)
        self.assertEqual(event.confirmed_at, cutoff)
        self.assertEqual(zone.formed_at, cutoff)
        self.assertEqual(zone.availability_basis, AvailabilityBasis.OBSERVED_RECEIPT)

    def test_policy_identity_binds_calendar_content_and_semantics(self):
        calendar = calendar_fixture()
        revised = replace(calendar, revision="fixture-v2")
        for factory in (impulse_policy, regime_policy):
            with self.subTest(policy=factory.__name__):
                strict = factory()
                selected = factory(calendar)
                self.assertTrue(selected.canonical_identity.startswith(
                    strict.canonical_identity + ";"
                ))
                self.assertIn(SESSION_STRATEGY_SEMANTICS, selected.canonical_identity)
                self.assertIn(calendar.fingerprint, selected.canonical_identity)
                self.assertNotEqual(selected.fingerprint, strict.fingerprint)
                self.assertNotEqual(selected.fingerprint, factory(revised).fingerprint)
                self.assertEqual(strict.fingerprint, replace(strict, calendar=None).fingerprint)
                self.assertEqual(strict.fingerprint, hashlib.sha256(
                    strict.canonical_identity.encode("ascii")
                ).hexdigest())
                with self.assertRaises(TypeError):
                    factory(object())
                with self.assertRaises(SessionCalendarError):
                    factory(calendar_fixture(3.25, 51))
        strict_zone = zones(bullish_bars(), impulse_policy())[0]
        self.assertIsNone(strict_zone.calendar)
        first = zones(bullish_bars(calendar), impulse_policy(calendar))[0]
        second = zones(bullish_bars(revised), impulse_policy(revised))[0]
        self.assertNotEqual(first.key, second.key)

    def test_detectors_reject_unknown_gaps_closed_bars_and_coverage_shortfall(self):
        calendar = calendar_fixture()
        source = bullish_bars(calendar)
        cases = (
            (source[:1] + source[2:], calendar),
            (source, replace(calendar, closures=())),
            (bullish_bars(), calendar),
            (source, replace(calendar, coverage_end=BASE + 52 * H1)),
        )
        for index, (bars, selected_calendar) in enumerate(cases):
            selected = impulse_policy(selected_calendar)
            with self.subTest(case=index):
                with self.assertRaises(SessionCalendarError):
                    h1_impulse_events(bars, policy=selected)
                with self.assertRaises(SessionCalendarError):
                    zones(bars, selected)

    def test_regime_counts_three_actual_right_wing_bars_and_preserves_structure(self):
        calendar = calendar_fixture(2, 50)
        source = bullish_pivots(calendar)
        actual = h1_regime_events(source, policy=regime_policy(calendar))
        strict = h1_regime_events(bullish_pivots(), policy=regime_policy())
        self.assertEqual(tuple(event.regime for event in actual),
                         tuple(event.regime for event in strict))
        self.assertEqual(actual[-1].regime, MarketRegime.BULLISH)
        self.assertEqual(actual[0].confirmed_at, BASE + 52 * H1)
        self.assertEqual(actual[-1].high_pivots, (source[0], source[2]))
        self.assertEqual(actual[-1].low_pivots, (source[1], source[3]))
        with self.assertRaises(MarketRegimeDataError):
            h1_regime_events(source, policy=regime_policy())
        with self.assertRaises(MarketRegimeDataError):
            h1_regime_events(source, policy=regime_policy(replace(calendar, closures=())))

    def test_generated_pivots_flow_to_regime_without_compressing_time(self):
        calendar = calendar_fixture(4, 52)
        highs = (102, 103, 104, 110, 104, 103, 102)
        source = tuple(QuoteBar.from_mid(
            start_time=BASE + hour * H1,
            timestamp=BASE + (hour + 1) * H1,
            available_at=BASE + (hour + 1) * H1,
            availability_basis=AvailabilityBasis.SYNTHETIC,
            mid_open=100, mid_high=high, mid_low=90, mid_close=100,
            spread=0.2, volume=1,
        ) for hour, high in zip((0, 1, 2, 3, 52, 53, 54), highs))
        pivots = confirmed_h1_pivots(source, calendar=calendar)
        self.assertEqual(len(pivots), 1)
        self.assertEqual(pivots[0].confirmed_at, BASE + 55 * H1)
        selected = regime_policy(calendar)
        self.assertEqual(h1_regime_events(pivots, policy=selected,
                                         as_of=BASE + 54 * H1), ())
        event = h1_regime_events(pivots, policy=selected,
                                 as_of=BASE + 55 * H1)[0]
        self.assertEqual(event.high_pivots, pivots)
        self.assertEqual(event.structure_bar_start, BASE + 3 * H1)

    def test_regime_delayed_evidence_and_future_prefix_stability(self):
        calendar = calendar_fixture(2, 50)
        source = list(bullish_pivots(calendar))
        selected = regime_policy(calendar)
        source[0] = replace(source[0], available_at=BASE + 80 * H1,
                            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT)
        self.assertEqual(h1_regime_events(source, policy=selected,
                                         as_of=BASE + 70 * H1), ())
        events = h1_regime_events(source, policy=selected, as_of=BASE + 80 * H1)
        self.assertEqual(len(events), 4)
        self.assertTrue(all(event.available_at == BASE + 80 * H1 for event in events))
        self.assertEqual(h1_regime_events(source[:2], policy=selected), events[:2])
        revised = replace(calendar, revision="fixture-v2")
        self.assertNotEqual(events[-1].key,
                            h1_regime_events(source, policy=regime_policy(revised))[-1].key)

    def test_regime_rejects_forged_confirmation_inside_or_after_closure(self):
        # Three actual right-wing hours end at 4. A forged later confirmation
        # anywhere in the 4..52 closure must not pass the identical open count.
        calendar = calendar_fixture(4, 52)
        first = bullish_pivots()[0]
        for bad_end in (BASE + 5 * H1, BASE + 52 * H1, BASE + 53 * H1):
            forged = replace(first, confirmed_at=bad_end, available_at=bad_end)
            with self.subTest(confirmation=bad_end):
                with self.assertRaises((SessionCalendarError, MarketRegimeDataError)):
                    h1_regime_events((forged,), policy=regime_policy(calendar))
        self.assertEqual(len(h1_regime_events((first,), policy=regime_policy(calendar))), 1)


if __name__ == "__main__":
    unittest.main()
