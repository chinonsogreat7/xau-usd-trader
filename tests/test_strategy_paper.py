import hashlib
import json
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, Inexact, ROUND_CEILING, localcontext

from xau_trader.data import generate_demo_m15_bars
from xau_trader.domain import AvailabilityBasis
from xau_trader.strategy_paper import run_strategy_paper_replay
from strategy_paper_fixture import (
    STRUCTURAL_POLICY, coherent_quotes, execution_policy, instrument, replay, rounded_bars,
)
from structural_exit_fixture import structural_exit_m15_bars


D = Decimal


class StrategyPaperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = rounded_bars()
        cls.tape = coherent_quotes(cls.bars)
        cls.replay = replay(cls.bars)

    def run_case(self, **changes):
        args = dict(quotes=self.tape, instrument=instrument(),
                    execution_policy=execution_policy(), structural_policy=STRUCTURAL_POLICY)
        source = changes.pop("source", self.replay)
        args.update(changes)
        return run_strategy_paper_replay(source, **args).as_dict()

    def test_genuine_strategy_drives_intent_and_rejects_unfavorable_nearest_target(self):
        report = self.run_case()
        self.assertEqual(report["counts"]["decisions"], 224)
        self.assertEqual(report["counts"]["pre_roll"], 96)
        self.assertEqual(report["counts"]["generated_intents"], 1)
        self.assertEqual(report["counts"]["rejected"], 1)
        self.assertEqual(report["counts"]["trades"], 0)
        self.assertEqual(report["decisions"][221]["execution_reason"], "minimum_reward_risk")
        intent = report["intents"][0]
        self.assertEqual(intent["side"], "buy")
        self.assertEqual(D(intent["stop_loss"]), D("2002.49"))
        self.assertEqual([D(level) for level in intent["target_levels"]],
                         [D("2001.11"), D("2005.45"), D("2009.60")])
        self.assertEqual(intent["created_at"], intent["not_before"])
        self.assertEqual(intent["source_fingerprint"], report["decisions"][221]["plan_fingerprint"])
        self.assertTrue(report["strategy_generated"])
        self.assertTrue(report["engine_connected"])
        self.assertFalse(report["broker_connected"])
        self.assertFalse(report["source_risk_policy_complete"])
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(report["real_orders_submitted"], 0)

    def test_each_candidate_is_accounted_for_including_pre_roll(self):
        report = self.run_case()
        counts = report["counts"]
        self.assertEqual(sum(counts[key] for key in
                             ("skipped", "rejected", "cancelled", "not_observed", "open", "closed")), 224)
        self.assertEqual(counts["skipped"], 223)
        self.assertTrue(all(row["execution_reason"] == "pre_roll" for row in report["decisions"][:96]))

    def test_relaxed_test_policy_can_exercise_fill_exit_and_target_provenance(self):
        # Deliberate policy perturbation tests wiring, not source rules or profit.
        report = self.run_case(execution_policy=execution_policy(min_reward_risk=D("0.1")))
        self.assertEqual(report["counts"]["trades"], 1)
        row = report["decisions"][221]
        self.assertEqual(row["execution_status"], "closed")
        self.assertEqual(D(row["selected_target"]["price"]), D("2005.45"))
        self.assertEqual(report["execution"]["trades"][0]["source_fingerprint"], row["plan_fingerprint"])
        self.assertNotEqual(report["fingerprint"], self.run_case()["fingerprint"])

    def test_reflected_sell_uses_same_verified_path(self):
        reflected = tuple(replace(bar,
            bid_open=4000-bar.ask_open, bid_high=4000-bar.ask_low,
            bid_low=4000-bar.ask_high, bid_close=4000-bar.ask_close,
            ask_open=4000-bar.bid_open, ask_high=4000-bar.bid_low,
            ask_low=4000-bar.bid_high, ask_close=4000-bar.bid_close,
        ) for bar in self.bars)
        bars = rounded_bars(reflected)
        report = self.run_case(source=replay(bars), quotes=coherent_quotes(bars))
        self.assertEqual(report["intents"][0]["side"], "sell")
        self.assertEqual(D(report["intents"][0]["stop_loss"]), D("1997.51"))
        self.assertEqual(report["counts"]["rejected"], 1)

    def test_missing_boundary_rejects_once_without_later_retry(self):
        tape = list(self.tape)
        tape[222*4] = replace(tape[222*4], timestamp=tape[222*4].timestamp + timedelta(seconds=1),
                              available_at=tape[222*4].timestamp + timedelta(seconds=1))
        report = self.run_case(quotes=tape)
        self.assertEqual(report["decisions"][221]["execution_reason"], "missing_exact_open_quote")
        self.assertEqual(report["execution"]["counts"]["rejected_intents"], 1)
        self.assertEqual(report["execution"]["counts"]["accepted_intents"], 0)

    def test_equal_time_receipt_rejects_even_with_later_duplicate(self):
        tape = list(self.tape)
        opening = tape[222*4]
        tape.insert(222*4, replace(opening, available_at=opening.timestamp))
        report = self.run_case(quotes=tape)
        self.assertEqual(report["decisions"][221]["execution_reason"], "ambiguous_entry_ordering")
        self.assertEqual(report["counts"]["rejected"], 1)

    def test_missing_bar_group_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing quote group"):
            self.run_case(quotes=self.tape[:8] + self.tape[12:])

    def test_each_bid_ask_ohlc_is_bound(self):
        for offset in range(4):
            for side in ("bid", "ask"):
                with self.subTest(offset=offset, side=side):
                    tape = list(self.tape)
                    quote = tape[offset]
                    tape[offset] = replace(quote, **{side: getattr(quote, side) + D("0.01")})
                    with self.assertRaisesRegex(ValueError, "does not reconstruct"):
                        self.run_case(quotes=tape)

    def test_quote_outside_bar_horizon_is_rejected(self):
        for tape in ((replace(self.tape[0], timestamp=self.bars[0].start_time-timedelta(seconds=1),
                             available_at=self.bars[0].start_time),) + self.tape,
                     self.tape + (replace(self.tape[-1], timestamp=self.bars[-1].timestamp,
                                          available_at=self.bars[-1].timestamp),)):
            with self.assertRaisesRegex(ValueError, "outside"):
                self.run_case(quotes=tape)

    def test_quote_cannot_arrive_after_completed_bar(self):
        tape = list(self.tape)
        tape[3] = replace(tape[3], available_at=self.bars[0].timestamp + timedelta(microseconds=1))
        with self.assertRaisesRegex(ValueError, "received after"):
            self.run_case(quotes=tape)

    def test_off_grid_quotes_and_bars_are_not_silently_rounded(self):
        tape = list(self.tape)
        tape[0] = replace(tape[0], bid=tape[0].bid+D("0.001"))
        with self.assertRaisesRegex(ValueError, "price grid"):
            self.run_case(quotes=tape)
        with self.assertRaisesRegex(ValueError, "price grid"):
            self.run_case(source=replay(structural_exit_m15_bars()))

    def test_non_synthetic_and_forged_replay_rejected(self):
        bars = tuple(replace(bar, availability_basis=AvailabilityBasis.HISTORICAL_CLOSE_ASSUMPTION)
                     for bar in self.bars)
        with self.assertRaisesRegex(ValueError, "synthetic"):
            self.run_case(source=replay(bars))
        forged = replace(self.replay)
        object.__setattr__(forged, "h1_atr", self.replay.h1_atr[:-1])
        with self.assertRaisesRegex(ValueError, "recomputation"):
            self.run_case(source=forged)

    def test_reordered_and_empty_tapes_rejected(self):
        with self.assertRaisesRegex(ValueError, "complete synthetic"):
            self.run_case(quotes=())
        with self.assertRaisesRegex(ValueError, "nondecreasing"):
            self.run_case(quotes=(self.tape[1], self.tape[0])+self.tape[2:])

    def test_suffix_does_not_rewrite_prior_plans_or_intents(self):
        extended = list(rounded_bars(generate_demo_m15_bars(480)[:232]))
        extended[:224] = self.bars
        after = self.run_case(source=replay(extended), quotes=coherent_quotes(extended))
        before = self.run_case()
        self.assertEqual(after["intents"][:1], before["intents"])
        self.assertEqual(after["structural"]["decisions"][:224], before["structural"]["decisions"])

    def test_full_report_fingerprint_and_quote_order_binding(self):
        report = self.run_case()
        digest = report.pop("fingerprint")
        self.assertEqual(digest, hashlib.sha256(json.dumps(
            report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest())
        tape = list(self.tape)
        tape[1] = replace(tape[1], timestamp=tape[1].timestamp+timedelta(seconds=1),
                          available_at=tape[1].available_at+timedelta(seconds=1))
        changed = self.run_case(quotes=tape)
        self.assertNotEqual(changed["quote_tape_fingerprint"], report["quote_tape_fingerprint"])
        self.assertNotEqual(changed["input_fingerprint"], report["input_fingerprint"])
        self.assertNotEqual(changed["fingerprint"], digest)

    def test_report_copy_and_decimal_context_are_isolated(self):
        original = self.run_case()
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_CEILING
            context.traps[Inexact] = True
            self.assertEqual(self.run_case()["fingerprint"], original["fingerprint"])
        result = run_strategy_paper_replay(self.replay, quotes=self.tape, instrument=instrument(),
                                          execution_policy=execution_policy(), structural_policy=STRUCTURAL_POLICY)
        modified = result.as_dict()
        modified["decisions"].clear()
        self.assertEqual(len(result.as_dict()["decisions"]), 224)


if __name__ == "__main__":
    unittest.main()
