import hashlib
import json
import unittest
from dataclasses import replace
from datetime import timedelta

from session_feature_fixture import session_fixture
from xau_trader.diagnostic_replay import run_diagnostic_replay
from xau_trader.domain import AvailabilityBasis
from xau_trader.research_baseline import provisional_diagnostic_baseline_v1
from xau_trader.session_features import run_session_feature_replay


class SessionFeatureReplayTests(unittest.TestCase):
    def setUp(self):
        self.bars, self.calendar = session_fixture()
        self.bundle = provisional_diagnostic_baseline_v1()

    def run_features(self, bars=None, calendar=None, bundle=None):
        return run_session_feature_replay(
            self.bars if bars is None else bars,
            calendar=self.calendar if calendar is None else calendar,
            policy_bundle=self.bundle if bundle is None else bundle,
            dataset_fingerprint="a" * 64,
        )

    def test_two_sessions_compute_continuous_features_but_never_candidates(self):
        original = tuple(replace(bar) for bar in self.bars)
        report = self.run_features()
        self.assertEqual(report["status"], "session_features_complete")
        self.assertTrue(report["diagnostic_only"])
        self.assertIsNone(report["execution"])
        self.assertFalse(report["strategy_candidates_generated"])
        self.assertFalse(report["full_strategy_replay_supported"])
        self.assertNotIn("decisions", report)
        self.assertNotIn("zones", report)
        self.assertEqual(report["counts"]["m15_bars"], 184)
        self.assertEqual(report["counts"]["h1_bars"], 46)
        self.assertEqual(report["counts"]["scheduled_gaps"], 1)
        self.assertEqual(report["counts"]["ema_events"], 177)
        self.assertEqual(report["counts"]["atr_events"], 33)
        self.assertTrue(report["warmup"]["atr_seeded"])
        self.assertTrue(report["warmup"]["ema_seeded"])
        self.assertEqual(len(report["m15_bars"]), len(self.bars))
        self.assertEqual(self.bars, original)
        self.assertEqual(json.loads(json.dumps(report)), report)
        # No synthetic H1 candle covers the closed hour.
        starts = [row["start_time"] for row in report["h1_bars"]]
        closed_start = self.calendar.closures[0].start.isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        self.assertNotIn(closed_start, starts)
        with self.assertRaises(ValueError):
            run_diagnostic_replay(
                self.bars, policy_bundle=self.bundle, dataset_fingerprint="a" * 64,
            )

    def test_report_fingerprint_binds_calendar_inputs_policy_and_features(self):
        report = self.run_features()
        self.assertEqual(report, self.run_features())
        unsigned = {key: value for key, value in report.items() if key != "fingerprint"}
        expected = hashlib.sha256(json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest()
        self.assertEqual(report["fingerprint"], expected)
        changed = self.run_features(calendar=replace(
            self.calendar, source_reference="local-fixture://revised-evidence",
        ))
        self.assertEqual(report["h1_atr"], changed["h1_atr"])
        self.assertNotEqual(report["calendar_fingerprint"], changed["calendar_fingerprint"])
        self.assertNotEqual(report["fingerprint"], changed["fingerprint"])
        changed = self.run_features(bundle=replace(
            self.bundle, confirmation_policy=replace(self.bundle.confirmation_policy, ema_period=9),
        ))
        self.assertNotEqual(report["policy_bundle_fingerprint"], changed["policy_bundle_fingerprint"])
        self.assertNotEqual(report["fingerprint"], changed["fingerprint"])

    def test_future_suffix_does_not_rewrite_visible_prefix_features(self):
        prefix = self.run_features(bars=self.bars[:100])
        complete = self.run_features()
        cutoff = prefix["m15_bars"][-1]["timestamp"]
        for field in ("h1_atr", "m15_ema", "h1_pivots"):
            with self.subTest(field=field):
                visible = [event for event in complete[field] if event["available_at"] <= cutoff]
                self.assertEqual(prefix[field], visible)

    def test_missing_open_hour_and_undeclared_closure_are_rejected(self):
        with self.assertRaises(ValueError):
            self.run_features(bars=self.bars[:20] + self.bars[24:])
        with self.assertRaises(ValueError):
            self.run_features(calendar=replace(self.calendar, closures=()))

    def test_short_feature_history_is_labeled_unseeded_not_strategy_ready(self):
        report = self.run_features(bars=self.bars[:4])
        self.assertEqual(report["counts"]["h1_bars"], 1)
        self.assertFalse(report["warmup"]["atr_seeded"])
        self.assertFalse(report["warmup"]["ema_seeded"])
        self.assertFalse(report["warmup"]["full_strategy_readiness_assessed"])

    def test_bad_availability_is_rejected_and_delayed_receipts_are_not_backdated(self):
        late = tuple(replace(bar, available_at=bar.timestamp + timedelta(seconds=5))
                     for bar in self.bars)
        with self.assertRaisesRegex(ValueError, "availability must equal"):
            self.run_features(bars=late)
        observed = tuple(replace(bar, availability_basis=AvailabilityBasis.OBSERVED_RECEIPT)
                         for bar in late)
        report = self.run_features(bars=observed)
        self.assertGreater(report["m15_ema"][0]["available_at"],
                           report["m15_ema"][0]["bar_end"])
        bad_order = (replace(observed[0], available_at=observed[2].available_at),) + observed[1:]
        with self.assertRaisesRegex(ValueError, "non-decreasing"):
            self.run_features(bars=bad_order)
        with self.assertRaisesRegex(ValueError, "one availability basis"):
            self.run_features(bars=(observed[0],) + self.bars[1:])

    def test_empty_and_invalid_identity_or_types_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-empty"):
            self.run_features(bars=())
        with self.assertRaisesRegex(TypeError, "QuoteBar"):
            self.run_features(bars=(object(),))
        for kwargs, error in (({"calendar": object()}, TypeError),
                              ({"policy_bundle": object()}, TypeError),
                              ({"dataset_fingerprint": "fake"}, ValueError)):
            args = dict(calendar=self.calendar, policy_bundle=self.bundle, dataset_fingerprint="a" * 64)
            args.update(kwargs)
            with self.assertRaises(error):
                run_session_feature_replay(self.bars, **args)


if __name__ == "__main__":
    unittest.main()
