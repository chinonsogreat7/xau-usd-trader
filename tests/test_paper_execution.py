import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, ROUND_CEILING, ROUND_FLOOR, localcontext

from xau_trader.paper_execution import (
    BracketIntent, ExactOpenIntent, PaperInstrument, PaperPolicy, PaperQuote, run_paper_execution,
)


D = Decimal
START = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)


def instrument(**changes):
    return replace(PaperInstrument("XAU_USD", D("0.01"), D("1"), D("0.01"),
                                   D("10"), D("0.01"), D("0")), **changes)


def policy(**changes):
    return replace(PaperPolicy(
        initial_balance=D("1000"), risk_fraction=D("0.01"),
        max_daily_loss_fraction=D("0.10"), max_drawdown_fraction=D("0.20"),
        max_spread=D("1"), slippage=D("0"), commission_per_lot_per_side=D("0"),
        financing_per_lot_per_utc_day=D("0"), max_quote_age_ms=1000,
        max_trades_per_day=5, max_holding_seconds=3600, force_flat_at_end=True,
        min_reward_risk=D("0.1"), halt_at=None,
    ), **changes)


def quote(seconds, bid="100", ask="100.20", delay_ms=0):
    timestamp = START + timedelta(seconds=seconds)
    return PaperQuote(timestamp, timestamp + timedelta(milliseconds=delay_ms), D(bid), D(ask))


def intent(intent_id="a", side="buy", created=0, not_before=None, expires=3600,
           stop=None, target=None):
    return BracketIntent(intent_id, side, START + timedelta(seconds=created),
                         START + timedelta(seconds=created if not_before is None else not_before),
                         START + timedelta(seconds=expires),
                         D(stop or ("95" if side == "buy" else "105")),
                         D(target or ("110" if side == "buy" else "90")), "a" * 64)


def run(tape, requests=None, spec=None, config=None):
    return run_paper_execution(tape, [intent()] if requests is None else requests,
                               spec or instrument(), config or policy()).as_dict()


def reasons(result):
    return [event["reason"] for event in result["events"]]


def exact_intent(intent_id="exact", side="buy", created=0, boundary=1,
                 expires=3600, stop=None, levels=None):
    return ExactOpenIntent(
        intent_id, side, START + timedelta(seconds=created),
        START + timedelta(seconds=boundary), START + timedelta(seconds=expires),
        D(stop or ("95" if side == "buy" else "105")), "c" * 64,
        tuple(D(value) for value in (levels or ("90", "101", "110"))),
    )


