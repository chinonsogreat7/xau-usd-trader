import unittest
from datetime import datetime, timedelta, timezone

from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.multitimeframe import MultiTimeframeDataError
from xau_trader.supply_demand import (
    AtrMethod,
    AtrTiming,
    ImpulseDirection,
    ImpulseMovementPolicy,
    ImpulsePolicy,
    OriginSelectionPolicy,
    ZoneKind,
    h1_atr_events,
    h1_impulse_events,
    supply_demand_zone_events,
)


UTC = timezone.utc
BASE = datetime(2026, 1, 5, tzinfo=UTC)


def h1_bar(
    index,
    *,
    open_price,
    high,
    low,
    close,
    availability_delay=timedelta(0),
    availability_basis=AvailabilityBasis.SYNTHETIC,
):
    start = BASE + timedelta(hours=index)
    end = start + timedelta(hours=1)
    return QuoteBar.from_mid(
        start_time=start,
        timestamp=end,
        available_at=end + availability_delay,
        availability_basis=availability_basis,
        mid_open=open_price,
        mid_high=high,
        mid_low=low,
        mid_close=close,
        spread=0.2,
        volume=10.0,
    )


def impulse_policy(
    *,
    movement=ImpulseMovementPolicy.NET_OPEN_TO_CLOSE,
    atr_method=AtrMethod.WILDER_SMA_SEED,
    atr_timing=AtrTiming.BEFORE_IMPULSE_WINDOW,
    atr_period=1,
    movement_atr_multiple=1.5,
    body_atr_multiple=0.8,
):
    return ImpulsePolicy(
        movement=movement,
        atr_method=atr_method,
        atr_timing=atr_timing,
        atr_period=atr_period,
        minimum_directional_bars=3,
        movement_atr_multiple=movement_atr_multiple,
        body_atr_multiple=body_atr_multiple,
    )


def bullish_fixture():
    return (
        h1_bar(0, open_price=100.0, high=101.0, low=98.0, close=99.0),
        h1_bar(1, open_price=99.0, high=101.5, low=98.5, close=101.0),
        h1_bar(2, open_price=101.0, high=103.5, low=100.5, close=103.0),
        h1_bar(3, open_price=103.0, high=103.5, low=102.0, close=102.5),
        h1_bar(4, open_price=102.5, high=105.5, low=102.0, close=105.0),
    )


def bearish_fixture():
    return (
        h1_bar(0, open_price=100.0, high=102.0, low=99.5, close=101.0),
        h1_bar(1, open_price=101.0, high=101.5, low=98.5, close=99.0),
        h1_bar(2, open_price=99.0, high=99.5, low=96.5, close=97.0),
        h1_bar(3, open_price=97.0, high=98.0, low=96.5, close=97.5),
        h1_bar(4, open_price=97.5, high=98.0, low=94.0, close=94.5),
    )


