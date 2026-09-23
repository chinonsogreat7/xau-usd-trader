import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from session_feature_fixture import write_session_fixture
from xau_trader.cli import main


class SessionDiagnosticCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.csv, self.manifest, self.calendar = write_session_fixture(self.root)
        self.trace = self.root / "trace.json"
        self.decisions = self.root / "decisions.csv"

    def command(self, *extra):
        return ("replay-session-diagnostic", str(self.csv),
                "--manifest", str(self.manifest), "--calendar", str(self.calendar),
                "--trace-json", str(self.trace), "--decisions-csv", str(self.decisions)) + extra

    def assert_rejected(self, *extra, message):
        error = io.StringIO()
        with redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                main(self.command(*extra))
        self.assertEqual(raised.exception.code, 2)
        self.assertIn(message, error.getvalue())

    def test_full_cli_writes_bound_research_evidence_only(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(self.command())
        self.assertEqual(status, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["status"], "session_diagnostic_replay_complete")
        self.assertTrue(summary["diagnostic_only"])
        self.assertIsNone(summary["execution"])
        self.assertEqual(summary["counts"]["decisions"], 184)
        self.assertEqual(summary["counts"]["pre_roll_decisions"], 96)
        self.assertEqual(summary["counts"]["post_pre_roll_decisions"], 88)
        trace = json.loads(self.trace.read_text(encoding="utf-8"))
        self.assertEqual(trace["session"]["calendar_fingerprint"],
                         summary["session"]["calendar_binding"]["content_fingerprint"])
        with self.decisions.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 184)
        self.assertTrue(all(row["execution"] == "" for row in rows))

    def test_v1_and_changed_calendar_cannot_run_strategy(self):
        original = self.manifest.read_bytes()
        payload = json.loads(original)
        payload["version"] = 1
        payload["session_calendar"] = {key: payload["session_calendar"][key] for key in ("id", "version")}
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")
        with patch("xau_trader.cli.run_diagnostic_replay") as replay:
            self.assert_rejected(message="requires manifest v2")
            replay.assert_not_called()
        self.manifest.write_bytes(original)
        self.calendar.write_bytes(self.calendar.read_bytes() + b"\n")
        self.assert_rejected(message="artifact_sha256")
        self.assertFalse(self.trace.exists())
        self.assertFalse(self.decisions.exists())

    def test_outputs_cannot_overwrite_each_other_or_calendar_inputs(self):
        before = self.calendar.read_bytes()
        self.assert_rejected("--trace-json", str(self.calendar), message="cannot replace an input")
        self.assert_rejected("--trace-json", str(self.decisions), message="must be different")
        self.assertEqual(self.calendar.read_bytes(), before)
        self.decisions.write_bytes(b"Retain existing decision artifact")
        self.assert_rejected(message="already exists")
        self.assertEqual(self.decisions.read_bytes(), b"Retain existing decision artifact")
        self.assertFalse(self.trace.exists())

    def test_inputs_changing_during_calendar_load_fail_before_replay(self):
        from xau_trader.session_calendar import load_session_calendar

        def changing_load(path):
            calendar = load_session_calendar(path)
            self.csv.write_bytes(self.csv.read_bytes() + b"\n")
            return calendar
        with patch("xau_trader.cli.load_session_calendar", side_effect=changing_load):
            with patch("xau_trader.cli.run_diagnostic_replay") as replay:
                self.assert_rejected(message="inputs changed")
                replay.assert_not_called()
        self.assertFalse(self.trace.exists())
        self.assertFalse(self.decisions.exists())

    def test_second_artifact_failure_rolls_back_only_this_publication(self):
        import os
        real_link = os.link
        count = [0]

        def fail_second(source, target):
            count[0] += 1
            if count[0] == 2:
                raise OSError("Injected second artifact failure")
            return real_link(source, target)
        with patch("xau_trader.decision_trace.os.link", side_effect=fail_second):
            self.assert_rejected(message="Injected second artifact failure")
        self.assertFalse(self.trace.exists())
        self.assertFalse(self.decisions.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
