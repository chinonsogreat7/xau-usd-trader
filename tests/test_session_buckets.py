"""Exact scheduled boundaries are retained without becoming strategy candles."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
import random
import unittest
from unittest.mock import patch

from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.mt5_ticks import BidAskTick, MT5_COLUMNS, Mt5TickError, ParsedMt5Ticks, aggregate_mt5_ticks_m15
from xau_trader.session_buckets import (
    MAX_SESSION_BUCKETS, SCHEMA_NAME, SessionBucketError,
    aggregate_mt5_session_buckets, validate_session_bucket_aggregation,
)
from xau_trader.session_calendar import ScheduledClosure, SessionCalendarArtifact


START = datetime(2026, 9, 22, 20, tzinfo=timezone.utc)
QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)


def parsed_ticks(times, *, bids=None, flags=None):
    bids = list(bids) if bids is not None else [2000 + i for i in range(len(times))]
    return ParsedMt5Ticks(
        tuple(BidAskTick(at, bid, bid + .25, i + 2, flags=flags) for i, (at, bid) in enumerate(zip(times, bids))),
        "utf-8", "\t", MT5_COLUMNS + (("<FLAGS>",) if flags is not None else ()), "a" * 64,
    )


def calendar(start=START, end=START + HOUR, closures=()):
    return SessionCalendarArtifact(
        calendar_id="synthetic-exact-session-boundaries", revision="fixture-v1",
        provider_name="Synthetic fixture provider", provider_legal_entity="Synthetic fixture entity",
        instrument="XAU_USD", product_form="synthetic_quotes", coverage_start=start, coverage_end=end,
        source_reference="local-fixture://session-buckets", retrieved_at=end, closures=tuple(closures),
    )


def aggregate(times, *, start=START, end=START + HOUR, closures=(), source=None, schedule=None, **kwargs):
    return aggregate_mt5_session_buckets(
        source or parsed_ticks(times), coverage_start=start, coverage_end=end,
        calendar=schedule or calendar(start, end, closures),
        availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION, **kwargs,
    )


class SessionBucketTests(unittest.TestCase):
    def test_2058_close_preserves_thirteen_minute_observations(self):
        end = START + 2 * HOUR
        close = START + timedelta(minutes=58)
        result = aggregate(
            [START, START + QUARTER, START + 2 * QUARTER, START + 3 * QUARTER, close - timedelta(microseconds=1)],
            end=end, closures=[ScheduledClosure(close, end, "Synthetic daily break")],
        )
        bucket = result.buckets[3]
        self.assertEqual(bucket.state, "partial_open")
        self.assertEqual(bucket.observation_status, "observed")
        self.assertEqual(bucket.scheduled_open_microseconds, 13 * 60 * 1000000)
        self.assertEqual(bucket.scheduled_closed_microseconds, 2 * 60 * 1000000)
        self.assertEqual(bucket.tick_count, 2)
        self.assertEqual(bucket.last_tick_at, close - timedelta(microseconds=1))
        self.assertEqual(bucket.observed_ohlc.bid_close, 2004)
        self.assertEqual(bucket.open_segments[0].end_time, close)
        self.assertEqual(bucket.closed_intervals[0].start_time, close)
        self.assertTrue(all(bucket.state == "closed" for bucket in result.buckets[4:]))
        self.assertTrue(all(bucket.observed_ohlc is None for bucket in result.buckets[4:]))
        self.assertNotIsInstance(bucket, QuoteBar)

    def test_sunday_2201_reopen_is_partial_not_shifted_to_2215(self):
        start = datetime(2026, 9, 20, 21, tzinfo=timezone.utc)
        end = start + 2 * HOUR
        reopen = start + HOUR + timedelta(minutes=1)
        result = aggregate(
            [reopen, start + HOUR + QUARTER, start + HOUR + 2 * QUARTER, start + HOUR + 3 * QUARTER],
            start=start, end=end, closures=[ScheduledClosure(start, reopen, "Synthetic weekend closure")],
        )
        self.assertEqual([b.state for b in result.buckets], ["closed"] * 4 + ["partial_open"] + ["full_open"] * 3)
        first = result.buckets[4]
        self.assertEqual(first.start_time, start + HOUR)
        self.assertEqual(first.first_tick_at, reopen)
        self.assertEqual(first.scheduled_open_microseconds, 14 * 60 * 1000000)
        self.assertEqual(first.open_segments[0].start_boundary_silence_microseconds, 0)

    def test_multiple_disjoint_open_fragments_preserve_exact_microseconds(self):
        one = START + timedelta(minutes=2, seconds=1, microseconds=7)
        two = START + timedelta(minutes=3, seconds=2, microseconds=19)
        three = START + timedelta(minutes=5, microseconds=11)
        four = START + timedelta(minutes=8, microseconds=2)
        result = aggregate(
            [START, two, four, START + QUARTER, START + 2 * QUARTER, START + 3 * QUARTER],
            closures=[ScheduledClosure(one, two, "First closure"), ScheduledClosure(three, four, "Second closure")],
        )
        bucket = result.buckets[0]
        self.assertEqual(len(bucket.open_segments), 3)
        self.assertEqual([s.tick_count for s in bucket.open_segments], [1, 1, 1])
        self.assertEqual(bucket.scheduled_closed_microseconds, 241000003)
        self.assertEqual(bucket.scheduled_open_microseconds + bucket.scheduled_closed_microseconds, 900000000)
        self.assertEqual(sum(s.duration_microseconds for s in bucket.open_segments), bucket.scheduled_open_microseconds)
        self.assertEqual(bucket.open_segments[1].start_time, two)
        self.assertEqual(bucket.closed_intervals[1].end_time, four)

    def test_closure_spanning_gap_not_counted_as_open_segment_intertick_gap(self):
        close = START + timedelta(minutes=1)
        reopen = START + timedelta(minutes=14)
        result = aggregate(
            [START, START + timedelta(seconds=2), reopen, reopen + timedelta(seconds=3)],
            closures=[ScheduledClosure(close, reopen, "Scheduled mid-bucket closure")],
        )
        bucket = result.buckets[0]
        self.assertEqual(bucket.observation_status, "observed")
        self.assertEqual([s.maximum_intertick_microseconds for s in bucket.open_segments], [2000000, 3000000])
        self.assertEqual(bucket.observed_ohlc.bid_close, 2003)
        self.assertEqual(bucket.tick_count, 4)

    def test_empty_open_fragment_marks_missing_even_when_other_fragment_has_ticks(self):
        result = aggregate(
            [START], closures=[ScheduledClosure(START + timedelta(minutes=2), START + timedelta(minutes=5), "Synthetic closure")],
        )
        first = result.buckets[0]
        self.assertEqual(first.state, "partial_open")
        self.assertEqual(first.observation_status, "missing_open_data")
        self.assertEqual([s.tick_count for s in first.open_segments], [1, 0])
        self.assertIsNotNone(first.observed_ohlc)
        self.assertIsNone(first.open_segments[1].first_tick_at)
        self.assertIsNone(first.open_segments[1].maximum_intertick_microseconds)
        self.assertEqual(result.buckets[1].state, "full_open")
        self.assertEqual(result.buckets[1].observation_status, "missing_open_data")
        self.assertIsNone(result.buckets[1].observed_ohlc)
        self.assertIsNone(result.buckets[1].source_row_start)

    def test_ticks_at_closure_start_are_rejected_and_at_reopen_permitted(self):
        close = START + timedelta(minutes=5)
        reopen = START + timedelta(minutes=10)
        closure = ScheduledClosure(close, reopen, "Boundary fixture")
        for timestamp in (close, close + timedelta(microseconds=1), reopen - timedelta(microseconds=1)):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(SessionBucketError, "scheduled closure"):
                aggregate([timestamp], closures=[closure])
        result = aggregate([close - timedelta(microseconds=1), reopen], closures=[closure])
        self.assertEqual(result.buckets[0].tick_count, 2)
        self.assertEqual(result.buckets[0].observation_status, "observed")

    def test_ticks_in_fully_closed_bucket_or_trailing_closure_are_rejected(self):
        for close, end, timestamp in (
            (START, START + QUARTER, START),
            (START + timedelta(minutes=10), START + HOUR, START + timedelta(minutes=11)),
            (START, START + HOUR, START + timedelta(minutes=30)),
        ):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(SessionBucketError, "scheduled closure"):
                aggregate([timestamp], closures=[ScheduledClosure(close, end, "Synthetic closed interval")])

    def test_quarter_boundary_tick_belongs_only_to_next_bucket(self):
        result = aggregate([START + QUARTER - timedelta(microseconds=1), START + QUARTER])
        self.assertEqual([b.tick_count for b in result.buckets], [1, 1, 0, 0])
        self.assertEqual(result.buckets[1].first_tick_at, START + QUARTER)
        self.assertEqual(sum(b.tick_count for b in result.buckets), len(result.ticks))

    def test_equal_times_preserve_source_row_order_and_actual_ohlc(self):
        source = parsed_ticks([START, START, START], bids=[2002, 2005, 2000], flags=6)
        result = aggregate([], source=source)
        bucket = result.buckets[0]
        self.assertEqual([t.source_row for t in result.ticks], [2, 3, 4])
        self.assertEqual([t.bid for t in result.ticks], [2002, 2005, 2000])
        self.assertEqual((bucket.source_row_start, bucket.source_row_end), (2, 4))
        self.assertEqual(bucket.observed_ohlc.bid_open, 2002)
        self.assertEqual(bucket.observed_ohlc.bid_high, 2005)
        self.assertEqual(bucket.observed_ohlc.bid_low, 2000)
        self.assertEqual(bucket.observed_ohlc.bid_close, 2000)
        self.assertEqual(bucket.observed_ohlc.ask_close, 2000.25)
        self.assertEqual(bucket.open_segments[0].maximum_intertick_microseconds, 0)
        self.assertEqual([t.flags for t in result.ticks], [6, 6, 6])

    def test_whole_hour_legacy_ohlc_compatibility_without_quote_bar_promotion(self):
        end = START + 3 * HOUR
        closure = ScheduledClosure(START + HOUR, START + 2 * HOUR, "Whole hour break")
        source = parsed_ticks([START + quarter * QUARTER for quarter in range(12) if not 4 <= quarter < 8])
        schedule = calendar(START, end, [closure])
        legacy = aggregate_mt5_ticks_m15(
            source, coverage_start=START, coverage_end=end, calendar=schedule,
            availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
        )
        result = aggregate([], source=source, end=end, schedule=schedule)
        observations = [b for b in result.buckets if b.state == "full_open"]
        self.assertEqual(len(observations), len(legacy.bars))
        for observed, bar in zip(observations, legacy.bars):
            self.assertEqual(observed.start_time, bar.start_time)
            for name in ("bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close"):
                self.assertEqual(getattr(observed.observed_ohlc, name), getattr(bar, name))
        self.assertEqual(len(result.buckets), 12)

    def test_calendar_closure_clips_to_coverage_without_rewriting_calendar(self):
        start = START + HOUR
        end = START + 2 * HOUR
        schedule = calendar(START, START + 3 * HOUR, [ScheduledClosure(START, start + timedelta(minutes=1), "Earlier closure")])
        result = aggregate([start + timedelta(minutes=1)], start=start, end=end, schedule=schedule)
        self.assertEqual(result.buckets[0].scheduled_closed_microseconds, 60000000)
        self.assertEqual(result.calendar.closures[0].start, START)

    def test_input_coverage_rejects_trimming(self):
        for timestamp in (START - timedelta(microseconds=1), START + HOUR):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(SessionBucketError, "trimming"):
                aggregate([timestamp])
        result = aggregate([START, START + HOUR - timedelta(microseconds=1)])
        self.assertEqual(len(result.ticks), 2)

    def test_coverage_requires_finite_calendar_and_whole_utc_hours(self):
        source = parsed_ticks([START])
        for start, end in (
            (START, START), (START, START - HOUR), (START + timedelta(minutes=1), START + HOUR),
            (START, START + HOUR + timedelta(microseconds=1)),
            (START.replace(tzinfo=None), START + HOUR),
            (START.astimezone(timezone(timedelta(hours=1))), START + HOUR),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(SessionBucketError):
                aggregate([], source=source, start=start, end=end, schedule=calendar())
        with self.assertRaisesRegex(SessionBucketError, "finite calendar"):
            aggregate([], source=source, end=START + 2 * HOUR, schedule=calendar())

    def test_bound_is_checked_before_source_copy_or_bucket_allocation(self):
        end = START + timedelta(hours=MAX_SESSION_BUCKETS // 4 + 1)
        with patch("xau_trader.session_buckets._validated_source") as source_check:
            with self.assertRaisesRegex(SessionBucketError, "MAX_SESSION_BUCKETS"):
                aggregate([START], end=end)
        source_check.assert_not_called()
        with patch("xau_trader.session_buckets.MAX_SESSION_BUCKETS", 4):
            self.assertEqual(len(aggregate([START]).buckets), 4)

    def test_empty_parsed_ticks_are_not_silently_a_closed_dataset(self):
        source = parsed_ticks([START])
        object.__setattr__(source, "ticks", ())
        with self.assertRaisesRegex(Mt5TickError, "nonempty"):
            aggregate([], source=source)

    def test_observed_receipt_cannot_be_invented(self):
        for basis in (AvailabilityBasis.OBSERVED_RECEIPT, None, True, "server_time"):
            with self.subTest(basis=basis), self.assertRaises(SessionBucketError):
                aggregate_mt5_session_buckets(parsed_ticks([START]), coverage_start=START, coverage_end=START + HOUR,
                    calendar=calendar(), availability_basis=basis)

    def test_serialization_is_versioned_json_safe_and_has_no_full_tick_payload(self):
        result = aggregate([START])
        payload = result.as_dict()
        json.dumps(payload, allow_nan=False)
        self.assertEqual(payload["schema"], SCHEMA_NAME)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["summary"], {
            "tick_count": 1, "bucket_count": 4, "full_open_count": 4, "partial_open_count": 0,
            "closed_count": 0, "missing_open_data_count": 3, "open_segment_count": 4, "empty_open_segment_count": 3,
        })
        self.assertEqual(payload["calendar_fingerprint"], result.calendar.fingerprint)
        self.assertEqual(payload["raw_sha256"], result.parsed.raw_sha256)
        self.assertNotIn("ticks", payload)
        self.assertNotIn("available_at", payload["buckets"][0])
        self.assertEqual(validate_session_bucket_aggregation(result), result)

    def test_snapshot_does_not_share_mutable_source_dataclass_objects(self):
        source = parsed_ticks([START])
        schedule = calendar()
        result = aggregate([], source=source, schedule=schedule)
        object.__setattr__(source.ticks[0], "bid", 9000)
        object.__setattr__(schedule, "calendar_id", "changed-calendar")
        self.assertEqual(result.ticks[0].bid, 2000)
        self.assertEqual(result.calendar.calendar_id, "synthetic-exact-session-boundaries")
        self.assertEqual(validate_session_bucket_aggregation(result), result)
        with self.assertRaises(FrozenInstanceError):
            result.buckets = ()

    def test_mutable_and_invalid_source_metadata_rejected(self):
        source = parsed_ticks([START])
        object.__setattr__(source, "ticks", list(source.ticks))
        with self.assertRaisesRegex(SessionBucketError, "immutable"):
            aggregate([], source=source)
        source = parsed_ticks([START])
        object.__setattr__(source.ticks[0], "ask", float("nan"))
        with self.assertRaises(Mt5TickError):
            aggregate([], source=source)
        schedule = calendar()
        object.__setattr__(schedule, "closures", [])
        with self.assertRaisesRegex(SessionBucketError, "immutable"):
            aggregate([START], schedule=schedule)

    def test_replaced_bucket_flags_counts_ohlc_and_segment_metadata_are_rejected(self):
        result = aggregate([START])
        first = result.buckets[0]
        for corrupted in (
            replace(first, state="closed"), replace(first, observation_status="missing_open_data"),
            replace(first, tick_count=True), replace(first, tick_count=1.0),
            replace(first, scheduled_open_microseconds=0), replace(first, source_row_start=99),
            replace(first, observed_ohlc=replace(first.observed_ohlc, bid_close=7)),
            replace(first, open_segments=list(first.open_segments)),
            replace(first, open_segments=(replace(first.open_segments[0], tick_count=False),)),
        ):
            changed = replace(result, buckets=(corrupted,) + result.buckets[1:])
            with self.subTest(corrupted=corrupted), self.assertRaisesRegex(SessionBucketError, "does not match"):
                validate_session_bucket_aggregation(changed)
            with self.assertRaises(SessionBucketError):
                changed.as_dict()
        with self.assertRaises(SessionBucketError):
            validate_session_bucket_aggregation(replace(result, buckets=list(result.buckets)))

    def test_randomized_interval_partition_matches_independent_reference(self):
        rng = random.Random(20260923)
        for case in range(30):
            boundaries = sorted(rng.sample(range(1, 3600 * 1000000 - 1), 14))
            closures = [
                ScheduledClosure(START + timedelta(microseconds=left), START + timedelta(microseconds=right), "Synthetic partition")
                for left, right in zip(boundaries[::2], boundaries[1::2])
            ]
            times = [START + timedelta(seconds=second) for second in range(0, 3600, 11)]
            times = [at for at in times if not any(c.start <= at < c.end for c in closures)]
            # START is always open because generated closure boundaries are positive.
            result = aggregate(times, closures=closures)
            with self.subTest(case=case):
                self.assertEqual(sum(b.tick_count for b in result.buckets), len(times))
                for bucket in result.buckets:
                    expected_times = [at for at in times if bucket.start_time <= at < bucket.end_time]
                    self.assertEqual(bucket.tick_count, len(expected_times))
                    intervals = [(s.start_time, s.end_time) for s in bucket.open_segments]
                    intervals += [(s.start_time, s.end_time) for s in bucket.closed_intervals]
                    intervals.sort()
                    self.assertEqual(intervals[0][0], bucket.start_time)
                    self.assertEqual(intervals[-1][1], bucket.end_time)
                    self.assertTrue(all(left[1] == right[0] for left, right in zip(intervals, intervals[1:])))
                    for segment in bucket.open_segments:
                        expected = [at for at in times if segment.start_time <= at < segment.end_time]
                        self.assertEqual(segment.tick_count, len(expected))
                    self.assertEqual(bucket.scheduled_open_microseconds + bucket.scheduled_closed_microseconds, 900000000)
                validate_session_bucket_aggregation(result)


if __name__ == "__main__":
    unittest.main()
