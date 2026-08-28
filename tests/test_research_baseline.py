import unittest
from dataclasses import FrozenInstanceError, replace

from xau_trader.candidate_signals import CandidatePolicy
from xau_trader.m15_confirmation import EmaInitialization
from xau_trader.research_baseline import (
    BaselineStatus,
    OPERATOR_DOCUMENT_SHA256,
    ResearchPolicyBundle,
    provisional_diagnostic_baseline_v1,
)
from xau_trader.supply_demand import AtrTiming, ImpulseMovementPolicy
from xau_trader.zone_lifecycle import EqualTimeOrder, RetestPolicy


class ResearchPolicyBundleTests(unittest.TestCase):
    def test_named_baseline_selects_every_predeclared_choice(self):
        bundle = provisional_diagnostic_baseline_v1()

        self.assertEqual(bundle.status, BaselineStatus.PROVISIONAL_UNAPPROVED)
        self.assertEqual(bundle.source_sha256, OPERATOR_DOCUMENT_SHA256)
        self.assertEqual(
            bundle.impulse_policy.movement,
            ImpulseMovementPolicy.NET_OPEN_TO_CLOSE,
        )
        self.assertEqual(
            bundle.impulse_policy.atr_timing,
            AtrTiming.BEFORE_IMPULSE_WINDOW,
        )
        self.assertEqual(bundle.impulse_policy.atr_period, 14)
        self.assertEqual(bundle.origin_lookback_bars, 20)
        self.assertEqual(
            bundle.lifecycle_policy.retest_policy,
            RetestPolicy.FIRST_TOUCH_ONLY,
        )
        self.assertEqual(
            bundle.lifecycle_policy.equal_time_order,
            EqualTimeOrder.H1_INVALIDATION_THEN_M15_TOUCH,
        )
        self.assertEqual(bundle.confirmation_policy.ema_period, 8)
        self.assertEqual(
            bundle.confirmation_policy.ema_initialization,
            EmaInitialization.SMA_PERIOD_SEED,
        )
        self.assertEqual(bundle.confirmation_policy.confirmation_expiry_bars, 2)

    def test_bundle_fingerprint_binds_source_and_every_child_policy(self):
        bundle = provisional_diagnostic_baseline_v1()

        self.assertEqual(len(bundle.fingerprint), 64)
        self.assertIn("source_sha256={}".format(OPERATOR_DOCUMENT_SHA256), bundle.canonical_identity)
        changed_confirmation = replace(
            bundle.confirmation_policy,
            ema_initialization=EmaInitialization.FIRST_CLOSE_SEED,
        )
        changed = replace(bundle, confirmation_policy=changed_confirmation)
        self.assertNotEqual(changed.fingerprint, bundle.fingerprint)

    def test_candidate_formation_binding_cannot_drift_from_bundle(self):
        bundle = provisional_diagnostic_baseline_v1()
        mismatched = replace(
            bundle.candidate_policy,
            expected_origin_lookback_bars=bundle.origin_lookback_bars + 1,
        )

        with self.assertRaisesRegex(ValueError, "origin lookback"):
            replace(bundle, candidate_policy=mismatched)
        with self.assertRaisesRegex(ValueError, "impulse policy"):
            replace(
                bundle,
                candidate_policy=replace(
                    bundle.candidate_policy,
                    expected_impulse_policy_fingerprint="f" * 64,
                ),
            )

    def test_bundle_has_no_constructor_defaults_and_is_immutable(self):
        with self.assertRaises(TypeError):
            ResearchPolicyBundle()
        with self.assertRaises(TypeError):
            replace(
                provisional_diagnostic_baseline_v1(),
                status=BaselineStatus.PROVISIONAL_UNAPPROVED.value,
            )
        with self.assertRaises(FrozenInstanceError):
            provisional_diagnostic_baseline_v1().revision = 2


if __name__ == "__main__":
    unittest.main()
