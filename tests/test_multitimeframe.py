import unittest
from datetime import datetime, timedelta, timezone

from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.multitimeframe import (
    MultiTimeframeDataError,
    PivotKind,
    aggregate_m15_to_h1,
    confirmed_h1_pivots,
)


UTC = timezone.utc
BASE = datetime(2026, 1, 5, tzinfo=UTC)


def quote_bar(
    start,
    duration,
    *,
    bid_open,
    bid_high,
    bid_low,
    bid_close,
    available_at=None,
    availability_basis=AvailabilityBasis.SYNTHETIC,
    volume=1.0,
):
    end = start + duration
    return QuoteBar(
        start_time=start,
        timestamp=end,
        available_at=end if available_at is None else available_at,
        availability_basis=availability_basis,
        bid_open=bid_open,
        bid_high=bid_high,
        bid_low=bid_low,
        bid_close=bid_close,
        ask_open=bid_open + 0.2,
        ask_high=bid_high + 0.2,
        ask_low=bid_low + 0.2,
        ask_close=bid_close + 0.2,
        volume=volume,
    )


def m15_bar(index, *, volume=1.0, start_base=BASE):
    start = start_base + timedelta(minutes=15 * index)
    price = 100.0 + index
    return quote_bar(
        start,
        timedelta(minutes=15),
        bid_open=price,
        bid_high=price + 1.0,
        bid_low=price - 1.0,
        bid_close=price + 0.5,
        volume=volume,
    )


def h1_bar(
    index,
    *,
    high,
    low,
    availability_delay=timedelta(0),
    availability_basis=AvailabilityBasis.SYNTHETIC,
):
    start = BASE + timedelta(hours=index)
    middle = (high + low) / 2.0
    end = start + timedelta(hours=1)
    return QuoteBar.from_mid(
        start_time=start,
        timestamp=end,
        available_at=end + availability_delay,
        availability_basis=availability_basis,
        mid_open=middle,
        mid_high=high,
        mid_low=low,
        mid_close=middle,
        spread=0.2,
        volume=10.0,
    )


class M15AggregationTests(unittest.TestCase):
    def test_exact_ohlc_bid_ask_volume_and_availability_aggregation(self):
        children = (
            quote_bar(
                BASE,
                timedelta(minutes=15),
                bid_open=100.0,
                bid_high=103.0,
                bid_low=99.0,
                bid_close=102.0,
                volume=1.0,
            ),
            quote_bar(
                BASE + timedelta(minutes=15),
                timedelta(minutes=15),
                bid_open=102.0,
                bid_high=105.0,
                bid_low=101.0,
                bid_close=104.0,
                available_at=BASE + timedelta(hours=2),
                availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
                volume=2.0,
            ),
            quote_bar(
                BASE + timedelta(minutes=30),
                timedelta(minutes=15),
                bid_open=104.0,
                bid_high=104.5,
                bid_low=98.0,
                bid_close=99.0,
                volume=3.0,
            ),
            quote_bar(
                BASE + timedelta(minutes=45),
                timedelta(minutes=15),
                bid_open=99.0,
                bid_high=101.0,
                bid_low=97.0,
                bid_close=100.0,
                volume=4.0,
            ),
        )

        aggregated = aggregate_m15_to_h1(children)

        self.assertEqual(len(aggregated), 1)
        bar = aggregated[0]
        self.assertEqual(bar.start_time, BASE)
        self.assertEqual(bar.timestamp, BASE + timedelta(hours=1))
        self.assertEqual(
            (bar.bid_open, bar.bid_high, bar.bid_low, bar.bid_close),
            (100.0, 105.0, 97.0, 100.0),
        )
        self.assertEqual(
            (bar.ask_open, bar.ask_high, bar.ask_low, bar.ask_close),
            (100.2, 105.2, 97.2, 100.2),
        )
        self.assertEqual(bar.volume, 10.0)
        self.assertEqual(bar.available_at, BASE + timedelta(hours=2))
        self.assertEqual(bar.availability_basis, AvailabilityBasis.OBSERVED_RECEIPT)

    def test_missing_child_volume_keeps_h1_volume_unknown(self):
        children = [m15_bar(index) for index in range(4)]
        children[2] = m15_bar(2, volume=None)
        self.assertIsNone(aggregate_m15_to_h1(children)[0].volume)

    def test_gap_fails_closed(self):
        children = [m15_bar(index) for index in (0, 1, 3, 4)]
        with self.assertRaisesRegex(MultiTimeframeDataError, "not contiguous"):
            aggregate_m15_to_h1(children)

    def test_non_quarter_hour_alignment_fails_closed(self):
        children = [
            m15_bar(index, start_base=BASE + timedelta(minutes=5)) for index in range(4)
        ]
        with self.assertRaisesRegex(MultiTimeframeDataError, "15-minute"):
            aggregate_m15_to_h1(children)

    def test_partial_hour_fails_closed(self):
        with self.assertRaisesRegex(MultiTimeframeDataError, "end on a UTC hour"):
            aggregate_m15_to_h1([m15_bar(index) for index in range(3)])

    def test_future_complete_hours_do_not_change_existing_aggregation(self):
        first_hour = tuple(m15_bar(index) for index in range(4))
        first_result = aggregate_m15_to_h1(first_hour)
        extended_result = aggregate_m15_to_h1(
            first_hour + tuple(m15_bar(index) for index in range(4, 8))
        )
        self.assertEqual(extended_result[:1], first_result)