class AtrEventTests(unittest.TestCase):
    def _varying_range_bars(self):
        return (
            h1_bar(0, open_price=9.0, high=10.0, low=8.0, close=9.0),
            h1_bar(1, open_price=9.0, high=12.0, low=9.0, close=11.0),
            h1_bar(2, open_price=11.0, high=13.0, low=10.0, close=12.0),
        )

    def test_sma_and_wilder_methods_have_explicit_deterministic_values(self):
        bars = self._varying_range_bars()

        simple = h1_atr_events(
            bars, period=2, method=AtrMethod.SIMPLE_MOVING_AVERAGE
        )
        wilder = h1_atr_events(bars, period=2, method=AtrMethod.WILDER_SMA_SEED)

        self.assertEqual(len(simple), 2)
        self.assertEqual(len(wilder), 2)
        self.assertAlmostEqual(simple[0].value, 2.5)
        self.assertAlmostEqual(wilder[0].value, 2.5)
        self.assertAlmostEqual(simple[1].value, 3.0)
        self.assertAlmostEqual(wilder[1].value, 2.75)
        self.assertAlmostEqual(simple[1].true_range, 3.0)

    def test_atr_is_not_emitted_until_period_is_warm(self):
        bars = self._varying_range_bars()
        self.assertEqual(
            h1_atr_events(bars[:2], period=3, method=AtrMethod.WILDER_SMA_SEED),
            (),
        )
        self.assertEqual(
            len(
                h1_atr_events(
                    bars, period=3, method=AtrMethod.WILDER_SMA_SEED
                )
            ),
            1,
        )

    def test_sma_availability_drops_an_expired_delayed_dependency(self):
        bars = list(self._varying_range_bars())
        bars.append(
            h1_bar(3, open_price=12.0, high=14.0, low=11.0, close=13.0)
        )
        bars[0] = h1_bar(
            0,
            open_price=9.0,
            high=10.0,
            low=8.0,
            close=9.0,
            availability_delay=timedelta(hours=20),
            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
        )
        as_of = BASE + timedelta(hours=5)

        visible = h1_atr_events(
            bars,
            period=2,
            method=AtrMethod.SIMPLE_MOVING_AVERAGE,
            as_of=as_of,
        )

        # ATR at bar 3 needs TR[2:4], which needs bars 1:4, not delayed bar 0.
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].bar_end, BASE + timedelta(hours=4))
        self.assertEqual(visible[0].available_at, BASE + timedelta(hours=4))

    def test_wilder_availability_retains_seed_dependencies(self):
        bars = list(self._varying_range_bars())
        bars.append(
            h1_bar(3, open_price=12.0, high=14.0, low=11.0, close=13.0)
        )
        bars[0] = h1_bar(
            0,
            open_price=9.0,
            high=10.0,
            low=8.0,
            close=9.0,
            availability_delay=timedelta(hours=20),
        )
        self.assertEqual(
            h1_atr_events(
                bars,
                period=2,
                method=AtrMethod.WILDER_SMA_SEED,
                as_of=BASE + timedelta(hours=5),
            ),
            (),
        )


