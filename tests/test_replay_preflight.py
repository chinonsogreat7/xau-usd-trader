import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from xau_trader.diagnostic_replay import (
    DiagnosticReplayDataError,
    diagnostic_minimum_input_m15_bars,
    diagnostic_pre_roll_m15_bars,
    run_diagnostic_replay,
)
from xau_trader.domain import AvailabilityBasis, QuoteBar
from xau_trader.replay_preflight import inspect_replay_bars
from xau_trader.research_baseline import provisional_diagnostic_baseline_v1


START = datetime(2026, 1, 5, tzinfo=timezone.utc)
M15 = timedelta(minutes=15)


def series(count, start=START, basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION):
    return tuple(
        QuoteBar.from_mid(
            start_time=start + index * M15,
            timestamp=start + (index + 1) * M15,
            available_at=start + (index + 1) * M15,
            availability_basis=basis,
            mid_open=2000.0,
            mid_high=2001.0,
            mid_low=1999.0,
            mid_close=2000.0,
            spread=0.2,
        )
        for index in range(count)
    )


class ReplayPreflightTests(unittest.TestCase):
    def setUp(self):
        self.bundle = provisional_diagnostic_baseline_v1()
        self.bars = series(100)

    def inspect(self, bars):
        return inspect_replay_bars(bars, self.bundle)

    def codes(self, report):
        return {issue["code"] for issue in report["issues"]}

    def assert_incompatible(self, bars, code):
        report = self.inspect(bars)
        self.assertFalse(report["compatible"])
        self.assertEqual(report["status"], "incompatible")
        self.assertIn(code, self.codes(report))
        with self.assertRaises((DiagnosticReplayDataError, ValueError)):
            run_diagnostic_replay(bars, policy_bundle=self.bundle,
                                  dataset_fingerprint="a" * 64)
        return report

    def test_valid_minimum_matches_replay_and_is_json_safe_without_mutation(self):
        original = tuple(replace(bar) for bar in self.bars)
        report = self.inspect(self.bars)
        self.assertEqual(json.loads(json.dumps(report)), report)
        self.assertEqual(report["schema"], "xau_trader.replay_preflight")
        self.assertEqual(report["version"], 1)
        self.assertEqual(report["status"], "compatible")
        self.assertTrue(report["compatible"])
        self.assertEqual(report["compatibility_scope"], "technical_replay_input_only")
        self.assertEqual(report["policy_bundle_fingerprint"], self.bundle.fingerprint)
        self.assertEqual(report["requirements"], {
            "pre_roll_m15_bars": diagnostic_pre_roll_m15_bars(self.bundle),
            "minimum_input_m15_bars": diagnostic_minimum_input_m15_bars(self.bundle),
            "requires_contiguous_data": True,
        })
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["gaps"], [])
        self.assertEqual(report["contiguous_segments"], [{
            "start": START.isoformat(),
            "end": self.bars[-1].timestamp.isoformat(),
            "row_count": 100,
            "complete_hour_row_count": 100,
            "meets_minimum_history": True,
        }])
        replay = run_diagnostic_replay(self.bars, policy_bundle=self.bundle,
                                       dataset_fingerprint="a" * 64)
        self.assertEqual(replay.m15_bars, self.bars)
        self.assertEqual(self.bars, original)

    def test_empty_input(self):
        report = self.assert_incompatible((), "empty_input")
        self.assertEqual(report["row_count"], 0)
        self.assertEqual(report["contiguous_segments"], [])

    def test_split_sessions_do_not_pool_warmup_history(self):
        bars = series(92) + series(92, START + timedelta(days=1))
        report = self.assert_incompatible(bars, "insufficient_history")
        self.assertIn("data_gaps", self.codes(report))
        self.assertEqual(report["row_count"], 184)
        self.assertEqual(report["gaps"], [{
            "start": (START + timedelta(hours=23)).isoformat(),
            "end": (START + timedelta(days=1)).isoformat(),
            "duration_seconds": 3600.0,
        }])
        self.assertEqual([s["complete_hour_row_count"] for s in report["contiguous_segments"]],
                         [92, 92])
        self.assertFalse(any(s["meets_minimum_history"] for s in report["contiguous_segments"]))

    def test_long_segment_does_not_make_gapped_input_compatible(self):
        bars = self.bars + series(100, START + timedelta(days=2))
        report = self.assert_incompatible(bars, "data_gaps")
        self.assertNotIn("insufficient_history", self.codes(report))
        self.assertTrue(all(s["meets_minimum_history"] for s in report["contiguous_segments"]))

    def test_partial_hours_are_reported_without_trimming_input(self):
        report = self.assert_incompatible(series(100, START + M15), "partial_hour_start")
        self.assertIn("partial_hour_end", self.codes(report))
        self.assertEqual(report["row_count"], 100)
        segment = report["contiguous_segments"][0]
        self.assertEqual(segment["row_count"], 100)
        self.assertEqual(segment["complete_hour_row_count"], 96)
        self.assertFalse(segment["meets_minimum_history"])

    def test_trailing_partial_hour(self):
        report = self.assert_incompatible(series(101), "partial_hour_end")
        self.assertIn("incomplete_hours", self.codes(report))
        self.assertTrue(report["contiguous_segments"][0]["meets_minimum_history"])

    def test_too_short_for_policy(self):
        self.assert_incompatible(series(96), "insufficient_history")

    def test_requirements_follow_policy_bundle(self):
        changed = replace(
            self.bundle,
            origin_lookback_bars=30,
            candidate_policy=replace(self.bundle.candidate_policy,
                                     expected_origin_lookback_bars=30),
        )
        report = inspect_replay_bars(self.bars, changed)
        self.assertFalse(report["compatible"])
        self.assertEqual(report["requirements"]["pre_roll_m15_bars"],
                         diagnostic_pre_roll_m15_bars(changed))
        self.assertEqual(report["requirements"]["minimum_input_m15_bars"],
                         diagnostic_minimum_input_m15_bars(changed))
        self.assertNotEqual(report["policy_bundle_fingerprint"], self.bundle.fingerprint)

    def test_bad_duration_prevents_segment_counting(self):
        bars = list(self.bars)
        bars[0] = replace(bars[0], start_time=START - M15)
        report = self.assert_incompatible(bars, "invalid_duration")
        self.assertEqual(report["contiguous_segments"], [])
        self.assertIn("segments_unavailable", self.codes(report))

    def test_m15_alignment_prevents_segment_counting(self):
        report = self.assert_incompatible(series(100, START + timedelta(minutes=1)),
                                          "invalid_alignment")
        self.assertEqual(report["contiguous_segments"], [])

    def test_subminute_alignment_is_rejected(self):
        for delta in (timedelta(seconds=1), timedelta(microseconds=1)):
            with self.subTest(delta=delta):
                self.assert_incompatible(series(100, START + delta), "invalid_alignment")

    def test_non_utc_values_are_reported(self):
        bars = list(self.bars)
        bars[0] = replace(bars[0])
        # Normal construction normalizes timestamps; simulate a corrupted object.
        object.__setattr__(bars[0], "start_time", START.replace(tzinfo=None))
        report = self.inspect(bars)
        self.assertFalse(report["compatible"])
        self.assertIn("invalid_utc", self.codes(report))
        self.assertEqual(report["contiguous_segments"], [])

    def test_input_timezone_offsets_are_normalized_by_quote_bar(self):
        local_start = START.astimezone(timezone(timedelta(hours=1)))
        self.assertTrue(self.inspect(series(100, local_start))["compatible"])

    def test_out_of_order_and_duplicate_bars_prevent_segment_counting(self):
        for bars in ((self.bars[1], self.bars[0]) + self.bars[2:],
                     (self.bars[0], self.bars[0]) + self.bars[2:]):
            with self.subTest(bars=bars[:2]):
                report = self.assert_incompatible(bars, "invalid_order")
                self.assertEqual(report["contiguous_segments"], [])
                self.assertEqual(report["gaps"], [])

    def test_mixed_availability_basis(self):
        bars = list(self.bars)
        bars[-1] = replace(bars[-1], availability_basis=AvailabilityBasis.OBSERVED_RECEIPT)
        self.assert_incompatible(bars, "mixed_availability_basis")

    def test_historical_and_synthetic_receipts_must_equal_end(self):
        for basis in (AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION,
                      AvailabilityBasis.SYNTHETIC):
            with self.subTest(basis=basis):
                bars = [replace(bar, available_at=bar.timestamp + timedelta(seconds=1))
                        for bar in series(100, basis=basis)]
                self.assert_incompatible(bars, "availability_must_equal_close")

    def test_observed_receipts_may_be_delayed_or_tied(self):
        for bars in (
            tuple(replace(bar, available_at=bar.timestamp + timedelta(seconds=5))
                  for bar in series(100, basis=AvailabilityBasis.OBSERVED_RECEIPT)),
            tuple(replace(bar, available_at=self.bars[-1].timestamp)
                  for bar in series(100, basis=AvailabilityBasis.OBSERVED_RECEIPT)),
        ):
            with self.subTest(first_receipt=bars[0].available_at):
                self.assertTrue(self.inspect(bars)["compatible"])
                run_diagnostic_replay(bars, policy_bundle=self.bundle,
                                      dataset_fingerprint="a" * 64)

    def test_observed_receipts_out_of_order(self):
        bars = list(series(100, basis=AvailabilityBasis.OBSERVED_RECEIPT))
        bars[0] = replace(bars[0], available_at=bars[2].timestamp)
        self.assert_incompatible(bars, "availability_order")

    def test_non_quote_bars_and_invalid_bundle_raise_type_error(self):
        with self.assertRaisesRegex(TypeError, "QuoteBar"):
            self.inspect((self.bars[0], object()))
        with self.assertRaisesRegex(TypeError, "ResearchPolicyBundle"):
            inspect_replay_bars(self.bars, object())


if __name__ == "__main__":
    unittest.main()
