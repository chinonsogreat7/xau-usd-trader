import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from session_feature_fixture import write_session_fixture
from xau_trader.cli import main
from xau_trader.data import sha256_file
from xau_trader.session_calendar import load_session_calendar


class SessionFeatureCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.csv, self.manifest, self.calendar = write_session_fixture(self.root)
        self.output = self.root / "report.json"

    def run_cli(self, *extra, output=None):
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = main((
                "replay-session-features", str(self.csv),
                "--manifest", str(self.manifest), "--calendar", str(self.calendar),
                "--trace-json", str(self.output if output is None else output),
            ) + extra)
        return status, json.loads(stream.getvalue())

    def assert_rejected(self, *extra, output=None, message=None):
        error = io.StringIO()
        with redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                self.run_cli(*extra, output=output)
        self.assertEqual(raised.exception.code, 2)
        if message is not None:
            self.assertIn(message, error.getvalue())
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def edit_manifest(self, edit):
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        edit(payload)
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")

    def rebind_calendar_hashes(self):
        calendar = load_session_calendar(self.calendar)
        self.edit_manifest(lambda payload: payload["session_calendar"].update({
            "artifact_sha256": sha256_file(self.calendar),
            "content_fingerprint": calendar.fingerprint,
        }))

    def test_success_is_deterministic_feature_evidence_only(self):
        before = {path: path.read_bytes() for path in (self.csv, self.manifest, self.calendar)}
        with patch("xau_trader.cli.run_diagnostic_replay") as strategy:
            status, summary = self.run_cli()
        strategy.assert_not_called()
        self.assertEqual(status, 0)
        self.assertEqual(summary["status"], "session_features_complete")
        self.assertFalse(summary["strategy_candidates_generated"])
        self.assertFalse(summary["full_strategy_replay_supported"])
        self.assertIsNone(summary["execution"])
        self.assertEqual(summary["counts"]["m15_bars"], 184)
        self.assertEqual(summary["counts"]["h1_bars"], 46)
        self.assertEqual(summary["counts"]["ema_events"], 177)
        self.assertEqual(summary["counts"]["atr_events"], 33)
        self.assertEqual(summary["counts"]["scheduled_gaps"], 1)
        self.assertEqual(summary["calendar_binding"]["artifact_sha256"], sha256_file(self.calendar))
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["fingerprint"], summary["fingerprint"])
        self.assertEqual(report["calendar_fingerprint"], summary["calendar_binding"]["content_fingerprint"])
        self.assertNotIn("decisions", report)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        other = self.root / "second-report.json"
        self.run_cli(output=other)
        self.assertEqual(self.output.read_bytes(), other.read_bytes())

    def test_v1_manifest_is_not_accepted_as_bound_calendar(self):
        def use_v1(payload):
            payload["version"] = 1
            payload["session_calendar"] = {
                key: payload["session_calendar"][key] for key in ("id", "version")
            }
        self.edit_manifest(use_v1)
        self.assert_rejected(message="calendar binding requires manifest v2")

    def test_calendar_bytes_must_match_even_if_semantics_are_unchanged(self):
        original_fingerprint = load_session_calendar(self.calendar).fingerprint
        self.calendar.write_bytes(self.calendar.read_bytes() + b"\n")
        self.assertEqual(load_session_calendar(self.calendar).fingerprint, original_fingerprint)
        self.assert_rejected(message="artifact_sha256 does not match")

    def test_hash_rebinding_cannot_hide_provider_mismatch_or_unknown_gap(self):
        original = json.loads(self.calendar.read_text(encoding="utf-8"))
        changed = dict(original, provider_name="Different synthetic provider")
        self.calendar.write_text(json.dumps(changed), encoding="utf-8")
        self.rebind_calendar_hashes()
        self.assert_rejected(message="provider_name does not match")
        changed = dict(original, closures=[])
        self.calendar.write_text(json.dumps(changed), encoding="utf-8")
        self.rebind_calendar_hashes()
        self.assert_rejected(message="closure")

    def test_separate_raw_artifact_is_required_and_bound(self):
        raw = self.root / "source.txt"
        raw.write_bytes(b"Synthetic raw source distinct from normalized CSV")
        self.edit_manifest(lambda payload: payload["hashes"].update({"raw_sha256": sha256_file(raw)}))
        self.assert_rejected(message="raw_sha256")
        status, _ = self.run_cli("--raw-data", str(raw))
        self.assertEqual(status, 0)

    def test_existing_output_and_input_aliases_are_never_overwritten(self):
        existing = self.root / "existing.json"
        existing.write_bytes(b"Keep existing evidence")
        self.assert_rejected(output=existing, message="already exists")
        self.assertEqual(existing.read_bytes(), b"Keep existing evidence")
        raw = self.root / "separate-raw.txt"
        raw.write_bytes(b"Keep source bytes")
        for protected in (self.csv, self.manifest, self.calendar, raw):
            with self.subTest(protected=protected):
                before = protected.read_bytes()
                alias = self.root / (protected.name + ".alias")
                alias.symlink_to(protected)
                self.assert_rejected("--raw-data", str(raw), output=alias, message="cannot replace an input")
                self.assertEqual(protected.read_bytes(), before)
        dangling = self.root / "dangling.json"
        missing = self.root / "missing.json"
        dangling.symlink_to(missing)
        self.assert_rejected(output=dangling, message="already exists")
        self.assertFalse(missing.exists())

    def test_calendar_change_during_load_is_rejected(self):
        def changing_load(path):
            result = load_session_calendar(path)
            Path(path).write_bytes(Path(path).read_bytes() + b"\n")
            return result
        with patch("xau_trader.cli.load_session_calendar", side_effect=changing_load):
            self.assert_rejected(message="inputs changed")

    def test_source_change_during_calendar_load_is_rejected(self):
        def changing_load(path):
            result = load_session_calendar(path)
            self.csv.write_bytes(self.csv.read_bytes() + b"\n")
            return result
        with patch("xau_trader.cli.load_session_calendar", side_effect=changing_load):
            self.assert_rejected(message="inputs changed")

    def test_publish_failure_discards_staging(self):
        with patch("xau_trader.cli.os.link", side_effect=OSError("Injected publish failure")):
            self.assert_rejected(message="Injected publish failure")

    def test_late_output_collision_does_not_overwrite(self):
        def colliding_link(source, target):
            Path(target).write_bytes(b"Other writer owns this report")
            raise FileExistsError("Late collision")
        with patch("xau_trader.cli.os.link", side_effect=colliding_link):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.run_cli()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.output.read_bytes(), b"Other writer owns this report")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_strict_strategy_commands_do_not_accept_manifest_v2(self):
        commands = (
            ("check-replay-data", str(self.csv), "--manifest", str(self.manifest)),
            ("replay-diagnostic", str(self.csv), "--manifest", str(self.manifest),
             "--trace-json", str(self.output), "--decisions-csv", str(self.root / "decisions.csv")),
        )
        for command in commands:
            with self.subTest(command=command[0]):
                error = io.StringIO()
                with redirect_stderr(error), patch("xau_trader.cli.run_diagnostic_replay") as strategy:
                    with self.assertRaises(SystemExit) as raised:
                        main(command)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("replay-session-features", error.getvalue())
                strategy.assert_not_called()
                self.assertFalse(self.output.exists())
                self.assertFalse((self.root / "decisions.csv").exists())


if __name__ == "__main__":
    unittest.main()