class PaperExecutionTests(unittest.TestCase):
    def test_long_limit_target_and_commissions(self):
        result = run([quote(1), quote(2, "115", "115.20")],
                     config=policy(commission_per_lot_per_side=D("0.10")))
        self.assertEqual(result["fills"][0]["price"], "100.20")
        self.assertEqual(result["fills"][1]["price"], "110")
        self.assertEqual(result["trades"][0]["exit_reason"], "take_profit")
        self.assertEqual(result["fills"][0]["lots"], "1.85")
        self.assertEqual(D(result["total_commission"]), D("0.37"))
        self.assertEqual(D(result["net_pnl"]), D("17.76"))
        self.assertFalse(result["broker_connected"])
        self.assertEqual(result["real_orders_submitted"], 0)
        self.assertFalse(result["promotion_eligible"])

    def test_short_limit_target(self):
        result = run([quote(1), quote(2, "84.80", "85")], [intent(side="sell")])
        self.assertEqual(result["fills"][0]["price"], "100")
        self.assertEqual(result["fills"][1]["price"], "90")
        self.assertEqual(result["trades"][0]["net_pnl"], "20.00")

    def test_long_and_short_stops_fill_at_gapped_quote(self):
        for side, end, expected in (("buy", quote(2, "92", "92.20"), "91.90"),
                                    ("sell", quote(2, "107.80", "108"), "108.10")):
            with self.subTest(side=side):
                result = run([quote(1), end], [intent(side=side)],
                             config=policy(slippage=D("0.10")))
                trade = result["trades"][0]
                self.assertEqual(trade["exit_reason"], "stop_loss")
                self.assertEqual(trade["exit_price"], expected)
                self.assertGreater(-D(trade["net_pnl"]), D(trade["modeled_stop_risk"]))

    def test_adverse_rounding_in_both_directions(self):
        for side, first, last in (("buy", "100.23", "99.97"), ("sell", "99.97", "100.23")):
            with self.subTest(side=side):
                result = run([quote(1)], [intent(side=side)], config=policy(slippage=D("0.025")))
                self.assertEqual(result["fills"][0]["price"], first)
                self.assertEqual(result["fills"][1]["price"], last)

    def test_sizing_includes_both_commissions_and_both_slippages(self):
        result = run([quote(1), quote(2, "95", "95.20")], config=policy(
            slippage=D("0.10"), commission_per_lot_per_side=D("0.50")))
        self.assertEqual(result["fills"][0]["lots"], "1.56")
        trade = result["trades"][0]
        self.assertEqual(D(trade["modeled_stop_risk"]), D("9.984"))
        self.assertEqual(D(trade["net_pnl"]), D("-9.984"))

    def test_never_rounds_up_to_minimum_lot(self):
        result = run([quote(1)], spec=instrument(min_lots=D("2")))
        self.assertEqual(result["fills"], [])
        self.assertIn("below_minimum_lot", reasons(result))

    def test_lot_maximum_is_enforced(self):
        result = run([quote(1)], spec=instrument(max_lots=D("0.50")))
        self.assertEqual(result["fills"][0]["lots"], "0.50")

    def test_sizing_respects_remaining_daily_and_drawdown_allowances(self):
        for limit in ("max_daily_loss_fraction", "max_drawdown_fraction"):
            with self.subTest(limit=limit):
                config = policy(risk_fraction=D("0.10"), **{limit: D("0.05")})
                result = run([quote(1), quote(2, "95", "95.20"), quote(3)],
                             [intent(), intent("b", created=2)], config=config)
                self.assertEqual(result["fills"][0]["lots"], "9.61")
                self.assertEqual(D(result["trades"][0]["modeled_stop_risk"]), D("49.972"))
                self.assertIn("below_minimum_lot", reasons(result))
                self.assertEqual(result["counts"]["accepted_intents"], 1)

    def test_short_target_uses_ask_not_bid(self):
        result = run([quote(1), quote(2, "89.90", "90.10"), quote(3, "89.80", "90")],
                     [intent(side="sell")])
        self.assertEqual(result["trades"][0]["exited_at"], "2026-01-05T12:00:03Z")
        self.assertEqual(result["trades"][0]["exit_reason"], "take_profit")

    def test_reward_risk_uses_cost_adjusted_prices(self):
        result = run([quote(1)], config=policy(min_reward_risk=D("2")))
        self.assertIn("minimum_reward_risk", reasons(result))
        self.assertEqual(result["fills"], [])

    def test_minimum_stop_distance_uses_exit_trigger_side(self):
        result = run([quote(1)], [intent(stop="99.90")],
                     spec=instrument(minimum_stop_distance=D("0.20")))
        self.assertIn("minimum_stop_distance", reasons(result))

    def test_already_breached_stop_or_target_is_rejected(self):
        for request in (intent(stop="100"), intent(target="100.10"), intent(side="sell", stop="100.20"),
                        intent(side="sell", target="100.10")):
            with self.subTest(request=request):
                result = run([quote(1)], [request])
                self.assertIn("invalid_or_already_crossed_bracket", reasons(result))

    def test_source_and_receipt_times_both_gate_entries(self):
        first = PaperQuote(START, START + timedelta(milliseconds=200), D("100"), D("100.20"))
        result = run([first, quote(1)], [intent(not_before=1)])
        self.assertEqual(result["fills"][0]["quote_timestamp"], "2026-01-05T12:00:01Z")

    def test_equal_time_does_not_let_intent_trade_its_creation_quote(self):
        result = run([quote(0), quote(0), quote(1)])
        self.assertEqual(result["fills"][0]["timestamp"], "2026-01-05T12:00:01Z")

    def test_equal_quote_timestamps_keep_original_file_order(self):
        tape = [quote(1), quote(2, "110", "110.20"), quote(2, "94", "94.20")]
        result = run(tape)
        self.assertEqual(result["trades"][0]["exit_reason"], "take_profit")

    def test_expiry_is_exclusive_and_unmatched_intents_cancel_at_end(self):
        result = run([quote(1), quote(2)], [intent(expires=1), intent("later", created=2)])
        self.assertIn("expired", reasons(result))
        self.assertIn("end_of_data", reasons(result))
        self.assertEqual(result["counts"]["cancelled_intents"], 2)

    def test_future_intent_is_not_cancelled_before_it_exists(self):
        result = run([quote(1)], [intent(created=2)])
        self.assertEqual(result["counts"]["cancelled_intents"], 0)
        self.assertEqual(result["counts"]["not_observed_intents"], 1)
        self.assertEqual(result["not_observed_intents"][0]["reason"], "created_after_tape_end")
        self.assertFalse(any("intent_id" in event for event in result["events"]))

    def test_spread_rejection_is_one_attempt_without_retry(self):
        result = run([quote(1, "100", "102"), quote(2)])
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["counts"]["rejected_intents"], 1)
        self.assertIn("spread_exceeds_limit", reasons(result))

    def test_stale_entry_rejection_does_not_latch_halt_while_flat(self):
        result = run([quote(1, delay_ms=1500), quote(3)],
                     [intent(), intent("b", created=2.5)])
        self.assertIn("stale_quote", reasons(result))
        self.assertEqual(result["counts"]["accepted_intents"], 1)
        self.assertIsNone(result["global_halt"])

    def test_stale_open_position_halts_and_waits_for_fresh_liquidation(self):
        result = run([quote(1), quote(2, "94", "94.20", delay_ms=1500), quote(4, "93", "93.20")])
        self.assertEqual(result["global_halt"], "stale_quote_with_open_position")
        self.assertEqual(result["fills"][1]["timestamp"], "2026-01-05T12:00:04Z")
        self.assertEqual(result["fills"][1]["price"], "93")

    def test_final_stale_quote_never_forces_fabricated_liquidation(self):
        result = run([quote(1), quote(2, delay_ms=1500)])
        self.assertEqual(len(result["fills"]), 1)
        self.assertIsNotNone(result["open_position"])
        self.assertFalse(result["final_mark_is_fresh"])
        self.assertIn("last_quote_stale", reasons(result))

    def test_one_position_only_and_no_same_quote_reentry(self):
        result = run([quote(1), quote(2), quote(3, "110", "110.20")],
                     [intent(), intent("busy", created=1), intent("exit", created=2)])
        self.assertIn("position_already_open", reasons(result))
        self.assertIn("no_same_quote_reentry", reasons(result))
        self.assertEqual(result["counts"]["accepted_intents"], 1)

    def test_multiple_due_intents_reject_even_same_direction(self):
        result = run([quote(1)], [intent(), intent("b")])
        self.assertEqual(reasons(result).count("conflicting_intents"), 2)
        self.assertEqual(result["fills"], [])

    def test_drawdown_halt_closes_position_and_remains_sticky(self):
        result = run([quote(1), quote(2, "80", "80.20"), quote(3), quote(86401)],
                     [intent(), intent("b", created=2), intent("c", created=86400, expires=90000)],
                     config=policy(max_drawdown_fraction=D("0.02")))
        self.assertEqual(result["global_halt"], "maximum_drawdown")
        self.assertEqual(result["counts"]["accepted_intents"], 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "maximum_drawdown")

    def test_daily_halt_resets_on_next_received_utc_day(self):
        result = run([quote(1), quote(2, "80", "80.20"), quote(3), quote(86401)],
                     [intent(), intent("b", created=2), intent("c", created=86400, expires=90000)],
                     config=policy(max_daily_loss_fraction=D("0.02")))
        self.assertIn("daily_halt_active", reasons(result))
        self.assertIsNone(result["global_halt"])
        self.assertEqual(result["counts"]["accepted_intents"], 2)

    def test_daily_trade_count_limit(self):
        result = run([quote(1), quote(2, "110", "110.20"), quote(3)],
                     [intent(), intent("b", created=2)], config=policy(max_trades_per_day=1))
        self.assertIn("maximum_trades_per_day", reasons(result))

    def test_max_holding_time_closes_at_first_received_fresh_quote(self):
        result = run([quote(1), quote(10), quote(15)], config=policy(max_holding_seconds=10))
        self.assertEqual(result["trades"][0]["exited_at"], "2026-01-05T12:00:15Z")
        self.assertEqual(result["trades"][0]["exit_reason"], "maximum_holding_time")

    def test_operator_halt_closes_and_blocks_future_entries(self):
        result = run([quote(1), quote(2), quote(3)], [intent(), intent("b", created=2)],
                     config=policy(halt_at=START + timedelta(seconds=2)))
        self.assertEqual(result["trades"][0]["exit_reason"], "operator_halt")
        self.assertEqual(result["global_halt"], "operator_halt")
        self.assertIn("global_halt_active", reasons(result))

    def test_operator_halt_cannot_gain_past_a_triggered_standing_target(self):
        for side, later, target in (("buy", quote(2, "120", "120.20"), "110"),
                                    ("sell", quote(2, "80", "80.20"), "90")):
            with self.subTest(side=side):
                result = run([quote(1), later], [intent(side=side)],
                             config=policy(halt_at=START + timedelta(seconds=2)))
                self.assertEqual(result["fills"][1]["price"], target)
                self.assertEqual(result["fills"][1]["pricing"], "standing_target_limit")

    def test_financing_charges_each_utc_date_crossed_and_reconciles(self):
        result = run([quote(1), quote(86401 * 2)], config=policy(
            financing_per_lot_per_utc_day=D("0.50"), max_holding_seconds=999999))
        trade = result["trades"][0]
        self.assertEqual(D(result["total_financing"]), D("1.92"))
        self.assertEqual(D(result["net_pnl"]), D(trade["net_pnl"]))
        self.assertEqual(D(result["final_balance"]), D("1000") + D(trade["net_pnl"]))

    def test_unforced_open_exposure_includes_exit_costs_in_equity(self):
        result = run([quote(1)], config=policy(force_flat_at_end=False,
                                             commission_per_lot_per_side=D("0.10")))
        self.assertIsNotNone(result["open_position"])
        self.assertEqual(len(result["fills"]), 1)
        self.assertEqual(D(result["final_balance"]), D("999.815"))
        self.assertEqual(D(result["final_equity"]), D("999.26"))

    def test_result_and_input_fingerprints_are_deterministic_and_bound(self):
        original = run([quote(1)])
        self.assertEqual(original, run([quote(1)]))
        saved = original.pop("fingerprint")
        encoded = json.dumps(original, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(saved, hashlib.sha256(encoded).hexdigest())
        changed = run([quote(1)], [replace(intent(), source_fingerprint="b" * 64)])
        self.assertNotEqual(original["input_fingerprint"], changed["input_fingerprint"])

    def test_decimal_context_does_not_change_execution(self):
        baseline = run([quote(1), quote(2)])
        for rounding in (ROUND_FLOOR, ROUND_CEILING):
            with localcontext() as context:
                context.prec = 6
                context.rounding = rounding
                context.traps[Inexact] = True
                self.assertEqual(baseline, run([quote(1), quote(2)]))

    def test_target_gap_does_not_create_unattainable_equity_peak(self):
        result = run([quote(1), quote(2, "1000", "1000.20"), quote(3)],
                     config=policy(max_drawdown_fraction=D("0.02")))
        self.assertIsNone(result["global_halt"])
        self.assertEqual(result["trades"][0]["exit_reason"], "take_profit")

    def test_duplicate_ids_unsorted_times_and_offgrid_prices_rejected(self):
        cases = (([quote(1)], [intent(), intent()]),
                 ([quote(2), quote(1)], []),
                 ([quote(1)], [intent(created=2), intent("b", created=1)]),
                 ([quote(1, "100.001", "100.20")], []),
                 ([quote(1)], [intent(stop="95.001")]),
                 ([], []))
        for tape, requests in cases:
            with self.subTest(tape=tape, requests=requests):
                with self.assertRaises(ValueError):
                    run(tape, requests)

    def test_decreasing_availability_even_with_increasing_source_time_rejected(self):
        with self.assertRaises(ValueError):
            run([quote(1, delay_ms=1500), quote(2)])

    def test_invalid_fields_fail_closed(self):
        for value in (1.0, 1, True, "1", D("NaN"), D("Infinity"), D("0"), D("-1"), D("1e100")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    policy(initial_balance=value)
        for changes in ({"risk_fraction": D("1")}, {"slippage": D("-1")},
                        {"max_quote_age_ms": True}, {"force_flat_at_end": 1},
                        {"halt_at": datetime(2026, 1, 1)}, {"min_reward_risk": D("0")}):
            with self.assertRaises(ValueError):
                policy(**changes)
        with self.assertRaises(ValueError):
            instrument(symbol="EUR_USD")
        with self.assertRaises(ValueError):
            instrument(min_lots=D("0.015"))
        with self.assertRaises(ValueError):
            replace(intent(), side="long")
        with self.assertRaises(ValueError):
            replace(intent(), source_fingerprint="bad")
        with self.assertRaises(ValueError):
            replace(intent(), expires_at=START)
        with self.assertRaises(ValueError):
            PaperQuote(START, START - timedelta(seconds=1), D("1"), D("2"))


class ExactOpenExecutionTests(unittest.TestCase):
    def test_both_sides_select_nearest_directional_frozen_target(self):
        for side, target, exit_quote in (
            ("buy", "101", quote(2, "101", "101.20")),
            ("sell", "90", quote(2, "89.80", "90")),
        ):
            with self.subTest(side=side):
                result = run([quote(1), exit_quote], [exact_intent(side=side)])
                self.assertEqual(result["trades"][0]["take_profit"], target)
                self.assertEqual(result["trades"][0]["exit_reason"], "take_profit")
                accepted = next(event for event in result["events"]
                                if event["kind"] == "intent_accepted")
                self.assertEqual(accepted["take_profit"], target)
                self.assertEqual(accepted["entry_contract"],
                                 "exact_source_boundary_strict_receipt_order")
                self.assertFalse(result["broker_connected"])
                self.assertEqual(result["real_orders_submitted"], 0)

    def test_target_is_selected_after_adverse_entry_rounding(self):
        cases = (
            ("buy", ("100.21", "100.23", "101", "120"), "100.23", "101"),
            ("sell", ("90", "99", "99.97", "99.99"), "99.97", "99"),
        )
        for side, levels, entry, target in cases:
            with self.subTest(side=side):
                result = run([quote(1)], [exact_intent(side=side, levels=levels)],
                             config=policy(slippage=D("0.025"), force_flat_at_end=False))
                self.assertEqual(result["open_position"]["entry_price"], entry)
                self.assertEqual(result["open_position"]["take_profit"], target)

    def test_quote_price_selects_target_not_creation_time(self):
        request = exact_intent(levels=("101", "110", "120"))
        earlier_price = run([quote(1)], [request], config=policy(force_flat_at_end=False))
        boundary_gap = run([quote(1, "101", "101.20")], [request],
                           config=policy(force_flat_at_end=False))
        self.assertEqual(earlier_price["open_position"]["take_profit"], "101")
        self.assertEqual(boundary_gap["open_position"]["take_profit"], "110")
        self.assertEqual(request.target_levels, (D("101"), D("110"), D("120")))

    def test_nearest_target_is_not_skipped_to_meet_reward_risk(self):
        for side, levels in (("buy", ("101", "120")), ("sell", ("80", "99"))):
            with self.subTest(side=side):
                result = run([quote(1), quote(2)],
                             [exact_intent(side=side, levels=levels)],
                             config=policy(min_reward_risk=D("2")))
                self.assertEqual(result["fills"], [])
                self.assertEqual(result["counts"]["rejected_intents"], 1)
                self.assertIn("minimum_reward_risk", reasons(result))

    def test_no_strictly_directional_target_rejects_both_sides(self):
        for side, levels in (("buy", ("90", "100.20")),
                             ("sell", ("100", "110"))):
            with self.subTest(side=side):
                result = run([quote(1)], [exact_intent(side=side, levels=levels)])
                self.assertEqual(result["fills"], [])
                self.assertIn("no_directional_target", reasons(result))

    def test_equal_time_creation_and_receipt_rejects_without_boundary_retry(self):
        result = run([quote(1), quote(1, delay_ms=1), quote(2)],
                     [exact_intent(created=1)])
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["counts"]["rejected_intents"], 1)
        self.assertIn("ambiguous_entry_ordering", reasons(result))

    def test_exact_boundary_received_strictly_after_creation_is_allowed(self):
        result = run([quote(1, delay_ms=1)], [exact_intent(created=1)])
        self.assertEqual(result["counts"]["accepted_intents"], 1)
        self.assertEqual(result["fills"][0]["quote_timestamp"], "2026-01-05T12:00:01Z")
        self.assertEqual(result["fills"][0]["timestamp"], "2026-01-05T12:00:01.001000Z")

    def test_missing_boundary_rejects_once_without_late_entry(self):
        result = run([quote(0), quote(2), quote(3)], [exact_intent()])
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["counts"]["rejected_intents"], 1)
        self.assertIn("missing_exact_open_quote", reasons(result))

    def test_preboundary_source_quote_received_late_cannot_be_open(self):
        old = quote(0.5, delay_ms=501)
        result = run([old, quote(2)], [exact_intent(created=1)])
        self.assertEqual(result["fills"], [])
        rejection = next(event for event in result["events"]
                         if event["kind"] == "intent_rejected")
        self.assertEqual(rejection["reason"], "missing_exact_open_quote")
        self.assertEqual(rejection["quote_timestamp"], "2026-01-05T12:00:02Z")

    def test_stale_boundary_rejects_without_retry(self):
        result = run([quote(1, delay_ms=1500), quote(1, delay_ms=1501), quote(3)],
                     [exact_intent()])
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["counts"]["rejected_intents"], 1)
        self.assertIn("stale_quote", reasons(result))

    def test_spread_rejection_cannot_retry_a_second_boundary_quote(self):
        result = run([quote(1, "100", "102"), quote(1)], [exact_intent()])
        self.assertEqual(result["fills"], [])
        self.assertEqual(result["counts"]["rejected_intents"], 1)
        self.assertIn("spread_exceeds_limit", reasons(result))

    def test_existing_halt_stop_distance_and_lot_guards_still_apply(self):
        cases = (
            (instrument(), policy(halt_at=START), "global_halt_active"),
            (instrument(minimum_stop_distance=D("2")), policy(), "minimum_stop_distance"),
            (instrument(min_lots=D("2")), policy(), "below_minimum_lot"),
        )
        for spec, config, reason in cases:
            with self.subTest(reason=reason):
                result = run([quote(1)], [exact_intent()], spec=spec, config=config)
                self.assertEqual(result["fills"], [])
                self.assertIn(reason, reasons(result))

    def test_expiry_remains_exclusive(self):
        result = run([quote(1, delay_ms=1000)], [exact_intent(expires=2)])
        self.assertEqual(result["fills"], [])
        self.assertIn("expired", reasons(result))

    def test_future_tape_does_not_change_an_accepted_prefix(self):
        request = exact_intent()
        config = policy(force_flat_at_end=False)
        prefix = run([quote(1)], [request], config=config)
        extended = run([quote(1), quote(2, "99", "99.20")], [request], config=config)
        self.assertEqual(prefix["fills"], extended["fills"])
        self.assertEqual(prefix["events"], extended["events"])
        self.assertEqual(prefix["equity_curve"], extended["equity_curve"][:1])
        self.assertEqual(prefix["open_position"], extended["open_position"])

    def test_boundary_beyond_source_tape_is_not_observed_despite_later_receipt(self):
        request = exact_intent(created=1)
        for delay in (500, 501):
            with self.subTest(delay=delay):
                result = run([quote(0.5, delay_ms=delay)], [request])
                self.assertEqual(result["fills"], [])
                self.assertEqual(result["counts"]["cancelled_intents"], 0)
                self.assertEqual(result["counts"]["rejected_intents"], 0)
                self.assertEqual(result["counts"]["not_observed_intents"], 1)
                self.assertEqual(result["not_observed_intents"][0]["reason"],
                                 "entry_boundary_after_tape_end")

    def test_creation_after_receipt_tape_end_keeps_not_observed_priority(self):
        result = run([quote(0.5)], [exact_intent(created=1)])
        self.assertEqual(result["counts"]["not_observed_intents"], 1)
        self.assertEqual(result["not_observed_intents"][0]["reason"], "created_after_tape_end")
        self.assertEqual(result["counts"]["cancelled_intents"], 0)

    def test_expired_intent_before_source_boundary_remains_terminal(self):
        result = run([quote(0.5, delay_ms=1500)], [exact_intent(created=1, expires=2)])
        self.assertEqual(result["counts"]["cancelled_intents"], 1)
        self.assertEqual(result["counts"]["not_observed_intents"], 0)
        self.assertIn("expired", reasons(result))

    def test_legacy_intent_still_waits_after_equal_time_and_allows_later_quote(self):
        result = run([quote(0), quote(2)], [intent()])
        self.assertEqual(result["counts"]["accepted_intents"], 1)
        self.assertEqual(result["fills"][0]["quote_timestamp"], "2026-01-05T12:00:02Z")
        self.assertNotIn("ambiguous_entry_ordering", reasons(result))
        self.assertNotIn("missing_exact_open_quote", reasons(result))

    def test_mixed_due_intents_preserve_conflict_guard(self):
        result = run([quote(1)], [intent(), exact_intent()])
        self.assertEqual(result["fills"], [])
        self.assertEqual(reasons(result).count("conflicting_intents"), 2)

    def test_target_levels_must_be_immutable_positive_unique_and_ascending(self):
        for values in ([], [D("110")], (), (D("110"), D("110")),
                       (D("110"), D("100")), (D("0"),), (D("NaN"),),
                       (D("Infinity"),), ("110",), (110,), (True,)):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    replace(exact_intent(), target_levels=values)
        with self.assertRaises(FrozenInstanceError):
            exact_intent().target_levels = (D("120"),)

    def test_stop_and_each_frozen_target_must_be_on_instrument_grid(self):
        for request in (exact_intent(stop="95.001"),
                        exact_intent(levels=("90.001", "101"))):
            with self.subTest(request=request):
                with self.assertRaisesRegex(ValueError, "price grid"):
                    run([quote(1)], [request])

    def test_malformed_timing_and_missing_source_timestamp_fail_closed(self):
        for changes in ({"created_at": START + timedelta(seconds=2)},
                        {"expires_at": START + timedelta(seconds=1)},
                        {"not_before": datetime(2026, 1, 5)},
                        {"source_fingerprint": "bad"}, {"side": "long"}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    replace(exact_intent(), **changes)
        with self.assertRaises(ValueError):
            PaperQuote(None, START, D("100"), D("100.20"))


if __name__ == "__main__":
    unittest.main()
