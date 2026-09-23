import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal, Inexact, ROUND_CEILING, ROUND_FLOOR, localcontext

from xau_trader.candidate_signals import CandidateAction
from xau_trader.data import generate_demo_m15_bars
from xau_trader.diagnostic_replay import run_diagnostic_replay
from xau_trader.domain import AvailabilityBasis
from xau_trader.multitimeframe import PivotKind
from xau_trader.research_baseline import (
    provisional_diagnostic_baseline_v1, provisional_session_diagnostic_baseline_v1,
)
from xau_trader.structural_exits import (
    StructuralExitPolicy, _plan_candidate, confirmed_m15_swings,
    plan_structural_exits, select_entry_target,
)

from session_feature_fixture import session_fixture
from structural_exit_fixture import structural_exit_m15_bars


D = Decimal
POLICY = StructuralExitPolicy(3, 3, D("0.15"))


def replay(bars, bundle=None):
    return run_diagnostic_replay(bars, dataset_fingerprint="a" * 64,
                                 policy_bundle=bundle or provisional_diagnostic_baseline_v1())


def reflected(bars):
    return tuple(replace(
        bar, bid_open=4000 - bar.ask_open, bid_high=4000 - bar.ask_low,
        bid_low=4000 - bar.ask_high, bid_close=4000 - bar.ask_close,
        ask_open=4000 - bar.bid_open, ask_high=4000 - bar.bid_low,
        ask_low=4000 - bar.bid_high, ask_close=4000 - bar.bid_close,
    ) for bar in bars)


class StructuralExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = structural_exit_m15_bars()
        cls.replay = replay(cls.bars)
        cls.result = plan_structural_exits(cls.replay, policy=POLICY, price_tick=D("0.01"))
        cls.candidate = next(item for item in cls.replay.decisions if item.action == CandidateAction.BUY)
        cls.plan = next(item.plan for item in cls.result.decisions if item.plan is not None)

    def helper(self, **changes):
        args = dict(decision=self.candidate, swings=self.result.m15_swings,
                    atr_events=self.replay.h1_atr, h1_pivots=self.replay.h1_pivots,
                    policy=POLICY, price_tick=D("0.01"))
        args.update(changes)
        return _plan_candidate(**args)

    def test_public_planner_derives_genuine_buy_from_validated_replay(self):
        report = self.result.as_dict()
        self.assertEqual(report["counts"]["candidates"], 224)
        self.assertEqual(report["counts"]["pre_roll"], 96)
        self.assertEqual(report["counts"]["bracket_plans"], 1)
        self.assertEqual(self.plan.stop_loss, D("2002.49"))
        self.assertEqual(self.plan.atr.bar_end.hour, 7)
        self.assertEqual(self.plan.atr.bar_end.minute, 0)
        self.assertEqual(self.plan.proposed_entry_at, self.candidate.proposed_entry_at)
        self.assertEqual(self.plan.decision_available_at, self.candidate.available_at)
        self.assertTrue(report["hypothesis_unapproved"])
        self.assertFalse(report["engine_connected"])
        self.assertFalse(report["broker_connected"])
        self.assertNotIn("net_pnl", report)
        self.assertNotIn("entry_price", report["decisions"][221]["plan"])

    def test_all_candidates_have_rows_and_pre_roll_never_actionable(self):
        self.assertEqual(tuple(row.candidate_key for row in self.result.decisions),
                         tuple(item.key for item in self.replay.decisions))
        self.assertTrue(all(row.reason == "pre_roll" and row.plan is None
                            for row in self.result.decisions[:96]))
        self.assertIn("candidate_no_trade", [row.reason for row in self.result.decisions])

    def test_ladder_preserves_old_levels_and_nearest_has_no_rr_fallback(self):
        self.assertEqual(tuple(level.price for level in self.plan.target_levels),
                         (D("2001.11"), D("2005.45"), D("2009.60")))
        self.assertEqual(select_entry_target(self.plan, D("2004.45")).price, D("2005.45"))
        # The nearer level is retained even though this entry-to-target reward
        # is tiny relative to the stop; no search for a more flattering 2R.
        self.assertEqual(select_entry_target(self.plan, D("2005.44")).price, D("2005.45"))
        self.assertEqual(select_entry_target(self.plan, D("2005.45")).price, D("2009.60"))
        self.assertIsNone(select_entry_target(self.plan, D("2009.60")))

    def test_reflected_fixture_derives_genuine_sell_with_outward_rounding(self):
        source = replay(reflected(self.bars))
        result = plan_structural_exits(source, policy=POLICY, price_tick=D("0.01"))
        plan = next(row.plan for row in result.decisions if row.plan is not None)
        self.assertEqual(plan.action, CandidateAction.SELL)
        self.assertEqual(plan.stop_loss, D("1997.51"))
        self.assertEqual(tuple(level.price for level in plan.target_levels),
                         (D("1998.89"), D("1994.55"), D("1990.40")))
        self.assertEqual(select_entry_target(plan, D("1995.55")).price, D("1994.55"))
        self.assertIsNone(select_entry_target(plan, D("1990.40")))

    def test_latest_swing_by_center_not_lowest_ever_or_latest_receipt(self):
        original = self.plan.swing
        older = replace(original, pivot_bar_start=original.pivot_bar_start - timedelta(minutes=15),
                        pivot_bar_end=original.pivot_bar_end - timedelta(minutes=15),
                        mid_price=D("1900"), available_at=self.candidate.available_at)
        row = self.helper(swings=(older, original))
        self.assertEqual(row.plan.swing, original)
        self.assertEqual(row.plan.stop_loss, self.plan.stop_loss)

    def test_missing_or_delayed_m15_swing_skips(self):
        self.assertEqual(self.helper(swings=()).reason, "no_visible_m15_swing")
        delayed = replace(self.plan.swing, available_at=self.candidate.available_at + timedelta(microseconds=1))
        self.assertEqual(self.helper(swings=(delayed,)).reason, "no_visible_m15_swing")
        unconfirmed = replace(self.plan.swing, confirmed_at=self.candidate.available_at + timedelta(minutes=15))
        self.assertEqual(self.helper(swings=(unconfirmed,)).reason, "no_visible_m15_swing")

    def test_missing_delayed_or_future_atr_skips(self):
        self.assertEqual(self.helper(atr_events=()).reason, "no_visible_h1_atr")
        delayed = replace(self.plan.atr, available_at=self.candidate.available_at + timedelta(microseconds=1))
        self.assertEqual(self.helper(atr_events=(delayed,)).reason, "no_visible_h1_atr")
        future = replace(self.plan.atr, bar_end=self.candidate.m15_bar_end + timedelta(hours=1))
        self.assertEqual(self.helper(atr_events=(future,)).reason, "no_visible_h1_atr")

    def test_latest_atr_snapshot_not_old_impulse_snapshot(self):
        latest = self.plan.atr
        older = replace(latest, bar_start=latest.bar_start - timedelta(hours=1),
                        bar_end=latest.bar_end - timedelta(hours=1), value=100.0)
        row = self.helper(atr_events=(latest, older))
        self.assertEqual(row.plan.atr, latest)
        self.assertEqual(row.plan.atr_buffer, D(str(latest.value)) * D("0.15"))

    def test_no_visible_target_and_late_target_fail_closed(self):
        self.assertEqual(self.helper(h1_pivots=()).reason, "no_visible_target")
        pivot = self.plan.target_levels[0].pivot
        for late in (replace(pivot, available_at=self.candidate.available_at + timedelta(microseconds=1)),
                     replace(pivot, confirmed_at=self.candidate.available_at + timedelta(hours=1))):
            self.assertEqual(self.helper(h1_pivots=(late,)).reason, "no_visible_target")

    def test_future_and_delayed_suffix_cannot_change_snapshot_plan(self):
        future_swing = replace(self.plan.swing, pivot_bar_start=self.plan.swing.pivot_bar_start + timedelta(days=1),
                               confirmed_at=self.candidate.available_at + timedelta(days=1),
                               available_at=self.candidate.available_at + timedelta(days=1), mid_price=D("100"))
        pivot = self.plan.target_levels[0].pivot
        future_target = replace(pivot, available_at=self.candidate.available_at + timedelta(days=1), mid_price=2004.5)
        row = self.helper(swings=self.result.m15_swings + (future_swing,),
                          h1_pivots=self.replay.h1_pivots + (future_target,))
        self.assertEqual(row.plan, self.plan)

    def test_full_replay_future_suffix_preserves_prior_decision_plans(self):
        extended = list(generate_demo_m15_bars(480)[:232])
        extended[:224] = self.bars
        result = plan_structural_exits(replay(tuple(extended)), policy=POLICY, price_tick=D("0.01"))
        self.assertEqual(result.decisions[:224], self.result.decisions)
        self.assertNotEqual(result.fingerprint, self.result.fingerprint)

    def test_multiple_selected_zones_fail_closed(self):
        # Helper-level adversarial evidence, not an altered trusted replay.
        decision = replace(self.candidate, selected_retests=self.candidate.selected_retests * 2)
        self.assertEqual(self.helper(decision=decision).reason, "ambiguous_selected_zone")

    def test_equal_price_target_tie_uses_newest_center_and_keeps_both(self):
        original = self.plan.target_levels[1].pivot
        older = replace(original, pivot_bar_start=original.pivot_bar_start - timedelta(hours=1),
                        pivot_bar_end=original.pivot_bar_end - timedelta(hours=1))
        row = self.helper(h1_pivots=(older, original))
        self.assertEqual(len(row.plan.target_levels), 2)
        self.assertEqual(select_entry_target(row.plan, D("2004")).pivot, original)

    def test_coarse_tick_rounds_stops_outward_and_targets_toward_entry(self):
        plan = self.helper(price_tick=D("0.50")).plan
        self.assertEqual(plan.stop_loss, D("2002.00"))
        self.assertEqual(tuple(level.price for level in plan.target_levels),
                         (D("2001.00"), D("2005.00"), D("2009.50")))

    def test_pivots_are_strict_three_wing_with_full_window_availability(self):
        source = list(self.bars[:7])
        source = [replace(bar, bid_high=2010.0, ask_high=2010.2,
                          bid_low=1990.0, ask_low=1990.2) for bar in source]
        source[3] = replace(source[3], bid_high=2011.0, ask_high=2011.2,
                            bid_low=1989.0, ask_low=1989.2)
        source[0] = replace(source[0], available_at=source[-1].timestamp + timedelta(hours=1))
        swings = confirmed_m15_swings(source, policy=POLICY)
        self.assertEqual({swing.kind for swing in swings}, {PivotKind.HIGH, PivotKind.LOW})
        self.assertTrue(all(swing.confirmed_at == source[-1].timestamp for swing in swings))
        self.assertTrue(all(swing.available_at == source[0].available_at for swing in swings))
        self.assertEqual(confirmed_m15_swings(source[:-1], policy=POLICY), ())
        source[1] = replace(source[1], bid_high=2011.0, ask_high=2011.2)
        self.assertEqual(tuple(swing.kind for swing in confirmed_m15_swings(source, policy=POLICY)),
                         (PivotKind.LOW,))

    def test_calendar_pivot_wings_count_actual_bars_and_keep_real_close(self):
        bars, calendar = session_fixture()
        source = list(bars[89:96])
        source = [replace(bar, bid_high=2100.0, ask_high=2100.2,
                          bid_low=1900.0, ask_low=1900.2) for bar in source]
        source[3] = replace(source[3], bid_high=2101.0, ask_high=2101.2)
        swings = confirmed_m15_swings(source, policy=POLICY, calendar=calendar)
        self.assertEqual(len(swings), 1)
        self.assertEqual(swings[0].confirmed_at, source[-1].timestamp)
        self.assertEqual(swings[0].calendar_fingerprint, calendar.fingerprint)
        with self.assertRaisesRegex(ValueError, "contiguous"):
            confirmed_m15_swings(source, policy=POLICY)

    def test_public_session_replay_remains_supported(self):
        bars, calendar = session_fixture()
        source = replay(bars, provisional_session_diagnostic_baseline_v1(calendar))
        result = plan_structural_exits(source, policy=POLICY, price_tick=D("0.01"))
        self.assertEqual(len(result.decisions), len(bars))
        self.assertTrue(all(swing.calendar_fingerprint == calendar.fingerprint for swing in result.m15_swings))

    def test_public_planner_rejects_non_synthetic_replay(self):
        bars = tuple(replace(bar, availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION)
                     for bar in self.bars[:100])
        with self.assertRaisesRegex(ValueError, "synthetic"):
            plan_structural_exits(replay(bars), policy=POLICY, price_tick=D("0.01"))

    def test_public_planner_recomputes_replay_instead_of_trusting_frozen_fields(self):
        # Bypass frozen protection solely to simulate a malicious deserializer.
        corrupted = replace(self.replay)
        object.__setattr__(corrupted, "h1_atr", self.replay.h1_atr[:-1])
        with self.assertRaisesRegex(ValueError, "recomputation"):
            plan_structural_exits(corrupted, policy=POLICY, price_tick=D("0.01"))

    def test_policy_is_explicit_and_result_immutable(self):
        with self.assertRaises(TypeError):
            StructuralExitPolicy()
        for args in ((True, 3, D("0.15")), (0, 3, D("0.15")), (3, 3, 0.15), (3, 3, D("NaN"))):
            with self.assertRaises(ValueError):
                StructuralExitPolicy(*args)
        with self.assertRaises(FrozenInstanceError):
            self.plan.stop_loss = D("1")
        with self.assertRaises(ValueError):
            select_entry_target(self.plan, 2004.0)
        self.assertNotEqual(POLICY.fingerprint, StructuralExitPolicy(2, 3, D("0.15")).fingerprint)

    def test_result_fingerprint_binds_full_policy_and_evidence(self):
        payload = self.result.as_dict()
        digest = payload.pop("fingerprint")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(digest, hashlib.sha256(encoded).hexdigest())
        self.assertNotEqual(self.result.fingerprint, replace(self.result, price_tick=D("0.10")).fingerprint)
        self.assertEqual(payload["policy_fingerprint"], POLICY.fingerprint)

    def test_decimal_context_cannot_change_plan_or_fingerprint(self):
        for rounding in (ROUND_FLOOR, ROUND_CEILING):
            with localcontext() as context:
                context.prec = 6
                context.rounding = rounding
                context.traps[Inexact] = True
                self.assertEqual(self.helper().plan, self.plan)
                self.assertEqual(self.result.fingerprint, self.result.as_dict()["fingerprint"])


if __name__ == "__main__":
    unittest.main()