class ConfirmedPivotTests(unittest.TestCase):
    def _pivot_high_bars(self):
        return tuple(
            h1_bar(index, high=high, low=5.0)
            for index, high in enumerate((10.0, 11.0, 12.0, 20.0, 13.0, 12.0, 11.0))
        )

    def test_high_pivot_has_three_wings_and_third_right_bar_availability(self):
        bars = list(self._pivot_high_bars())
        bars[-1] = h1_bar(
            6,
            high=11.0,
            low=5.0,
            availability_delay=timedelta(minutes=7),
            availability_basis=AvailabilityBasis.OBSERVED_RECEIPT,
        )

        events = confirmed_h1_pivots(bars)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.kind, PivotKind.HIGH)
        self.assertEqual(event.pivot_bar_start, BASE + timedelta(hours=3))
        self.assertEqual(event.pivot_bar_end, BASE + timedelta(hours=4))
        self.assertEqual(event.confirmed_at, BASE + timedelta(hours=7))
        self.assertEqual(event.available_at, BASE + timedelta(hours=7, minutes=7))
        self.assertEqual(event.availability_basis, AvailabilityBasis.OBSERVED_RECEIPT)
        self.assertEqual((event.left_wing, event.right_wing), (3, 3))
        self.assertAlmostEqual(event.mid_price, 20.0)
        self.assertAlmostEqual(event.bid_price, 19.9)
        self.assertAlmostEqual(event.ask_price, 20.1)

    def test_low_pivot_is_mirrored(self):
        bars = tuple(
            h1_bar(index, high=20.0, low=low)
            for index, low in enumerate((10.0, 9.0, 8.0, 2.0, 7.0, 8.0, 9.0))
        )
        events = confirmed_h1_pivots(bars)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, PivotKind.LOW)
        self.assertAlmostEqual(events[0].mid_price, 2.0)

    def test_no_pivot_is_emitted_before_third_right_bar(self):
        bars = self._pivot_high_bars()
        self.assertEqual(confirmed_h1_pivots(bars[:6]), ())
        self.assertEqual(len(confirmed_h1_pivots(bars)), 1)

    def test_as_of_hides_confirmed_but_not_yet_available_pivot(self):
        bars = list(self._pivot_high_bars())
        bars[-1] = h1_bar(6, high=11.0, low=5.0, availability_delay=timedelta(minutes=7))
        available_at = BASE + timedelta(hours=7, minutes=7)
        self.assertEqual(
            confirmed_h1_pivots(bars, as_of=available_at - timedelta(microseconds=1)),
            (),
        )
        self.assertEqual(len(confirmed_h1_pivots(bars, as_of=available_at)), 1)

    def test_any_delayed_window_bar_gates_event_availability(self):
        bars = list(self._pivot_high_bars())
        bars[1] = h1_bar(1, high=11.0, low=5.0, availability_delay=timedelta(hours=8))
        event = confirmed_h1_pivots(bars)[0]
        self.assertEqual(event.available_at, BASE + timedelta(hours=10))

    def test_equal_high_disqualifies_strict_pivot(self):
        bars = tuple(
            h1_bar(index, high=high, low=5.0)
            for index, high in enumerate((10.0, 11.0, 20.0, 20.0, 13.0, 12.0, 11.0))
        )
        self.assertEqual(confirmed_h1_pivots(bars), ())

    def test_gap_in_h1_input_fails_closed(self):
        bars = list(self._pivot_high_bars())
        bars[4] = h1_bar(5, high=13.0, low=5.0)
        with self.assertRaisesRegex(MultiTimeframeDataError, "not contiguous"):
            confirmed_h1_pivots(bars)

    def test_future_suffix_cannot_change_an_already_emitted_event(self):
        prefix = self._pivot_high_bars()
        original = confirmed_h1_pivots(prefix)
        suffix = tuple(
            h1_bar(index, high=high, low=low)
            for index, high, low in (
                (7, 50.0, 4.0),
                (8, 40.0, 3.0),
                (9, 30.0, 2.0),
            )
        )
        extended = confirmed_h1_pivots(prefix + suffix)
        matching = tuple(
            event for event in extended if event.pivot_bar_start == original[0].pivot_bar_start
        )
        self.assertEqual(matching, original)


if __name__ == "__main__":
    unittest.main()
