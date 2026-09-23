"""Provisional loss controls: synthetic accounting, never broker evidence."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, ROUND_FLOOR, localcontext
import hashlib
import json
import unittest

from xau_trader.paper_execution import (
    BracketIntent, LOSS_LIMITS_VERSION, PaperInstrument, PaperLossLimits,
    PaperPolicy, PaperQuote, run_paper_execution,
)


D = Decimal
START = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


def instrument(**changes):
    return replace(PaperInstrument("XAU_USD", D("0.01"), D("1"), D("0.01"),
                                   D("1"), D("0.01"), D("0")), **changes)


def policy(**changes):
    return replace(PaperPolicy(
        initial_balance=D("1000"), risk_fraction=D("0.1"),
        max_daily_loss_fraction=D("0.8"), max_drawdown_fraction=D("0.8"),
        max_spread=D("10"), slippage=D("0"), commission_per_lot_per_side=D("0"),
        financing_per_lot_per_utc_day=D("0"), max_quote_age_ms=1000,
        max_trades_per_day=100, max_holding_seconds=10**9,
        force_flat_at_end=False, min_reward_risk=D("0.1"), halt_at=None,
    ), **changes)


def limits(**changes):
    return replace(PaperLossLimits(2, D("0.5")), **changes)


def quote(seconds, price="100", *, ask=None, delay_ms=0, start=START):
    source = start + timedelta(seconds=seconds)
    return PaperQuote(source, source + timedelta(milliseconds=delay_ms),
                      D(price), D(price if ask is None else ask))


def intent(name="a", created=0, *, stop="90", target="120", start=START):
    time = start + timedelta(seconds=created)
    return BracketIntent(name, "buy", time, time,
                         time + timedelta(days=30), D(stop), D(target), "b" * 64)


def run(tape, requests=(), *, guard=None, config=None, spec=None):
    return run_paper_execution(tape, requests, spec or instrument(), config or policy(),
                               loss_limits=limits() if guard is None else guard).as_dict()


def reasons(result):
    return [event["reason"] for event in result["events"]]


class PaperLossLimitsTests(unittest.TestCase):
    def test_two_total_losses_halt_even_with_a_winner_between_them(self):
        result = run([quote(1), quote(2, "90"), quote(3), quote(4, "120"),
                      quote(5), quote(6, "90"), quote(7)],
                     [intent(), intent("winner", 2), intent("second_loss", 4),
                      intent("blocked", 6)])
        self.assertEqual([D(t["net_pnl"]) for t in result["trades"]],
                         [D("-10"), D("20"), D("-10")])
        self.assertEqual(result["losses_today"], 2)
        self.assertTrue(result["loss_count_halt"])
        self.assertFalse(result["daily_halt"])
        self.assertIn("loss_count_halt_active", reasons(result))
        self.assertEqual(result["counts"]["accepted_intents"], 3)
        self.assertEqual(reasons(result).count("maximum_losses_per_day"), 1)

    def test_breakeven_does_not_count_or_reset_a_previous_loss(self):
        result = run([quote(1), quote(2, "90"), quote(3), quote(4),
                      quote(5), quote(6, "90")],
                     [intent(), intent("breakeven", 2), intent("second_loss", 4)],
                     config=policy(max_holding_seconds=1))
        self.assertEqual([t["counted_daily_loss"] for t in result["trades"]],
                         [True, False, True])
        self.assertEqual(result["equity_curve"][3]["losses_today"], 1)
        self.assertEqual(result["losses_today"], 2)
        self.assertTrue(result["loss_count_halt"])

    def test_commissions_turn_gross_positive_close_into_counted_loss(self):
        result = run([quote(1), quote(2, "100.50")], [intent()],
                     config=policy(commission_per_lot_per_side=D("1"), max_holding_seconds=1))
        trade = result["trades"][0]
        self.assertEqual(D(trade["gross_pnl"]), D("0.5"))
        self.assertEqual(D(trade["net_pnl"]), D("-1.5"))
        self.assertTrue(trade["counted_daily_loss"])
        self.assertEqual(result["losses_today"], 1)

    def test_loss_is_assigned_to_exit_receipt_date_not_entry_or_source_date(self):
        start = datetime(2026, 1, 5, 23, 59, 58, tzinfo=timezone.utc)
        result = run([quote(0.1, start=start), quote(1.9, "90", delay_ms=200, start=start)],
                     [intent(created=-1, start=start)])
        self.assertEqual(result["trades"][0]["loss_receipt_day"], "2026-01-06")
        self.assertEqual(result["losses_today"], 1)
        self.assertEqual(result["equity_curve"][0]["losses_today"], 0)

    def test_financing_is_included_and_loss_count_resets_next_utc_day(self):
        start = datetime(2026, 1, 5, 23, 59, 58, tzinfo=timezone.utc)
        result = run([quote(1, start=start), quote(2, "101", start=start),
                      quote(86403, start=start)],
                     [intent(start=start)],
                     config=policy(financing_per_lot_per_utc_day=D("2"), max_holding_seconds=1))
        trade = result["trades"][0]
        self.assertEqual(D(trade["gross_pnl"]), D("1"))
        self.assertEqual(D(trade["financing"]), D("2"))
        self.assertEqual(D(trade["net_pnl"]), D("-1"))
        self.assertEqual(result["equity_curve"][1]["losses_today"], 1)
        self.assertEqual(result["losses_today"], 0)

    def test_exact_breakeven_after_commissions_and_financing_does_not_count(self):
        start = datetime(2026, 1, 5, 23, 59, 58, tzinfo=timezone.utc)
        result = run([quote(1, start=start), quote(2, "103", start=start)],
                     [intent(start=start)], config=policy(
                         commission_per_lot_per_side=D("1"),
                         financing_per_lot_per_utc_day=D("1"), max_holding_seconds=1))
        self.assertEqual(D(result["trades"][0]["net_pnl"]), D("0"))
        self.assertFalse(result["trades"][0]["counted_daily_loss"])
        self.assertEqual(result["losses_today"], 0)

    def test_daily_loss_count_halt_resets_next_day_and_allows_new_entry(self):
        result = run([quote(1), quote(2, "90"), quote(3), quote(4, "90"),
                      quote(5), quote(86401)],
                     [intent(), intent("second", 2), intent("blocked", 4),
                      intent("tomorrow", 86400)])
        self.assertIn("loss_count_halt_active", reasons(result))
        self.assertEqual(result["counts"]["accepted_intents"], 3)
        self.assertEqual(result["losses_today"], 0)
        self.assertFalse(result["loss_count_halt"])

    def test_end_of_data_loss_latches_count_and_updates_final_curve(self):
        result = run([quote(1, ask="100.20")], [intent()],
                     guard=limits(max_losses_per_day=1),
                     config=policy(force_flat_at_end=True))
        self.assertEqual(result["trades"][0]["exit_reason"], "end_of_data")
        self.assertEqual(result["losses_today"], 1)
        self.assertTrue(result["loss_count_halt"])
        self.assertTrue(result["equity_curve"][-1]["loss_count_halt"])
        self.assertEqual(result["equity_curve"][-1]["losses_today"], 1)
        self.assertEqual(result["counts"]["events"], len(result["events"]))

    def test_weekly_high_water_includes_unrealized_profit(self):
        result = run([quote(1), quote(2, "120"), quote(3), quote(4)],
                     [intent(target="200"), intent("blocked", 3)],
                     guard=limits(max_weekly_drawdown_fraction=D("0.01")))
        self.assertEqual(D(result["week_peak"]), D("1020"))
        self.assertTrue(result["weekly_halt"])
        self.assertEqual(result["trades"][0]["exit_reason"], "maximum_weekly_drawdown")
        self.assertEqual(D(result["trades"][0]["net_pnl"]), D("0"))
        self.assertIn("weekly_halt_active", reasons(result))
        self.assertFalse(result["loss_count_halt"])

    def test_weekly_mark_includes_both_commissions_and_adverse_slippage(self):
        result = run([quote(1), quote(2, "120")], [intent(target="200")],
                     config=policy(slippage=D("0.1"), commission_per_lot_per_side=D("1")))
        self.assertEqual(D(result["equity_curve"][0]["equity"]), D("997.8"))
        self.assertEqual(D(result["week_peak"]), D("1017.8"))
        self.assertEqual(result["equity_curve"][0]["week_peak"], "1000")

    def test_standing_target_caps_week_high_water(self):
        result = run([quote(1), quote(2, "200")], [intent()],
                     config=policy(commission_per_lot_per_side=D("1")))
        self.assertEqual(D(result["week_peak"]), D("1018"))
        self.assertEqual(result["trades"][0]["exit_reason"], "take_profit")

    def test_weekly_latch_survives_daily_reset_then_clears_next_week(self):
        result = run([quote(1), quote(2, "80"), quote(86401), quote(604801)],
                     [intent(), intent("tomorrow", 86400), intent("next_week", 604800)],
                     guard=limits(max_weekly_drawdown_fraction=D("0.01")))
        self.assertTrue(result["equity_curve"][2]["weekly_halt"])
        self.assertFalse(result["equity_curve"][3]["weekly_halt"])
        self.assertEqual(result["counts"]["accepted_intents"], 2)
        self.assertIn("weekly_halt_active", reasons(result))
        self.assertEqual(result["week_id"], "2026-W03")
        self.assertEqual(D(result["week_peak"]), D("980"))

    def test_weekly_threshold_is_inclusive_and_gap_can_exceed_sizing_cap(self):
        result = run([quote(1), quote(2, "90")], [intent()],
                     guard=limits(max_weekly_drawdown_fraction=D("0.01")))
        self.assertTrue(result["weekly_halt"])
        self.assertEqual(result["trades"][0]["exit_reason"], "maximum_weekly_drawdown")
        result = run([quote(1), quote(2, "80")], [intent()],
                     guard=limits(max_weekly_drawdown_fraction=D("0.01")))
        self.assertGreater(-D(result["trades"][0]["net_pnl"]),
                           D(result["trades"][0]["modeled_stop_risk"]))
        self.assertEqual(D(result["maximum_weekly_drawdown_fraction"]), D("0.02"))

    def test_weekly_halt_from_stale_quote_never_fills_and_survives_recovery(self):
        result = run([quote(1), quote(2, "80", delay_ms=2000), quote(5, "110")],
                     [intent()], guard=limits(max_weekly_drawdown_fraction=D("0.01")))
        self.assertTrue(result["equity_curve"][1]["weekly_halt"])
        self.assertTrue(result["weekly_halt"])
        self.assertEqual(len(result["fills"]), 2)
        self.assertEqual(result["fills"][1]["timestamp"], "2026-01-05T12:00:05Z")
        self.assertEqual(result["trades"][0]["exit_reason"], "stale_quote_with_open_position")
        self.assertEqual(D(result["final_equity"]), D("1010"))
        self.assertEqual(D(result["week_peak"]), D("1010"))

    def test_final_stale_quote_leaves_halted_position_open(self):
        result = run([quote(1), quote(2, "80", delay_ms=2000)], [intent()],
                     guard=limits(max_weekly_drawdown_fraction=D("0.01")),
                     config=policy(force_flat_at_end=True))
        self.assertTrue(result["weekly_halt"])
        self.assertIsNotNone(result["open_position"])
        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual(result["losses_today"], 0)
        self.assertIn("last_quote_stale", reasons(result))

    def test_weekly_reset_preserves_weekend_gap_from_previous_observed_equity(self):
        start = datetime(2026, 1, 11, 23, 59, 58, tzinfo=timezone.utc)
        result = run([quote(1, start=start), quote(2, "80", start=start)],
                     [intent(start=start)], guard=limits(max_weekly_drawdown_fraction=D("0.01")))
        self.assertEqual(result["week_id"], "2026-W03")
        self.assertEqual(D(result["week_peak"]), D("1000"))
        self.assertTrue(result["weekly_halt"])
        self.assertFalse(result["daily_halt"])
        self.assertEqual(result["trades"][0]["exit_reason"], "maximum_weekly_drawdown")

    def test_weekly_reset_preserves_cross_boundary_financing(self):
        start = datetime(2026, 1, 11, 23, 59, 58, tzinfo=timezone.utc)
        result = run([quote(1, start=start), quote(2, start=start)],
                     [intent(start=start)], guard=limits(max_weekly_drawdown_fraction=D("0.01")),
                     config=policy(financing_per_lot_per_utc_day=D("20")))
        self.assertTrue(result["weekly_halt"])
        self.assertEqual(D(result["week_peak"]), D("1000"))
        self.assertEqual(D(result["total_financing"]), D("20"))
        self.assertEqual(result["trades"][0]["exit_reason"], "maximum_weekly_drawdown")
        self.assertEqual(result["losses_today"], 1)

    def test_weekly_reset_uses_prior_equity_not_prior_weeks_peak(self):
        result = run([quote(1), quote(2, "110"), quote(3), quote(604801), quote(1209601)],
                     [intent()], config=policy(max_holding_seconds=2))
        self.assertEqual(D(result["equity_curve"][2]["week_peak"]), D("1010"))
        self.assertEqual(D(result["equity_curve"][3]["week_peak"]), D("1000"))
        self.assertEqual(D(result["equity_curve"][4]["week_peak"]), D("1000"))
        self.assertEqual(len([e for e in result["events"] if e["kind"] == "week_started"]), 3)

    def test_iso_year_week_does_not_reset_on_january_first(self):
        start = datetime(2026, 12, 31, 12, tzinfo=timezone.utc)
        result = run([quote(1, start=start), quote(2, "80", start=start),
                      quote(86401, start=start), quote(4 * 86400 + 1, start=start)],
                     [intent(start=start)], guard=limits(max_weekly_drawdown_fraction=D("0.01")))
        self.assertEqual([point["week_id"] for point in result["equity_curve"]],
                         ["2026-W53", "2026-W53", "2026-W53", "2027-W01"])
        self.assertTrue(result["equity_curve"][2]["weekly_halt"])
        self.assertFalse(result["weekly_halt"])
        self.assertEqual(len([e for e in result["events"] if e["kind"] == "week_started"]), 2)

    def test_weekly_sizing_caps_remaining_allowance_and_rounds_down(self):
        result = run([quote(1), quote(2, "99"), quote(3)],
                     [intent(stop="99"), intent("next", 2)],
                     guard=limits(max_weekly_drawdown_fraction=D("0.03")),
                     spec=instrument(max_lots=D("10")))
        self.assertEqual(D(result["fills"][0]["lots"]), D("10"))
        self.assertEqual(D(result["fills"][2]["lots"]), D("2"))
        self.assertEqual(D(result["open_position"]["initial_risk_budget"]), D("20"))
        self.assertEqual(D(result["open_position"]["modeled_stop_risk"]), D("20"))

    def test_weekly_sizing_does_not_round_up_to_minimum(self):
        result = run([quote(1)], [intent()],
                     guard=limits(max_weekly_drawdown_fraction=D("0.001")),
                     spec=instrument(min_lots=D("1")))
        self.assertEqual(result["fills"], [])
        self.assertIn("below_minimum_lot", reasons(result))

    def test_weekly_sizing_includes_both_commissions(self):
        result = run([quote(1)], [intent()],
                     guard=limits(max_weekly_drawdown_fraction=D("0.01")),
                     config=policy(commission_per_lot_per_side=D("1")))
        self.assertEqual(D(result["open_position"]["lots"]), D("0.83"))
        self.assertEqual(D(result["open_position"]["modeled_stop_risk"]), D("9.96"))
        self.assertEqual(D(result["open_position"]["initial_risk_budget"]), D("10"))

    def test_short_loss_uses_same_weekly_and_daily_count_guards(self):
        short = replace(intent(), side="sell", stop_loss=D("110"), take_profit=D("80"))
        result = run([quote(1), quote(2, "120")], [short],
                     guard=limits(max_losses_per_day=1, max_weekly_drawdown_fraction=D("0.01")))
        self.assertEqual(result["trades"][0]["exit_reason"], "maximum_weekly_drawdown")
        self.assertEqual(D(result["trades"][0]["net_pnl"]), D("-20"))
        self.assertTrue(result["weekly_halt"])
        self.assertTrue(result["loss_count_halt"])

    def test_priority_global_then_daily_then_weekly(self):
        guard = limits(max_losses_per_day=1, max_weekly_drawdown_fraction=D("0.01"))
        for config, expected in ((policy(halt_at=START + timedelta(seconds=2)), "operator_halt"),
                                 (policy(max_drawdown_fraction=D("0.01")), "maximum_drawdown"),
                                 (policy(max_daily_loss_fraction=D("0.01")), "maximum_daily_loss")):
            with self.subTest(expected=expected):
                result = run([quote(1), quote(2, "80")], [intent()], guard=guard, config=config)
                self.assertEqual(result["trades"][0]["exit_reason"], expected)
                self.assertTrue(result["weekly_halt"])
                self.assertTrue(result["loss_count_halt"])

    def test_nonpositive_equity_then_negative_new_week_baseline_is_safe(self):
        result = run([quote(1), quote(2, "0.01"), quote(604801, "0.01")],
                     [intent(stop="99")], guard=limits(max_weekly_drawdown_fraction=D("0.03")),
                     spec=instrument(max_lots=D("100")), config=policy(risk_fraction=D("0.9")))
        self.assertEqual(result["global_halt"], "non_positive_equity")
        self.assertLess(D(result["week_peak"]), D("0"))
        self.assertLess(D(result["final_equity"]), D("0"))

    def test_none_disables_new_guards_visibly_and_keeps_legacy_behavior(self):
        tape = [quote(1), quote(2, "90"), quote(3), quote(4, "90"), quote(5)]
        requests = [intent(), intent("second", 2), intent("third", 4)]
        disabled = run_paper_execution(tape, requests, instrument(), policy()).as_dict()
        explicit = run_paper_execution(tape, requests, instrument(), policy(), loss_limits=None).as_dict()
        self.assertEqual(disabled, explicit)
        self.assertFalse(disabled["loss_limits"]["enabled"])
        self.assertEqual(disabled["counts"]["accepted_intents"], 3)
        self.assertNotIn("weekly_halt", disabled)
        self.assertNotIn("losses_today", disabled["events"][0])

    def test_limit_semantics_parameters_and_input_hash_are_bound(self):
        first = run([quote(1)], [intent()])
        changed_count = run([quote(1)], [intent()], guard=limits(max_losses_per_day=3))
        changed_week = run([quote(1)], [intent()], guard=limits(max_weekly_drawdown_fraction=D("0.4")))
        for changed in (changed_count, changed_week):
            self.assertNotEqual(first["input_fingerprint"], changed["input_fingerprint"])
            self.assertNotEqual(first["fingerprint"], changed["fingerprint"])
        document = first["loss_limits"].copy()
        digest = document.pop("fingerprint")
        expected = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                             allow_nan=False).encode("utf-8")).hexdigest()
        self.assertEqual(digest, expected)
        self.assertEqual(first["loss_limits"]["version"], LOSS_LIMITS_VERSION)
        self.assertTrue(first["loss_limits"]["provisional"])
        self.assertFalse(first["loss_limits"]["state_persisted"])
        disabled = run_paper_execution([quote(1)], [intent()], instrument(), policy()).as_dict()
        self.assertNotEqual(first["input_fingerprint"], disabled["input_fingerprint"])
        self.assertEqual(limits().fingerprint, digest)
        for record in first["events"] + first["equity_curve"]:
            self.assertEqual(record["loss_limits_fingerprint"], digest)
            self.assertIn("losses_today", record)
            self.assertIn("weekly_halt", record)
            self.assertIn("week_peak", record)

    def test_configuration_is_frozen_and_serialized_copies_are_independent(self):
        config = limits()
        with self.assertRaises(FrozenInstanceError):
            config.max_losses_per_day = 7
        first = config.as_dict()
        first["semantics"]["loss"] = "tampered"
        self.assertNotEqual(config.as_dict()["semantics"]["loss"], "tampered")

    def test_explicit_required_fields_and_strict_validation(self):
        with self.assertRaises(TypeError):
            PaperLossLimits()
        for value in (True, 0, -1, 2.0, 10**13):
            with self.subTest(count=value), self.assertRaises(ValueError):
                PaperLossLimits(value, D("0.03"))
        for value in (D("0"), D("1"), D("-0.01"), D("NaN"), D("Infinity"), 0.03,
                      D("1e-100")):
            with self.subTest(fraction=value), self.assertRaises(ValueError):
                PaperLossLimits(2, value)
        with self.assertRaises(ValueError):
            run_paper_execution([quote(1)], [], instrument(), policy(), loss_limits={})

    def test_guard_arithmetic_is_independent_of_callers_decimal_context(self):
        expected = run([quote(1), quote(2, "80")], [intent()],
                       guard=limits(max_weekly_drawdown_fraction=D("0.03")))
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_FLOOR
            context.traps[Inexact] = True
            actual = run([quote(1), quote(2, "80")], [intent()],
                         guard=limits(max_weekly_drawdown_fraction=D("0.03")))
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
