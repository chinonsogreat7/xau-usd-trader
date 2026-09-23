"""Import boundary tests using generated quotes and a fictional session calendar."""

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from partial_session_import_fixture import write_partial_session_import_fixture
from xau_trader.cli import main
from xau_trader.dataset_manifest import load_dataset_manifest
from xau_trader.mt5_import import import_mt5_tick_file
from xau_trader.mt5_import_plan import parse_mt5_import_plan_bytes
from xau_trader.mt5_session_import import import_mt5_session_tick_file
from xau_trader.paper_execution_io import parse_paper_scenario
from xau_trader.session_calendar import parse_session_calendar_bytes


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class Mt5SessionImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw, self.plan, self.calendar = write_partial_session_import_fixture(self.root)
        self.output = self.root / "session-buckets.json"

    def _bind_plan_calendar(self, plan):
        raw = self.calendar.read_bytes()
        calendar = parse_session_calendar_bytes(raw)
        plan["session_calendar"]["artifact_sha256"] = _digest(raw)
        plan["session_calendar"]["content_fingerprint"] = calendar.fingerprint

    def run_import(self, **overrides):
        arguments = dict(plan_path=self.plan, calendar_path=self.calendar, output_json=self.output)
        arguments.update(overrides)
        return import_mt5_session_tick_file(self.raw, **arguments)

    def assert_no_artifacts(self):
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_partial_sessions_preserve_every_source_tick_without_strategy_or_orders(self):
        before = {path: path.read_bytes() for path in (self.raw, self.plan, self.calendar)}
        with patch("xau_trader.cli.run_diagnostic_replay") as replay:
            summary = self.run_import()
            replay.assert_not_called()
        report = json.loads(self.output.read_text())
        self.assertEqual(report["schema"], "xau_trader.mt5_session_bucket_import")
        self.assertEqual(report["version"], 1)
        self.assertEqual(summary["counts"]["input_ticks"], 10)
        self.assertEqual(summary["counts"]["preserved_ticks"], 10)
        self.assertEqual(summary["counts"]["partial_open_count"], 1)
        self.assertEqual(summary["counts"]["full_open_count"], 7)
        self.assertEqual(summary["counts"]["closed_count"], 4)
        self.assertEqual(summary["counts"]["excluded_ticks"], 0)
        self.assertEqual(report["orders_submitted"], 0)
        self.assertFalse(report["broker_connected"])
        self.assertFalse(report["strategy_replay_executed"])
        self.assertFalse(report["legacy_replay_admitted"])
        self.assertFalse(report["execution_admitted"])
        self.assertIsNone(report["execution"])
        self.assertIn("session_policy", report)
        policy = report["session_policy"]
        self.assertEqual(policy["counts"]["included_partial_m15"], 1)
        self.assertEqual(policy["counts"]["included_partial_h1"], 1)
        self.assertEqual(policy["counts"]["eligible_m15"], 8)
        self.assertEqual(policy["counts"]["eligible_h1"], 2)
        partial_m15 = [row for row in policy["m15"] if row["state"] == "partial_open"]
        partial_h1 = [row for row in policy["h1"] if row["state"] == "partial_open"]
        for rows in (partial_m15, partial_h1):
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["observation_status"], "observed")
            self.assertTrue(rows[0]["indicator_input_eligible"])
            self.assertTrue(rows[0]["signal_input_eligible"])
            self.assertEqual(rows[0]["available_at"], "2024-01-01T21:00:00Z")
        self.assertEqual(partial_m15[0]["scheduled_open_microseconds"], 13 * 60 * 1000000)
        self.assertEqual(partial_h1[0]["scheduled_open_microseconds"], 58 * 60 * 1000000)
        self.assertEqual(partial_m15[0]["observed_ohlc"]["bid_open"], 2003.0)
        self.assertEqual(partial_m15[0]["observed_ohlc"]["bid_close"], 2005.0)
        self.assertEqual(partial_h1[0]["observed_ohlc"]["bid_open"], 2000.0)
        self.assertEqual(partial_h1[0]["observed_ohlc"]["bid_close"], 2005.0)
        self.assertFalse(partial_m15[0]["next_nominal_entry_boundary_open"])
        self.assertFalse(policy["strategy_replay_executed"])
        self.assertIsNone(policy["execution"])
        self.assertEqual(report["format"]["availability_basis"], "synthetic")
        self.assertEqual(report["import_plan"]["source"]["source_symbol"], "XAUUSD_fixture")
        self.assertEqual([tick["source_row"] for tick in report["ticks"]], list(range(2, 12)))
        self.assertEqual(report["ticks"][4]["timestamp"], report["ticks"][5]["timestamp"])
        self.assertEqual([tick["bid"] for tick in report["ticks"]], [2000.0 + n for n in range(10)])
        self.assertEqual([tick["volume"] for tick in report["ticks"]], list(range(10)))
        self.assertTrue(all(tick["flags"] == 6 and tick["last"] == 0 for tick in report["ticks"]))
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_hashes_canonical_fingerprints_and_deterministic_artifact(self):
        self.run_import()
        report = json.loads(self.output.read_text())
        fingerprint = report.pop("fingerprint")
        self.assertEqual(fingerprint, _digest(_canonical(report)))
        self.assertEqual(report["import_plan_fingerprint"], _digest(_canonical(report["import_plan"])))
        self.assertEqual(report["calendar_fingerprint"], _digest(_canonical(report["calendar"])))
        policy = dict(report["session_policy"])
        policy_fingerprint = policy.pop("fingerprint")
        self.assertEqual(policy_fingerprint, _digest(_canonical(policy)))
        self.assertEqual(policy["policy_fingerprint"], _digest(_canonical(policy["semantics"])))
        self.assertEqual(policy["raw_sha256"], report["input_hashes"]["raw_export_sha256"])
        self.assertEqual(policy["calendar_fingerprint"], report["calendar_fingerprint"])
        for field, path in (("raw_export_sha256", self.raw), ("import_plan_file_sha256", self.plan),
                            ("calendar_file_sha256", self.calendar)):
            self.assertEqual(report["input_hashes"][field], _digest(path.read_bytes()))
        second = self.root / "same-evidence.json"
        self.run_import(output_json=second)
        self.assertEqual(self.output.read_bytes(), second.read_bytes())
        report["ticks"][0]["bid"] += 1
        self.assertNotEqual(fingerprint, _digest(_canonical(report)))

    def test_simulated_user_export_metadata_survives_as_historical_close_assumption(self):
        # The bytes still come solely from setUp's generated quotes. This test
        # simulates USER_EXPORT metadata; it is not actual broker market data.
        original_raw = self.raw.read_bytes()
        plan = json.loads(self.plan.read_text())
        plan["source"].update({
            "route": "local-fixture://simulated-user-export-metadata",
            "account_environment": "simulated demo metadata for generated fixture only",
            "acquisition_basis": "user_export",
            "source_symbol": "XAUUSD_user_export_fixture",
        })
        self.plan.write_bytes(_canonical(plan))
        self.run_import()
        report = json.loads(self.output.read_text())
        self.assertEqual(report["import_plan"]["source"], plan["source"])
        self.assertEqual(report["import_plan"]["provider"], plan["provider"])
        self.assertEqual(report["import_plan"]["rights"], plan["rights"])
        for layer in ("format", "aggregation", "session_policy"):
            with self.subTest(layer=layer):
                self.assertEqual(report[layer]["availability_basis"], "historical_close_assumption")
        self.assertEqual(report["input_hashes"]["raw_export_sha256"], _digest(original_raw))
        self.assertEqual(self.raw.read_bytes(), original_raw)
        self.assertEqual(report["session_policy"]["counts"]["included_partial_m15"], 1)
        self.assertEqual(report["session_policy"]["counts"]["included_partial_h1"], 1)
        self.assertFalse(report["legacy_replay_admitted"])
        self.assertFalse(report["execution_admitted"])
        self.assertFalse(report["strategy_replay_executed"])
        self.assertFalse(report["source_identity_independently_verified"])
        self.assertFalse(report["data_rights_verified"])
        self.assertIsNone(report["execution"])

    def test_generated_2201_reopening_includes_both_partial_edges(self):
        variant = self.root / "generated-reopening-variant"
        variant.mkdir()
        self.raw, self.plan, self.calendar = write_partial_session_import_fixture(
            variant, reopening_minute=1,
        )
        summary = self.run_import()
        report = json.loads(self.output.read_text())
        self.assertEqual(summary["counts"]["input_ticks"], 10)
        self.assertEqual(summary["counts"]["partial_open_count"], 2)
        self.assertEqual(summary["counts"]["full_open_count"], 6)
        self.assertEqual(summary["counts"]["closed_count"], 4)
        self.assertEqual(summary["counts"]["missing_open_data_count"], 0)
        policy = report["session_policy"]
        self.assertEqual(policy["counts"]["included_partial_m15"], 2)
        self.assertEqual(policy["counts"]["included_partial_h1"], 2)
        self.assertEqual([row["scheduled_open_microseconds"] for row in policy["m15"]
                          if row["state"] == "partial_open"], [780000000, 840000000])
        self.assertEqual([row["scheduled_open_microseconds"] for row in policy["h1"]
                          if row["state"] == "partial_open"], [3480000000, 3540000000])
        self.assertEqual(report["ticks"][6]["timestamp"], "2024-01-01T22:01:00.100000Z")
        self.assertEqual(report["import_plan"]["source"]["acquisition_basis"], "synthetic_generation")

    def test_fixture_reopening_option_is_explicit_and_bounded(self):
        for invalid in (True, 2, -1, "1", None):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, "reopening_minute"):
                write_partial_session_import_fixture(self.root / "not-created", reopening_minute=invalid)
        self.assertFalse((self.root / "not-created").exists())

    def test_declared_fixed_offset_is_applied_without_inventing_a_timezone(self):
        lines = self.raw.read_text().splitlines()
        converted = [lines[0]]
        for line in lines[1:]:
            fields = line.split("\t")
            shifted = datetime.strptime(fields[0] + " " + fields[1], "%Y.%m.%d %H:%M:%S.%f") + timedelta(hours=2)
            fields[0] = shifted.strftime("%Y.%m.%d")
            fields[1] = shifted.strftime("%H:%M:%S.%f")[:-3]
            converted.append("\t".join(fields))
        self.raw.write_bytes(("\n".join(converted) + "\n").encode("utf-8"))
        plan = json.loads(self.plan.read_text())
        plan["raw_sha256"] = _digest(self.raw.read_bytes())
        plan["timestamp"]["utc_offset_minutes"] = 120
        plan["timestamp"]["evidence_reference"] = "local-fixture://fixed-plus-two-hour-test-clock"
        self.plan.write_bytes(_canonical(plan))
        self.run_import()
        report = json.loads(self.output.read_text())
        self.assertEqual(report["ticks"][0]["timestamp"], "2024-01-01T20:00:00.100000Z")
        self.assertEqual(report["ticks"][-1]["timestamp"], "2024-01-01T22:45:00.100000Z")
        self.assertEqual(report["format"]["timestamp_offset_minutes"], 120)

    def test_missing_open_bucket_is_reported_not_filled_or_relabelled_closed(self):
        raw = self.raw.read_bytes()
        self.raw.write_bytes(b"\n".join(line for line in raw.split(b"\n") if b"20:15:00.100" not in line))
        plan = json.loads(self.plan.read_text())
        plan["raw_sha256"] = _digest(self.raw.read_bytes())
        self.plan.write_bytes(_canonical(plan))
        summary = self.run_import()
        report = json.loads(self.output.read_text())
        self.assertEqual(summary["counts"]["input_ticks"], 9)
        self.assertEqual(summary["counts"]["missing_open_data_count"], 1)
        bucket = report["aggregation"]["buckets"][1]
        self.assertEqual(bucket["state"], "full_open")
        self.assertEqual(bucket["observation_status"], "missing_open_data")
        self.assertIsNone(bucket["observed_ohlc"])
        self.assertEqual(bucket["tick_count"], 0)

    def test_all_calendar_identity_mismatches_fail_without_publication(self):
        original = self.plan.read_bytes()
        mutations = (
            ("provider", "name", "Different synthetic provider", "provider_name"),
            ("provider", "legal_entity", "Different synthetic entity", "provider_legal_entity"),
            ("instrument", "product_form", "different synthetic contract", "product_form"),
            ("session_calendar", "id", "different-calendar", "calendar id"),
            ("session_calendar", "version", "different-revision", "calendar revision"),
            ("session_calendar", "artifact_sha256", "a" * 64, "artifact_sha256"),
            ("session_calendar", "content_fingerprint", "a" * 64, "content_fingerprint"),
        )
        for section, field, value, error in mutations:
            with self.subTest(field=field):
                plan = json.loads(original)
                plan[section][field] = value
                self.plan.write_bytes(_canonical(plan))
                with self.assertRaisesRegex(ValueError, error):
                    self.run_import()
                self.assert_no_artifacts()
        self.plan.write_bytes(original)

    def test_calendar_coverage_and_retrieval_time_mismatches(self):
        original_calendar, original_plan = self.calendar.read_bytes(), self.plan.read_bytes()
        for field, value, error in (("coverage_start", "2024-01-01T20:30:00Z", "coverage"),
                                    ("coverage_end", "2024-01-01T22:30:00Z", "coverage"),
                                    ("retrieved_at", "2024-01-02T01:00:00Z", "retrieved_at")):
            with self.subTest(field=field):
                calendar, plan = json.loads(original_calendar), json.loads(original_plan)
                calendar[field] = value
                self.calendar.write_bytes(_canonical(calendar))
                self._bind_plan_calendar(plan)
                self.plan.write_bytes(_canonical(plan))
                with self.assertRaisesRegex(ValueError, error):
                    self.run_import()
                self.assert_no_artifacts()

    def test_raw_and_calendar_byte_changes_fail(self):
        original_raw = self.raw.read_bytes()
        self.raw.write_bytes(original_raw.replace(b"2000.0", b"1999.0", 1))
        with self.assertRaisesRegex(ValueError, "raw export hash"):
            self.run_import()
        self.assert_no_artifacts()
        self.raw.write_bytes(original_raw)
        self.calendar.write_bytes(self.calendar.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "artifact_sha256"):
            self.run_import()
        self.assert_no_artifacts()

    def test_each_input_change_during_policy_evaluation_prevents_publication(self):
        from xau_trader.session_bucket_policy import evaluate_session_bucket_policy

        for path in (self.raw, self.plan, self.calendar):
            with self.subTest(path=path):
                original = path.read_bytes()

                def change(aggregation):
                    result = evaluate_session_bucket_policy(aggregation)
                    path.write_bytes(original + b"\n")
                    return result

                with patch("xau_trader.session_bucket_policy.evaluate_session_bucket_policy", side_effect=change):
                    with self.assertRaisesRegex(ValueError, "inputs changed"):
                        self.run_import()
                self.assert_no_artifacts()
                path.write_bytes(original)

    def test_transient_plan_path_change_does_not_change_parsed_snapshot(self):
        original = self.plan.read_bytes()

        def transient(raw):
            self.plan.write_bytes(original.replace(b"XAUUSD_fixture", b"OTHER_FIXTURE"))
            try:
                return parse_mt5_import_plan_bytes(raw)
            finally:
                self.plan.write_bytes(original)

        with patch("xau_trader.mt5_session_import.parse_mt5_import_plan_bytes", side_effect=transient):
            self.run_import()
        report = json.loads(self.output.read_text())
        self.assertEqual(report["import_plan"]["source"]["source_symbol"], "XAUUSD_fixture")
        self.assertEqual(report["input_hashes"]["import_plan_file_sha256"], _digest(original))

    def test_bounded_inputs_before_parse_or_publication(self):
        for constant in ("MAX_RAW_BYTES", "MAX_PLAN_BYTES", "MAX_CALENDAR_BYTES"):
            with self.subTest(constant=constant), patch("xau_trader.mt5_session_import." + constant, 1):
                with self.assertRaisesRegex(ValueError, "byte limit"):
                    self.run_import()
                self.assert_no_artifacts()

    def test_nonregular_input_rejected(self):
        fifo = self.root / "ticks.fifo"
        os.mkfifo(fifo)
        for path in (self.root, fifo):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "regular files"):
                import_mt5_session_tick_file(path, plan_path=self.plan, calendar_path=self.calendar,
                                            output_json=self.output)
        self.assert_no_artifacts()

    def test_output_direct_symlink_hardlink_and_parent_aliases_are_rejected(self):
        for source in (self.raw, self.plan, self.calendar):
            before = source.read_bytes()
            with self.assertRaisesRegex(ValueError, "cannot replace an input"):
                self.run_import(output_json=source)
            alias = self.root / (source.name + ".alias")
            alias.symlink_to(source)
            with self.assertRaisesRegex(ValueError, "cannot replace an input"):
                self.run_import(output_json=alias)
            hardlink = self.root / (source.name + ".hardlink")
            os.link(source, hardlink)
            with self.assertRaises(FileExistsError):
                self.run_import(output_json=hardlink)
            self.assertEqual(source.read_bytes(), before)
        parent_alias = self.root / "same-directory"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "cannot replace an input"):
            self.run_import(output_json=parent_alias / self.raw.name)
        self.assert_no_artifacts()

    def test_existing_or_dangling_output_is_never_overwritten(self):
        self.output.write_bytes(b"Keep this existing artifact")
        with self.assertRaises(FileExistsError):
            self.run_import()
        self.assertEqual(self.output.read_bytes(), b"Keep this existing artifact")
        self.output.unlink()
        target = self.root / "missing.json"
        self.output.symlink_to(target)
        with self.assertRaises(FileExistsError):
            self.run_import()
        self.assertFalse(target.exists())
        self.assertTrue(self.output.is_symlink())

    def test_exclusive_publication_preserves_racing_writer_and_cleans_staging(self):
        link = os.link

        def race(source, target):
            self.output.write_bytes(b"Another writer owns this")
            return link(source, target)

        with patch("xau_trader.mt5_import.os.link", side_effect=race):
            with self.assertRaises(FileExistsError):
                self.run_import()
        self.assertEqual(self.output.read_bytes(), b"Another writer owns this")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_output_alias_created_during_evaluation_cannot_replace_source(self):
        from xau_trader.session_bucket_policy import evaluate_session_bucket_policy

        original = self.raw.read_bytes()

        def alias_after_evaluation(aggregation):
            result = evaluate_session_bucket_policy(aggregation)
            self.output.symlink_to(self.raw)
            return result

        with patch("xau_trader.session_bucket_policy.evaluate_session_bucket_policy",
                   side_effect=alias_after_evaluation):
            with self.assertRaisesRegex(ValueError, "cannot replace an input"):
                self.run_import()
        self.assertEqual(self.raw.read_bytes(), original)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_tick_at_closed_boundary_is_rejected_not_dropped(self):
        lines = self.raw.read_text().splitlines()
        lines.insert(7, "2024.01.01\t20:58:00.000\t2005.0\t2005.5\t0\t0\t6")
        self.raw.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
        plan = json.loads(self.plan.read_text())
        plan["raw_sha256"] = _digest(self.raw.read_bytes())
        self.plan.write_bytes(_canonical(plan))
        with self.assertRaisesRegex(ValueError, "closed|closure"):
            self.run_import()
        self.assert_no_artifacts()

    def test_legacy_import_remains_fail_closed_and_bundle_is_not_a_replay_or_paper_input(self):
        with self.assertRaisesRegex(ValueError, "whole UTC hour"):
            import_mt5_tick_file(self.raw, plan_path=self.plan, calendar_path=self.calendar,
                                 output_csv=self.root / "legacy.csv",
                                 output_manifest=self.root / "legacy.manifest.json",
                                 report_json=self.root / "legacy.report.json")
        self.run_import()
        with self.assertRaises(ValueError):
            load_dataset_manifest(self.output)
        with self.assertRaises(ValueError):
            parse_paper_scenario(self.output.read_bytes())
        self.assertFalse((self.root / "legacy.csv").exists())

    def test_cli_prints_compact_counts_not_source_quotes(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(("import-mt5-session-ticks", str(self.raw), "--plan", str(self.plan),
                         "--calendar", str(self.calendar), "--output-json", str(self.output)))
        self.assertEqual(code, 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["counts"]["input_ticks"], 10)
        self.assertEqual(result["counts"]["partial_open_count"], 1)
        self.assertEqual(result["counts"]["missing_open_data_count"], 0)
        self.assertEqual(result["counts"]["closed_count"], 4)
        artifact = json.loads(self.output.read_text())
        self.assertEqual(result["counts"], artifact["counts"])
        self.assertEqual(result["policy_version"], artifact["session_policy"]["semantics"]["version"])
        self.assertEqual(result["policy_scope"], artifact["session_policy"]["scope"])
        self.assertEqual(result["policy_counts"], artifact["session_policy"]["counts"])
        self.assertFalse(result["missing_open_data_blocks_replay"])
        self.assertNotIn("ticks", result)
        self.assertNotIn("aggregation", result)
        self.assertFalse(result["execution_admitted"])

    def test_cli_reports_missing_and_partial_counts_without_source_payload(self):
        lines = self.raw.read_bytes().split(b"\n")
        self.raw.write_bytes(b"\n".join(line for line in lines if b"20:15:00.100" not in line))
        plan = json.loads(self.plan.read_text())
        plan["raw_sha256"] = _digest(self.raw.read_bytes())
        self.plan.write_bytes(_canonical(plan))
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(("import-mt5-session-ticks", str(self.raw), "--plan", str(self.plan),
                         "--calendar", str(self.calendar), "--output-json", str(self.output)))
        self.assertEqual(code, 0)
        result = json.loads(stream.getvalue())
        artifact = json.loads(self.output.read_text())
        self.assertEqual(result["counts"], artifact["counts"])
        self.assertEqual(result["counts"]["input_ticks"], 9)
        self.assertEqual(result["counts"]["preserved_ticks"], 9)
        self.assertEqual(result["counts"]["bucket_count"], 12)
        self.assertEqual(result["counts"]["partial_open_count"], 1)
        self.assertEqual(result["counts"]["full_open_count"], 7)
        self.assertEqual(result["counts"]["missing_open_data_count"], 1)
        self.assertEqual(result["counts"]["closed_count"], 4)
        self.assertEqual(result["counts"]["excluded_ticks"], 0)
        self.assertTrue(artifact["session_policy"]["missing_open_data_blocks_replay"])
        self.assertTrue(result["missing_open_data_blocks_replay"])
        self.assertEqual(result["policy_counts"], artifact["session_policy"]["counts"])
        self.assertEqual(result["policy_counts"]["included_partial_m15"], 1)
        self.assertEqual(result["policy_counts"]["included_partial_h1"], 0)
        self.assertNotIn("ticks", result)
        self.assertNotIn("aggregation", result)
        self.assertNotIn("session_policy", result)

    def test_cli_invalid_plan_publishes_nothing(self):
        self.plan.write_bytes(b"{}")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
            main(("import-mt5-session-ticks", str(self.raw), "--plan", str(self.plan),
                  "--calendar", str(self.calendar), "--output-json", str(self.output)))
        self.assertEqual(failure.exception.code, 2)
        self.assert_no_artifacts()


if __name__ == "__main__":
    unittest.main()
