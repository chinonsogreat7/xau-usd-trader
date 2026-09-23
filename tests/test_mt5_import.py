import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mt5_import_fixture import write_mt5_import_fixture
from session_feature_fixture import session_fixture
from xau_trader.cli import main
from xau_trader.data import quote_bars_csv_bytes, read_quote_bars_csv, sha256_file, write_quote_bars_csv
from xau_trader.dataset_manifest import load_dataset_manifest, validate_dataset_binding
from xau_trader.mt5_import import _publish_new_artifacts, import_mt5_tick_file


class Mt5ImportIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw, self.plan, self.calendar = write_mt5_import_fixture(self.root)
        self.output = self.root / "normalized.csv"
        self.manifest = self.root / "normalized.manifest.json"
        self.report = self.root / "import-report.json"

    def arguments(self):
        return dict(plan_path=self.plan, calendar_path=self.calendar,
                    output_csv=self.output, output_manifest=self.manifest, report_json=self.report)

    def run_import(self, **overrides):
        arguments = self.arguments()
        arguments.update(overrides)
        return import_mt5_tick_file(self.raw, **arguments)

    def assert_no_artifacts(self):
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.report.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_normalizes_exact_bid_ask_history_without_running_strategy(self):
        before = {path: path.read_bytes() for path in (self.raw, self.plan, self.calendar)}
        with patch("xau_trader.cli.run_diagnostic_replay") as replay:
            summary = self.run_import()
            replay.assert_not_called()
        self.assertEqual(summary["status"], "mt5_tick_normalization_complete")
        self.assertEqual(summary["counts"]["input_ticks"], 736)
        self.assertEqual(summary["counts"]["m15_bars"], 184)
        self.assertEqual(summary["counts"]["scheduled_gaps"], 1)
        self.assertFalse(summary["strategy_replay_executed"])
        self.assertIsNone(summary["execution"])
        bars, _ = session_fixture()
        expected = tuple(replace(bar, volume=None) for bar in bars)
        self.assertEqual(tuple(read_quote_bars_csv(self.output)), expected)
        self.assertEqual(self.output.read_bytes(), quote_bars_csv_bytes(expected))
        manifest = load_dataset_manifest(self.manifest)
        validate_dataset_binding(manifest, expected, raw_sha256=sha256_file(self.raw),
                                 normalized_csv_sha256=sha256_file(self.output))
        parameters = dict(manifest.source.request_parameters)
        self.assertEqual(parameters["bar_open_semantics"], "first_observed_tick_not_guaranteed_boundary_quote")
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertFalse(report["tick_completeness_verified"])
        self.assertFalse(report["strategy_readiness_assessed"])
        self.assertEqual(report["bar_tick_evidence"][0]["tick_count"], 4)
        self.assertEqual(report["bar_tick_evidence"][0]["start_boundary_silence_seconds"], 0.1)
        fingerprint = report.pop("fingerprint")
        self.assertEqual(fingerprint, hashlib.sha256(json.dumps(
            report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")).hexdigest())
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_import_outputs_and_report_are_deterministic(self):
        self.run_import()
        other = (self.root / "other.csv", self.root / "other.manifest.json", self.root / "other-report.json")
        self.run_import(output_csv=other[0], output_manifest=other[1], report_json=other[2])
        for first, second in zip((self.output, self.manifest, self.report), other):
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_fixed_positive_offset_rolls_dates_back_to_utc(self):
        self.raw, self.plan, self.calendar = write_mt5_import_fixture(self.root, offset_minutes=120)
        self.run_import()
        bars, _ = session_fixture()
        self.assertEqual(read_quote_bars_csv(self.output)[0].start_time, bars[0].start_time)
        self.assertEqual(read_quote_bars_csv(self.output)[-1].timestamp, bars[-1].timestamp)

    def test_new_byte_serializer_preserves_existing_writer_bytes(self):
        bars, _ = session_fixture()
        path = self.root / "old-writer.csv"
        write_quote_bars_csv(path, bars)
        self.assertEqual(path.read_bytes(), quote_bars_csv_bytes(bars))

    def test_raw_or_calendar_edits_are_rejected_before_publication(self):
        original = self.raw.read_bytes()
        self.raw.write_bytes(original.replace(b"\t0\t0\t6", b"\t0\t1\t6", 1))
        with self.assertRaisesRegex(ValueError, "raw export hash"):
            self.run_import()
        self.assert_no_artifacts()
        self.raw.write_bytes(original)
        self.calendar.write_bytes(self.calendar.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "artifact_sha256"):
            self.run_import()
        self.assert_no_artifacts()

    def test_input_changes_during_load_are_detected(self):
        from xau_trader.mt5_import_plan import parse_mt5_import_plan_bytes

        def changing_parse(raw):
            plan = parse_mt5_import_plan_bytes(raw)
            self.plan.write_bytes(self.plan.read_bytes() + b"\n")
            return plan
        with patch("xau_trader.mt5_import.parse_mt5_import_plan_bytes", side_effect=changing_parse):
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                self.run_import()
        self.assert_no_artifacts()

    def test_transient_metadata_changes_cannot_change_the_parsed_snapshot(self):
        from xau_trader.mt5_import_plan import parse_mt5_import_plan_bytes
        from xau_trader.session_calendar import parse_session_calendar_bytes

        original_plan = self.plan.read_bytes()
        original_calendar = self.calendar.read_bytes()

        def transient_plan(raw):
            self.plan.write_bytes(original_plan.replace(b"XAUUSD_fixture", b"OTHER_FIXTURE"))
            try:
                return parse_mt5_import_plan_bytes(raw)
            finally:
                self.plan.write_bytes(original_plan)

        def transient_calendar(raw):
            self.calendar.write_bytes(b"{}")
            try:
                return parse_session_calendar_bytes(raw)
            finally:
                self.calendar.write_bytes(original_calendar)

        with patch("xau_trader.mt5_import.parse_mt5_import_plan_bytes", side_effect=transient_plan), \
                patch("xau_trader.mt5_import.parse_session_calendar_bytes", side_effect=transient_calendar):
            self.run_import()
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["import_plan"]["source"]["source_symbol"], "XAUUSD_fixture")
        self.assertEqual(report["input_hashes"]["import_plan_file_sha256"], hashlib.sha256(original_plan).hexdigest())
        self.assertEqual(report["input_hashes"]["calendar_file_sha256"], hashlib.sha256(original_calendar).hexdigest())

    def test_each_input_is_bounded_before_parsing_or_publication(self):
        for constant in ("MAX_RAW_BYTES", "MAX_PLAN_BYTES", "MAX_CALENDAR_BYTES"):
            with self.subTest(constant=constant), patch("xau_trader.mt5_import." + constant, 1):
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    self.run_import()
                self.assert_no_artifacts()

    def test_nonregular_inputs_fail_without_reading_them(self):
        for invalid in (self.root, self.root / "ticks.fifo"):
            if invalid != self.root:
                os.mkfifo(invalid)
            with self.subTest(path=invalid):
                with self.assertRaisesRegex(ValueError, "regular files"):
                    import_mt5_tick_file(invalid, **self.arguments())
                self.assert_no_artifacts()

    def test_outputs_cannot_alias_inputs_or_existing_files(self):
        for source in (self.raw, self.plan, self.calendar):
            before = source.read_bytes()
            alias = self.root / (source.name + ".alias")
            alias.symlink_to(source)
            with self.assertRaisesRegex(ValueError, "cannot replace an input"):
                self.run_import(output_csv=alias)
            self.assertEqual(source.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "must be different"):
            self.run_import(output_manifest=self.output)
        self.report.write_bytes(b"Retain existing report")
        with self.assertRaises(FileExistsError):
            self.run_import()
        self.assertEqual(self.report.read_bytes(), b"Retain existing report")
        self.assertFalse(self.output.exists())
        self.assertFalse(self.manifest.exists())

    def test_dangling_output_symlink_is_rejected(self):
        missing = self.root / "not-created.json"
        self.report.symlink_to(missing)
        with self.assertRaises(FileExistsError):
            self.run_import()
        self.assertTrue(self.report.is_symlink())
        self.assertFalse(missing.exists())

    def test_third_publish_failure_removes_only_our_created_links(self):
        link = os.link
        count = [0]

        def fail_third(source, target):
            count[0] += 1
            if count[0] == 3:
                raise OSError("Injected third publish failure")
            return link(source, target)
        with patch("xau_trader.mt5_import.os.link", side_effect=fail_third):
            with self.assertRaisesRegex(OSError, "third publish"):
                self.run_import()
        self.assert_no_artifacts()

    def test_rollback_preserves_another_writers_replacement(self):
        link = os.link
        count = [0]

        def replace_then_fail(source, target):
            count[0] += 1
            if count[0] == 2:
                self.output.unlink()
                self.output.write_bytes(b"Different writer owns this output")
                raise OSError("Injected competing writer")
            return link(source, target)
        with patch("xau_trader.mt5_import.os.link", side_effect=replace_then_fail):
            with self.assertRaisesRegex(OSError, "competing writer"):
                self.run_import()
        self.assertEqual(self.output.read_bytes(), b"Different writer owns this output")
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.report.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_staged_identity_is_captured_before_any_output_is_published(self):
        link = os.link
        path_stat = Path.stat
        published = []

        def note_publication(source, target):
            link(source, target)
            published.append(target)

        def prohibit_late_staging_stat(path, *args, **kwargs):
            if published and path.name.endswith(".tmp"):
                raise OSError("Staging stat unavailable after publication")
            return path_stat(path, *args, **kwargs)

        with patch("xau_trader.mt5_import.os.link", side_effect=note_publication), \
                patch.object(Path, "stat", prohibit_late_staging_stat):
            _publish_new_artifacts(((self.output, b"one"), (self.report, b"two")))
        self.assertEqual(self.output.read_bytes(), b"one")
        self.assertEqual(self.report.read_bytes(), b"two")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_cli_import_then_explicit_session_replay(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = main(("import-mt5-ticks", str(self.raw), "--plan", str(self.plan),
                           "--calendar", str(self.calendar), "--output-csv", str(self.output),
                           "--manifest", str(self.manifest), "--report-json", str(self.report)))
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stream.getvalue())["counts"]["m15_bars"], 184)
        trace = self.root / "strategy.json"
        decisions = self.root / "decisions.csv"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main((
                "replay-session-diagnostic", str(self.output), "--manifest", str(self.manifest),
                "--calendar", str(self.calendar), "--raw-data", str(self.raw),
                "--trace-json", str(trace), "--decisions-csv", str(decisions),
            )), 0)
        result = json.loads(trace.read_text(encoding="utf-8"))
        self.assertEqual(result["counts"]["decisions"], 184)
        self.assertIsNone(result["execution"])

    def test_cli_errors_have_no_artifacts(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(("import-mt5-ticks", str(self.raw), "--plan", str(self.plan),
                      "--calendar", str(self.calendar), "--output-csv", str(self.output),
                      "--manifest", str(self.manifest), "--report-json", str(self.raw)))
        self.assertEqual(raised.exception.code, 2)
        self.assert_no_artifacts()


if __name__ == "__main__":
    unittest.main()
