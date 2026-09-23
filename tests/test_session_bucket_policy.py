"""Synthetic policy regressions, not evidence of strategy profitability."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from xau_trader.domain import AvailabilityBasis
from xau_trader.mt5_ticks import parse_mt5_tick_bytes
from xau_trader.session_bucket_policy import evaluate_session_bucket_policy
from xau_trader.session_buckets import aggregate_mt5_session_buckets
from xau_trader.session_calendar import ScheduledClosure, SessionCalendarArtifact


START = datetime(2026, 9, 20, 20, tzinfo=timezone.utc)


def fixture(*, closure_start=58, closure_end=121, missing_minute=None, hours=4):
    end = START + timedelta(hours=hours)
    calendar = SessionCalendarArtifact(
        "synthetic-partial-policy", "fixture-v1", "Synthetic provider", "Synthetic entity",
        "XAU_USD", "synthetic_quotes", START, end, "local-fixture://partial-policy", end,
        (ScheduledClosure(START + timedelta(minutes=closure_start), START + timedelta(minutes=closure_end), "synthetic scheduled break"),),
    )
    lines = ["<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>"]
    for minute in range(hours * 60):
        if closure_start <= minute < closure_end or minute == missing_minute:
            continue
        at = START + timedelta(minutes=minute, milliseconds=100)
        bid = 2000 + minute / 10
        lines.append("{}\t{}\t{:.3f}\t{:.3f}\t\t".format(at.strftime("%Y.%m.%d"), at.strftime("%H:%M:%S.%f")[:-3], bid, bid + 0.2))
    parsed = parse_mt5_tick_bytes(("\n".join(lines) + "\n").encode(), utc_offset_minutes=0)
    return aggregate_mt5_session_buckets(parsed, coverage_start=START, coverage_end=end,
                                        calendar=calendar, availability_basis=AvailabilityBasis.SYNTHETIC)


class PartialCandlePolicyTests(unittest.TestCase):
    def test_partial_m15_enters_indicators_and_signals_once(self):
        result = evaluate_session_bucket_policy(fixture())
        close = result["m15"][3]
        self.assertEqual(close["state"], "partial_open")
        self.assertEqual(close["scheduled_open_microseconds"], 780000000)
        self.assertTrue(close["indicator_input_eligible"])
        self.assertTrue(close["signal_input_eligible"])
        self.assertFalse(close["next_nominal_entry_boundary_open"])
        self.assertEqual(close["available_at"], "2026-09-20T21:00:00Z")
        opening = result["m15"][8]
        self.assertEqual(opening["scheduled_open_microseconds"], 840000000)
        self.assertTrue(opening["signal_input_eligible"])
        self.assertTrue(opening["next_nominal_entry_boundary_open"])
        self.assertEqual(result["counts"]["included_partial_m15"], 2)

    def test_partial_h1_preserves_observed_ohlc_no_padding(self):
        source = fixture()
        result = evaluate_session_bucket_policy(source)
        hour = result["h1"][0]
        self.assertTrue(hour["indicator_input_eligible"])
        self.assertEqual(hour["scheduled_open_microseconds"], 3480000000)
        self.assertEqual(hour["observed_ohlc"]["bid_close"], 2005.7)
        self.assertEqual(result["h1"][1]["observation_status"], "closed")
        self.assertIsNone(result["h1"][1]["observed_ohlc"])
        self.assertEqual(result["counts"]["eligible_h1"], 3)

    def test_closed_children_are_not_fabricated_for_partial_hour(self):
        result = evaluate_session_bucket_policy(fixture(closure_start=32))
        hour = result["h1"][0]
        self.assertTrue(hour["indicator_input_eligible"])
        self.assertEqual(hour["scheduled_open_m15_count"], 3)
        self.assertEqual(hour["observed_open_m15_count"], 3)
        self.assertEqual(hour["scheduled_open_microseconds"], 1920000000)

    def test_missing_short_open_fragment_blocks_its_hour_and_replay(self):
        result = evaluate_session_bucket_policy(fixture(closure_start=1, closure_end=121, missing_minute=0))
        self.assertTrue(result["missing_open_data_blocks_replay"])
        self.assertFalse(result["m15"][0]["indicator_input_eligible"])
        self.assertFalse(result["h1"][0]["signal_input_eligible"])
        self.assertIsNone(result["h1"][0]["observed_ohlc"])

    def test_missing_second_open_fragment_blocks_otherwise_observed_partial_candle(self):
        source = fixture(closure_start=5, closure_end=10, hours=1)
        # Keep real observations from the first open fragment [20:00,20:05),
        # but omit all of [20:10,20:15). This must not become a closed interval.
        missing_start = START + timedelta(minutes=10)
        missing_end = START + timedelta(minutes=15)
        ticks = tuple(tick for tick in source.parsed.ticks
                      if not missing_start <= tick.timestamp < missing_end)
        # In-memory synthetic fixture transformation; production IO additionally
        # binds the raw file bytes and cannot reuse an unrelated source digest.
        changed = aggregate_mt5_session_buckets(
            replace(source.parsed, ticks=ticks), coverage_start=source.coverage_start,
            coverage_end=source.coverage_end, calendar=source.calendar,
            availability_basis=source.availability_basis,
        )
        self.assertEqual([segment.tick_count for segment in changed.buckets[0].open_segments], [5, 0])
        result = evaluate_session_bucket_policy(changed)
        partial = result["m15"][0]
        self.assertEqual(partial["state"], "partial_open")
        self.assertEqual(partial["observation_status"], "missing_open_data")
        self.assertEqual(partial["tick_count"], 5)
        self.assertEqual(partial["observed_ohlc"]["bid_close"], 2000.4)
        self.assertEqual(partial["scheduled_open_microseconds"], 600000000)
        self.assertFalse(partial["indicator_input_eligible"])
        self.assertFalse(partial["signal_input_eligible"])
        self.assertFalse(partial["next_nominal_entry_input_eligible"])
        hour = result["h1"][0]
        self.assertEqual(hour["observation_status"], "missing_open_data")
        self.assertEqual(hour["scheduled_open_m15_count"], 4)
        self.assertEqual(hour["observed_open_m15_count"], 3)
        self.assertFalse(hour["indicator_input_eligible"])
        self.assertFalse(hour["signal_input_eligible"])
        self.assertIsNone(hour["observed_ohlc"])
        self.assertTrue(result["missing_open_data_blocks_replay"])

    def test_partial_h1_first_and_last_prices_ignore_closed_outer_quarters(self):
        source = fixture(closure_start=0, closure_end=15, hours=1)
        closing = START + timedelta(minutes=45)
        calendar = replace(source.calendar, closures=source.calendar.closures + (
            ScheduledClosure(closing, source.coverage_end, "Synthetic final-quarter closure"),
        ))
        ticks = []
        for tick in source.parsed.ticks:
            if tick.timestamp >= closing:
                continue
            if tick.timestamp.minute == 20:
                tick = replace(tick, bid=2050.0, ask=2050.2)
            elif tick.timestamp.minute == 30:
                tick = replace(tick, bid=1990.0, ask=1990.2)
            ticks.append(tick)
        changed = aggregate_mt5_session_buckets(
            replace(source.parsed, ticks=tuple(ticks)), coverage_start=source.coverage_start,
            coverage_end=source.coverage_end, calendar=calendar,
            availability_basis=source.availability_basis,
        )
        result = evaluate_session_bucket_policy(changed)
        self.assertEqual([row["state"] for row in result["m15"]], ["closed", "full_open", "full_open", "closed"])
        self.assertIsNone(result["m15"][0]["observed_ohlc"])
        self.assertIsNone(result["m15"][3]["observed_ohlc"])
        hour = result["h1"][0]
        self.assertEqual(hour["state"], "partial_open")
        self.assertEqual(hour["observation_status"], "observed")
        self.assertEqual(hour["scheduled_open_m15_count"], 2)
        self.assertEqual(hour["observed_open_m15_count"], 2)
        self.assertEqual(hour["scheduled_open_microseconds"], 1800000000)
        self.assertEqual(hour["start_time"], "2026-09-20T20:00:00Z")
        self.assertEqual(hour["end_time"], "2026-09-20T21:00:00Z")
        self.assertEqual(hour["available_at"], "2026-09-20T21:00:00Z")
        self.assertEqual(hour["observed_ohlc"], {
            "bid_open": 2001.5, "bid_high": 2050.0, "bid_low": 1990.0, "bid_close": 2004.4,
            "ask_open": 2001.7, "ask_high": 2050.2, "ask_low": 1990.2, "ask_close": 2004.6,
        })
        self.assertTrue(hour["indicator_input_eligible"])
        self.assertTrue(hour["signal_input_eligible"])
        self.assertFalse(result["missing_open_data_blocks_replay"])

    def test_final_boundary_not_executable_by_default(self):
        result = evaluate_session_bucket_policy(fixture())
        self.assertFalse(result["m15"][-1]["next_nominal_entry_boundary_open"])
        self.assertFalse(result["strategy_replay_executed"])
        self.assertIsNone(result["execution"])

    def test_reopening_inside_next_slot_does_not_open_earlier_boundary(self):
        result = evaluate_session_bucket_policy(fixture(closure_end=61))
        prior = result["m15"][3]
        reopening = result["m15"][4]
        self.assertTrue(prior["signal_input_eligible"])
        self.assertFalse(prior["next_nominal_entry_boundary_open"])
        self.assertFalse(prior["next_nominal_entry_input_eligible"])
        self.assertEqual(prior["next_nominal_entry_boundary_reason"], "next_boundary_inside_scheduled_closure")
        self.assertEqual(reopening["start_time"], "2026-09-20T21:00:00Z")
        self.assertTrue(reopening["signal_input_eligible"])
        self.assertEqual(reopening["available_at"], "2026-09-20T21:15:00Z")

    def test_boundary_open_at_exact_reopening_is_not_a_signal_from_closed_bucket(self):
        result = evaluate_session_bucket_policy(fixture(closure_end=120))
        closed = result["m15"][7]
        self.assertEqual(closed["state"], "closed")
        self.assertFalse(closed["signal_input_eligible"])
        self.assertTrue(closed["next_nominal_entry_boundary_open"])
        self.assertFalse(closed["next_nominal_entry_input_eligible"])
        self.assertEqual(closed["next_nominal_entry_boundary_reason"], "scheduled_open_not_proof_of_executable_quote")

    def test_future_prices_do_not_change_earlier_policy_inputs(self):
        source = fixture()
        future_start = START + timedelta(hours=3)
        changed_ticks = tuple(
            replace(tick, bid=tick.bid + 100, ask=tick.ask + 100) if tick.timestamp >= future_start else tick
            for tick in source.parsed.ticks
        )
        # This is an in-memory synthetic causality test, not a new verified
        # source export. Production IO independently binds the actual bytes.
        changed = aggregate_mt5_session_buckets(
            replace(source.parsed, ticks=changed_ticks), coverage_start=source.coverage_start,
            coverage_end=source.coverage_end, calendar=source.calendar,
            availability_basis=source.availability_basis,
        )
        before = evaluate_session_bucket_policy(source)
        after = evaluate_session_bucket_policy(changed)
        self.assertEqual(before["m15"][:12], after["m15"][:12])
        self.assertEqual(before["h1"][:3], after["h1"][:3])
        self.assertNotEqual(before["m15"][12:], after["m15"][12:])

    def test_partial_prices_are_used_without_duration_scaling(self):
        source = fixture()
        close_time = START + timedelta(minutes=57, milliseconds=100)
        ticks = tuple(replace(tick, bid=2500.0, ask=2500.2) if tick.timestamp == close_time else tick
                      for tick in source.parsed.ticks)
        changed = aggregate_mt5_session_buckets(
            replace(source.parsed, ticks=ticks), coverage_start=source.coverage_start,
            coverage_end=source.coverage_end, calendar=source.calendar,
            availability_basis=source.availability_basis,
        )
        result = evaluate_session_bucket_policy(changed)
        self.assertEqual(result["m15"][3]["observed_ohlc"]["bid_close"], 2500.0)
        self.assertEqual(result["h1"][0]["observed_ohlc"]["bid_close"], 2500.0)
        self.assertTrue(result["m15"][3]["signal_input_eligible"])
        self.assertTrue(result["h1"][0]["signal_input_eligible"])

    def test_forged_bucket_flags_rejected(self):
        source = fixture()
        fake = replace(source.buckets[3], state="full_open")
        with self.assertRaises(ValueError):
            evaluate_session_bucket_policy(replace(source, buckets=source.buckets[:3] + (fake,) + source.buckets[4:]))

    def test_determinism_and_source_binding(self):
        first = evaluate_session_bucket_policy(fixture())
        self.assertEqual(first, evaluate_session_bucket_policy(fixture()))
        second = evaluate_session_bucket_policy(fixture(closure_start=57))
        self.assertEqual(first["policy_fingerprint"], second["policy_fingerprint"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])


if __name__ == "__main__":
    unittest.main()