class ImpulseEventTests(unittest.TestCase):
    def test_bullish_four_bar_impulse_is_auditable(self):
        policy = impulse_policy()
        event = h1_impulse_events(bullish_fixture(), policy=policy)[0]

        self.assertEqual(event.direction, ImpulseDirection.BULLISH)
        self.assertEqual(event.window_start, BASE + timedelta(hours=1))
        self.assertEqual(event.window_end, BASE + timedelta(hours=5))
        self.assertEqual(event.confirmed_at, BASE + timedelta(hours=5))
        self.assertEqual(event.available_at, BASE + timedelta(hours=5))
        self.assertEqual(event.directional_bar_count, 3)
        self.assertEqual(event.minimum_directional_bars, 3)
        self.assertEqual(event.policy_fingerprint, policy.fingerprint)
        self.assertEqual(
            event.key,
            (
                policy.fingerprint,
                ImpulseDirection.BULLISH,
                BASE + timedelta(hours=1),
                BASE + timedelta(hours=5),
            ),
        )
        self.assertAlmostEqual(event.atr_value, 3.0)
        self.assertAlmostEqual(event.directional_movement, 6.0)
        self.assertAlmostEqual(event.largest_body, 2.5)

    def test_bearish_four_bar_impulse_is_mirrored(self):
        events = h1_impulse_events(bearish_fixture(), policy=impulse_policy())

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].direction, ImpulseDirection.BEARISH)
        self.assertEqual(events[0].directional_bar_count, 3)
        self.assertAlmostEqual(events[0].directional_movement, 6.5)

    def test_full_range_policy_can_qualify_when_net_policy_does_not(self):
        bars = (
            h1_bar(0, open_price=100.0, high=102.5, low=97.5, close=99.0),
            h1_bar(1, open_price=100.0, high=106.0, low=99.0, close=102.0),
            h1_bar(2, open_price=102.0, high=105.0, low=101.0, close=104.0),
            h1_bar(3, open_price=104.0, high=107.0, low=103.0, close=106.0),
            h1_bar(4, open_price=106.0, high=106.5, low=99.5, close=100.5),
        )
        net = impulse_policy(body_atr_multiple=0.0, movement_atr_multiple=1.0)
        full_range = impulse_policy(
            movement=ImpulseMovementPolicy.FULL_WINDOW_RANGE,
            body_atr_multiple=0.0,
            movement_atr_multiple=1.0,
        )
        directional_bodies = impulse_policy(
            movement=ImpulseMovementPolicy.SUM_DIRECTIONAL_BODIES,
            body_atr_multiple=0.0,
            movement_atr_multiple=1.0,
        )

        self.assertEqual(h1_impulse_events(bars, policy=net), ())
        events = h1_impulse_events(bars, policy=full_range)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0].directional_movement, 8.0)
        body_events = h1_impulse_events(bars, policy=directional_bodies)
        self.assertEqual(len(body_events), 1)
        self.assertAlmostEqual(body_events[0].directional_movement, 6.0)

    def test_atr_timing_is_an_explicit_material_policy(self):
        bars = (
            h1_bar(0, open_price=100.0, high=100.5, low=99.5, close=99.8),
            h1_bar(1, open_price=100.0, high=102.0, low=100.0, close=102.0),
            h1_bar(2, open_price=102.0, high=104.0, low=102.0, close=104.0),
            h1_bar(3, open_price=104.0, high=104.5, low=103.0, close=103.5),
            h1_bar(4, open_price=103.5, high=110.0, low=100.0, close=106.0),
        )
        before = impulse_policy(body_atr_multiple=0.0)
        inclusive = impulse_policy(
            atr_timing=AtrTiming.IMPULSE_END_INCLUSIVE,
            body_atr_multiple=0.0,
        )
        before_end = impulse_policy(
            atr_timing=AtrTiming.BEFORE_IMPULSE_END,
            body_atr_multiple=0.0,
        )

        self.assertEqual(len(h1_impulse_events(bars, policy=before)), 1)
        before_end_event = h1_impulse_events(bars, policy=before_end)[0]
        self.assertEqual(
            before_end_event.atr_snapshot_bar_end, BASE + timedelta(hours=4)
        )
        self.assertEqual(h1_impulse_events(bars, policy=inclusive), ())

    def test_delayed_dependency_controls_as_of_visibility_and_basis(self):
        bars = list(bullish_fixture())
        bars[2] = h1_bar(
            2,
            open_price=101.0,
            high=103.5,
            low=100.5,
            close=103.0,
            availability_delay=timedelta(hours=8),
            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
        )
        available_at = BASE + timedelta(hours=11)

        self.assertEqual(
            h1_impulse_events(
                bars,
                policy=impulse_policy(),
                as_of=available_at - timedelta(microseconds=1),
            ),
            (),
        )
        event = h1_impulse_events(
            bars, policy=impulse_policy(), as_of=available_at
        )[0]
        self.assertEqual(event.available_at, available_at)
        self.assertEqual(event.availability_basis, AvailabilityBasis.OBSERVED_RECEIPT)

    def test_gap_fails_closed(self):
        bars = list(bullish_fixture())
        bars.pop(2)
        with self.assertRaisesRegex(MultiTimeframeDataError, "not contiguous"):
            h1_impulse_events(bars, policy=impulse_policy())

    def test_future_suffix_cannot_change_an_existing_impulse(self):
        prefix = bullish_fixture()
        original = h1_impulse_events(prefix, policy=impulse_policy())
        suffix = (
            h1_bar(5, open_price=105.0, high=106.0, low=104.0, close=105.5),
            h1_bar(6, open_price=105.5, high=107.0, low=105.0, close=106.5),
        )

        extended = h1_impulse_events(prefix + suffix, policy=impulse_policy())
        matching = tuple(
            event
            for event in extended
            if event.window_start == original[0].window_start
        )
        self.assertEqual(matching, original)

    def test_complete_policy_fingerprint_prevents_event_key_collisions(self):
        baseline = impulse_policy(movement_atr_multiple=1.5)
        lenient = impulse_policy(movement_atr_multiple=1.0)

        baseline_event = h1_impulse_events(
            bullish_fixture(), policy=baseline
        )[0]
        lenient_event = h1_impulse_events(bullish_fixture(), policy=lenient)[0]

        self.assertNotEqual(baseline.fingerprint, lenient.fingerprint)
        self.assertNotEqual(baseline_event.key, lenient_event.key)
        self.assertEqual(
            impulse_policy(movement_atr_multiple=1).fingerprint,
            impulse_policy(movement_atr_multiple=1.0).fingerprint,
        )


