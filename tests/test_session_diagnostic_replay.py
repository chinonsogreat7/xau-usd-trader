import csv
import io
import json
import unittest
from dataclasses import replace
from datetime import timedelta

from session_feature_fixture import session_fixture
from xau_trader.candidate_signals import CandidateAction, CandidateReason
from xau_trader.decision_trace import (
    DECISION_CSV_COLUMNS, SESSION_DECISION_CSV_COLUMNS,
    diagnostic_decisions_csv_bytes, diagnostic_trace_json_bytes,
)
from xau_trader.diagnostic_replay import (
    SESSION_DIAGNOSTIC_REPLAY_ENGINE_VERSION,
    run_diagnostic_replay, validate_diagnostic_replay_result,
)
from xau_trader.domain import AvailabilityBasis
from xau_trader.research_baseline import (
    provisional_diagnostic_baseline_v1, provisional_session_diagnostic_baseline_v1,
)


class SessionDiagnosticReplayTests(unittest.TestCase):
    def setUp(self):
        self.bars, self.calendar = session_fixture()
        self.bundle = provisional_session_diagnostic_baseline_v1(self.calendar)

    def replay(self, bars=None, bundle=None, as_of=None):
        return run_diagnostic_replay(
            self.bars if bars is None else bars,
            policy_bundle=self.bundle if bundle is None else bundle,
            dataset_fingerprint="a" * 64, as_of=as_of,
        )

    def test_full_two_session_replay_preserves_history_and_cancels_closed_entries(self):
        result = self.replay()
        self.assertEqual(result.m15_bars, self.bars)
        self.assertEqual(len(result.h1_bars), 46)
        self.assertEqual(len(result.decisions), 184)
        self.assertEqual(len(result.zones), 12)
        self.assertEqual(len(result.m15_confirmations), 4)
        self.assertEqual(len(result.pre_roll_decisions), 96)
        self.assertEqual(len(result.post_pre_roll_decisions), 88)
        self.assertEqual(result.readiness.post_pre_roll_m15_start, self.bars[96].start_time)
        self.assertNotEqual(result.readiness.post_pre_roll_m15_start,
                            self.bars[0].start_time + 96 * timedelta(minutes=15))
        closing = result.decisions[91]
        self.assertEqual(closing.reasons, (CandidateReason.SCHEDULED_CLOSURE_NEXT_OPEN,))
        self.assertEqual(closing.next_m15_open, self.calendar.closures[0].end)
        self.assertIsNone(closing.proposed_entry_at)
        self.assertEqual(result.decisions[-1].reasons, (CandidateReason.CALENDAR_COVERAGE_EXHAUSTED,))
        self.assertEqual(result.engine_version, SESSION_DIAGNOSTIC_REPLAY_ENGINE_VERSION)
        for decision in result.decisions:
            self.assertEqual(decision.calendar, self.calendar)
            if decision.action != CandidateAction.NO_TRADE:
                self.calendar.validate_bar(decision.proposed_entry_at,
                                           decision.proposed_entry_at + timedelta(minutes=15))
                self.assertEqual(decision.proposed_entry_at, decision.m15_bar_end)
        validate_diagnostic_replay_result(result)

    def test_future_suffix_does_not_change_any_visible_stream(self):
        prefix = self.replay(bars=self.bars[:100])
        complete = self.replay()
        cutoff = self.bars[99].timestamp
        for field in ("h1_atr", "h1_pivots", "h1_regimes", "h1_impulses", "zones",
                      "m15_ema", "m15_candles", "m15_confirmations", "decisions"):
            with self.subTest(field=field):
                self.assertEqual(getattr(prefix, field), tuple(
                    event for event in getattr(complete, field) if event.available_at <= cutoff
                ))
        altered = self.bars[:100] + tuple(replace(bar, volume=999999.0) for bar in self.bars[100:])
        self.assertEqual(self.replay(as_of=cutoff), self.replay(bars=altered, as_of=cutoff))

    def test_json_and_csv_expose_calendar_and_remain_deterministic(self):
        result = self.replay()
        encoded = diagnostic_trace_json_bytes(result)
        self.assertEqual(encoded, diagnostic_trace_json_bytes(self.replay()))
        trace = json.loads(encoded)
        self.assertEqual(trace["schema_version"], 2)
        self.assertEqual(trace["replay"]["schema_version"], 3)
        self.assertEqual(trace["session"]["calendar_fingerprint"], self.calendar.fingerprint)
        self.assertEqual(trace["session"]["calendar"], self.calendar.as_dict())
        self.assertEqual(trace["session"]["semantics"]["entry_at_closure"],
                         "cancel_do_not_queue_for_reopening")
        self.assertIsNone(trace["execution"])
        reader = csv.DictReader(io.StringIO(diagnostic_decisions_csv_bytes(result).decode()))
        self.assertEqual(tuple(reader.fieldnames), SESSION_DECISION_CSV_COLUMNS)
        rows = list(reader)
        self.assertEqual(len(rows), 184)
        self.assertTrue(all(row["calendar_fingerprint"] == self.calendar.fingerprint for row in rows))
        self.assertTrue(all(row["execution"] == "" for row in rows))

    def test_export_recomputes_derived_evidence_and_rejects_forgery(self):
        result = self.replay()
        forged = replace(result, h1_atr=(replace(result.h1_atr[0], value=result.h1_atr[0].value + 1),)
                         + result.h1_atr[1:])
        self.assertNotEqual(result.fingerprint, forged.fingerprint)
        with self.assertRaisesRegex(ValueError, "recomputation"):
            diagnostic_trace_json_bytes(forged)
        with self.assertRaisesRegex(ValueError, "recomputation"):
            diagnostic_decisions_csv_bytes(forged)

    def test_policy_identity_binds_calendar_and_mixed_calendars_fail(self):
        changed = replace(self.calendar, revision="fixture-v2")
        other = provisional_session_diagnostic_baseline_v1(changed)
        self.assertNotEqual(self.bundle.fingerprint, other.fingerprint)
        self.assertNotEqual(self.replay().fingerprint, self.replay(bundle=other).fingerprint)
        with self.assertRaisesRegex(ValueError, "same session calendar"):
            replace(self.bundle, lifecycle_policy=other.lifecycle_policy)
        with self.assertRaisesRegex(ValueError, "same session calendar"):
            replace(self.bundle, confirmation_policy=provisional_diagnostic_baseline_v1().confirmation_policy)
        with self.assertRaises(TypeError):
            provisional_session_diagnostic_baseline_v1(None)

    def test_unknown_open_gap_and_incomplete_warmup_are_not_waived(self):
        with self.assertRaises(ValueError):
            self.replay(bars=self.bars[:20] + self.bars[24:])
        with self.assertRaises(ValueError):
            self.replay(bars=self.bars[:92])
        with self.assertRaises(ValueError):
            self.replay(bundle=provisional_session_diagnostic_baseline_v1(
                replace(self.calendar, closures=()),
            ))
        with self.assertRaises(ValueError):
            self.replay(bars=self.bars[:-1])

    def test_delayed_receipts_beyond_calendar_are_not_tradable_or_backdated(self):
        receipt = self.calendar.coverage_end + timedelta(hours=2)
        bars = tuple(replace(bar, available_at=receipt,
                             availability_basis=AvailabilityBasis.OBSERVED_RECEIPT)
                     for bar in self.bars)
        result = self.replay(bars=bars)
        self.assertEqual(len(result.decisions), 184)
        self.assertTrue(all(decision.action == CandidateAction.NO_TRADE for decision in result.decisions))
        self.assertTrue(all(decision.available_at == receipt for decision in result.decisions))
        self.assertTrue(all(zone.available_at == receipt for zone in result.zones))
        validate_diagnostic_replay_result(result)

    def test_strict_path_still_rejects_gaps_and_keeps_old_export_schema(self):
        strict = provisional_diagnostic_baseline_v1()
        with self.assertRaises(ValueError):
            self.replay(bundle=strict)
        result = self.replay(bars=self.bars[:92] + tuple(replace(
            bar, start_time=bar.start_time - timedelta(hours=1),
            timestamp=bar.timestamp - timedelta(hours=1),
            available_at=bar.available_at - timedelta(hours=1),
        ) for bar in self.bars[92:]), bundle=strict)
        trace = json.loads(diagnostic_trace_json_bytes(result))
        self.assertEqual(trace["schema_version"], 1)
        self.assertNotIn("session", trace)
        reader = csv.DictReader(io.StringIO(diagnostic_decisions_csv_bytes(result).decode()))
        self.assertEqual(tuple(reader.fieldnames), DECISION_CSV_COLUMNS)


if __name__ == "__main__":
    unittest.main()