class ZoneEventTests(unittest.TestCase):
    def test_bullish_impulse_creates_demand_from_prior_bearish_origin(self):
        zones = supply_demand_zone_events(
            bullish_fixture(),
            impulse_policy=impulse_policy(),
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=8,
        )

        self.assertEqual(len(zones), 1)
        zone = zones[0]
        self.assertEqual(zone.kind, ZoneKind.DEMAND)
        self.assertEqual(zone.origin_bar_start, BASE)
        self.assertEqual(zone.formed_at, BASE + timedelta(hours=5))
        self.assertAlmostEqual(zone.lower_price, 98.0)
        self.assertAlmostEqual(zone.upper_price, 100.0)
        self.assertEqual(zone.key[0], ZoneKind.DEMAND)
        self.assertEqual(
            zone.impulse_key,
            h1_impulse_events(
                bullish_fixture(), policy=impulse_policy()
            )[0].key,
        )

    def test_bearish_impulse_creates_supply_from_prior_bullish_origin(self):
        zones = supply_demand_zone_events(
            bearish_fixture(),
            impulse_policy=impulse_policy(),
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=8,
        )

        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0].kind, ZoneKind.SUPPLY)
        self.assertAlmostEqual(zones[0].lower_price, 100.0)
        self.assertAlmostEqual(zones[0].upper_price, 102.0)

    def test_origin_policy_selects_before_window_or_inside_window(self):
        bars = (
            h1_bar(0, open_price=100.0, high=101.0, low=97.0, close=99.0),
            h1_bar(1, open_price=99.0, high=101.5, low=98.5, close=101.0),
            h1_bar(2, open_price=101.0, high=101.5, low=99.5, close=100.5),
            h1_bar(3, open_price=100.5, high=103.5, low=100.0, close=103.0),
            h1_bar(4, open_price=103.0, high=105.5, low=102.5, close=105.0),
        )
        policy = impulse_policy(
            movement_atr_multiple=0.0, body_atr_multiple=0.0
        )

        before = supply_demand_zone_events(
            bars,
            impulse_policy=policy,
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=8,
        )[0]
        inclusive = supply_demand_zone_events(
            bars,
            impulse_policy=policy,
            origin_policy=(
                OriginSelectionPolicy.LAST_OPPOSITE_AT_OR_BEFORE_WINDOW_END
            ),
            origin_lookback_bars=8,
        )[0]

        self.assertEqual(before.origin_bar_start, BASE)
        self.assertEqual(inclusive.origin_bar_start, BASE + timedelta(hours=2))
        self.assertAlmostEqual(before.lower_price, 97.0)
        self.assertAlmostEqual(inclusive.lower_price, 99.5)
        self.assertNotEqual(before.key, inclusive.key)

    def test_missing_opposite_origin_emits_no_zone(self):
        bars = (
            h1_bar(0, open_price=99.0, high=101.0, low=98.5, close=100.0),
            h1_bar(1, open_price=100.0, high=102.0, low=99.5, close=101.5),
            h1_bar(2, open_price=101.5, high=103.0, low=101.0, close=102.5),
            h1_bar(3, open_price=102.5, high=104.0, low=102.0, close=103.5),
            h1_bar(4, open_price=103.5, high=105.0, low=103.0, close=104.5),
        )
        policy = impulse_policy(
            movement_atr_multiple=0.0, body_atr_multiple=0.0
        )

        self.assertEqual(
            supply_demand_zone_events(
                bars,
                impulse_policy=policy,
                origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
                origin_lookback_bars=8,
            ),
            (),
        )

    def test_finite_lookback_excludes_an_ancient_origin(self):
        bars = (
            h1_bar(0, open_price=100.0, high=101.0, low=98.0, close=99.0),
            h1_bar(1, open_price=99.0, high=100.0, low=98.5, close=99.0),
            h1_bar(2, open_price=99.0, high=100.0, low=98.5, close=99.0),
            h1_bar(3, open_price=99.0, high=101.5, low=98.5, close=101.0),
            h1_bar(4, open_price=101.0, high=103.5, low=100.5, close=103.0),
            h1_bar(5, open_price=103.0, high=103.5, low=102.0, close=102.5),
            h1_bar(6, open_price=102.5, high=105.5, low=102.0, close=105.0),
        )
        policy = impulse_policy(
            movement_atr_multiple=0.0, body_atr_multiple=0.0
        )

        excluded = supply_demand_zone_events(
            bars,
            impulse_policy=policy,
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=2,
        )
        included = supply_demand_zone_events(
            bars,
            impulse_policy=policy,
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=3,
        )

        self.assertEqual(excluded, ())
        self.assertEqual(len(included), 1)
        self.assertEqual(included[0].origin_bar_start, BASE)
        self.assertEqual(included[0].origin_lookback_bars, 3)

    def test_zone_visibility_includes_a_delayed_origin(self):
        bars = list(bullish_fixture())
        bars[0] = h1_bar(
            0,
            open_price=100.0,
            high=101.0,
            low=98.0,
            close=99.0,
            availability_delay=timedelta(hours=10),
            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
        )
        available_at = BASE + timedelta(hours=11)

        hidden = supply_demand_zone_events(
            bars,
            impulse_policy=impulse_policy(),
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=8,
            as_of=available_at - timedelta(microseconds=1),
        )
        visible = supply_demand_zone_events(
            bars,
            impulse_policy=impulse_policy(),
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=8,
            as_of=available_at,
        )

        self.assertEqual(hidden, ())
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].available_at, available_at)
        self.assertEqual(
            visible[0].availability_basis, AvailabilityBasis.OBSERVED_RECEIPT
        )

    def test_origin_proof_waits_for_a_delayed_intervening_candle(self):
        bars = (
            h1_bar(0, open_price=100.0, high=101.0, low=98.0, close=99.0),
            h1_bar(
                1,
                open_price=99.0,
                high=101.0,
                low=98.5,
                close=100.0,
                availability_delay=timedelta(hours=10),
                availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
            ),
            h1_bar(2, open_price=100.0, high=102.5, low=99.5, close=102.0),
            h1_bar(3, open_price=102.0, high=104.5, low=101.5, close=104.0),
            h1_bar(4, open_price=104.0, high=104.5, low=103.0, close=103.5),
            h1_bar(5, open_price=103.5, high=106.5, low=103.0, close=106.0),
        )
        policy = impulse_policy(
            atr_timing=AtrTiming.IMPULSE_END_INCLUSIVE,
            movement_atr_multiple=0.0,
            body_atr_multiple=0.0,
        )
        target_start = BASE + timedelta(hours=2)
        delayed_at = BASE + timedelta(hours=12)

        visible_impulses = h1_impulse_events(
            bars,
            policy=policy,
            as_of=BASE + timedelta(hours=6),
        )
        self.assertEqual(
            tuple(event.window_start for event in visible_impulses),
            (target_start,),
        )
        hidden_zones = supply_demand_zone_events(
            bars,
            impulse_policy=policy,
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=2,
            as_of=delayed_at - timedelta(microseconds=1),
        )
        visible_zones = supply_demand_zone_events(
            bars,
            impulse_policy=policy,
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=2,
            as_of=delayed_at,
        )

        self.assertEqual(
            tuple(
                zone
                for zone in hidden_zones
                if zone.impulse_window_start == target_start
            ),
            (),
        )
        target_zones = tuple(
            zone
            for zone in visible_zones
            if zone.impulse_window_start == target_start
        )
        self.assertEqual(len(target_zones), 1)
        self.assertEqual(target_zones[0].origin_bar_start, BASE)
        self.assertEqual(target_zones[0].available_at, delayed_at)
        self.assertEqual(
            target_zones[0].availability_basis,
            AvailabilityBasis.OBSERVED_RECEIPT,
        )

    def test_zone_key_binds_the_complete_originating_impulse_policy(self):
        baseline = impulse_policy(movement_atr_multiple=1.5)
        lenient = impulse_policy(movement_atr_multiple=1.0)
        baseline_zone = supply_demand_zone_events(
            bullish_fixture(),
            impulse_policy=baseline,
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=8,
        )[0]
        lenient_zone = supply_demand_zone_events(
            bullish_fixture(),
            impulse_policy=lenient,
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=8,
        )[0]

        self.assertEqual(
            baseline_zone.impulse_window_start,
            lenient_zone.impulse_window_start,
        )
        self.assertEqual(baseline_zone.origin_bar_start, lenient_zone.origin_bar_start)
        self.assertNotEqual(baseline_zone.impulse_key, lenient_zone.impulse_key)
        self.assertNotEqual(baseline_zone.key, lenient_zone.key)

    def test_gap_fails_closed_before_zone_formation(self):
        bars = list(bearish_fixture())
        bars.pop(1)
        with self.assertRaisesRegex(MultiTimeframeDataError, "not contiguous"):
            supply_demand_zone_events(
                bars,
                impulse_policy=impulse_policy(),
                origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
                origin_lookback_bars=8,
            )

    def test_future_suffix_cannot_change_an_existing_zone(self):
        prefix = bullish_fixture()
        original = supply_demand_zone_events(
            prefix,
            impulse_policy=impulse_policy(),
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=8,
        )
        suffix = (
            h1_bar(5, open_price=105.0, high=106.0, low=104.0, close=105.5),
            h1_bar(6, open_price=105.5, high=107.0, low=105.0, close=106.5),
        )

        extended = supply_demand_zone_events(
            prefix + suffix,
            impulse_policy=impulse_policy(),
            origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
            origin_lookback_bars=8,
        )
        matching = tuple(zone for zone in extended if zone.key == original[0].key)
        self.assertEqual(matching, original)


class PolicyValidationTests(unittest.TestCase):
    def test_policy_enums_are_required_not_silent_string_defaults(self):
        with self.assertRaisesRegex(TypeError, "movement must be"):
            ImpulsePolicy(
                movement="net_open_to_close",
                atr_method=AtrMethod.WILDER_SMA_SEED,
                atr_timing=AtrTiming.BEFORE_IMPULSE_WINDOW,
                atr_period=14,
                minimum_directional_bars=3,
                movement_atr_multiple=1.5,
                body_atr_multiple=0.8,
            )

    def test_naive_as_of_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            h1_impulse_events(
                bullish_fixture(),
                policy=impulse_policy(),
                as_of=datetime(2026, 1, 5),
            )

    def test_origin_lookback_must_be_a_positive_explicit_integer(self):
        with self.assertRaisesRegex(ValueError, "at least 1"):
            supply_demand_zone_events(
                bullish_fixture(),
                impulse_policy=impulse_policy(),
                origin_policy=OriginSelectionPolicy.LAST_OPPOSITE_BEFORE_WINDOW,
                origin_lookback_bars=0,
            )


if __name__ == "__main__":
    unittest.main()
