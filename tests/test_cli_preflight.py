import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from xau_trader.cli import main
from xau_trader.data import generate_demo_m15_bars, sha256_file, write_quote_bars_csv


class ReplayPreflightCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.csv = self.root / "m15.csv"
        self.manifest = self.root / "m15.manifest.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main((
                "export-demo-replay-data", str(self.csv),
                "--manifest", str(self.manifest), "--bars", "100",
            )), 0)

    def inspect(self, *extra):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(("check-replay-data", str(self.csv)) + extra)
        return status, json.loads(output.getvalue())

    def test_complete_inputs_pass_without_running_strategy_or_creating_files(self):
        before = {path: path.read_bytes() for path in self.root.iterdir()}
        with patch("xau_trader.cli.run_diagnostic_replay") as replay:
            status, report = self.inspect("--manifest", str(self.manifest))
        self.assertEqual(status, 0)
        self.assertTrue(report["ready_for_diagnostic_replay"])
        self.assertTrue(report["technical"]["compatible"])
        self.assertEqual(report["provenance"]["status"], "binding_valid")
        self.assertFalse(report["provenance"]["rights_claims_independently_verified"])
        replay.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.iterdir()})

    def test_missing_manifest_reports_technical_result_without_admitting_data(self):
        status, report = self.inspect()
        self.assertEqual(status, 1)
        self.assertTrue(report["technical"]["compatible"])
        self.assertFalse(report["ready_for_diagnostic_replay"])
        self.assertEqual(report["provenance"]["status"], "missing_manifest")

    def test_declared_daily_break_is_bound_but_not_replay_compatible(self):
        original = generate_demo_m15_bars(184)
        gap = timedelta(hours=1)
        bars = original[:92] + tuple(replace(
            bar, start_time=bar.start_time + gap,
            timestamp=bar.timestamp + gap, available_at=bar.available_at + gap,
        ) for bar in original[92:])
        write_quote_bars_csv(self.csv, bars)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["coverage"] = {
            "start": bars[0].start_time.isoformat(),
            "end": bars[-1].timestamp.isoformat(),
            "row_count": len(bars),
            "known_gaps": [{
                "start": bars[91].timestamp.isoformat(),
                "end": bars[92].start_time.isoformat(),
                "reason": "Synthetic scheduled-break regression fixture",
            }],
        }
        digest = sha256_file(self.csv)
        manifest["hashes"] = {"raw_sha256": digest, "normalized_csv_sha256": digest}
        manifest["source"]["retrieved_at"] = bars[-1].timestamp.isoformat()
        manifest["source"]["request_parameters"]["bar_count"] = str(len(bars))
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        status, report = self.inspect("--manifest", str(self.manifest))
        self.assertEqual(status, 1)
        self.assertFalse(report["ready_for_diagnostic_replay"])
        self.assertEqual(report["provenance"]["status"], "binding_valid")
        self.assertEqual(report["provenance"]["known_gap_count"], 1)
        self.assertEqual(report["technical"]["row_count"], 184)
        self.assertFalse(report["technical"]["compatible"])
        self.assertEqual([
            segment["complete_hour_row_count"]
            for segment in report["technical"]["contiguous_segments"]
        ], [92, 92])

    def test_exact_raw_artifact_is_required_when_manifest_hash_differs(self):
        raw = self.root / "source.txt"
        raw.write_bytes(b"synthetic raw source representation")
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["hashes"]["raw_sha256"] = sha256_file(raw)
        self.manifest.write_text(json.dumps(payload), encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self.inspect("--manifest", str(self.manifest))
        self.assertEqual(raised.exception.code, 2)
        status, report = self.inspect(
            "--manifest", str(self.manifest), "--raw-data", str(raw),
        )
        self.assertEqual(status, 0)
        self.assertEqual(report["provenance"]["raw_sha256"], sha256_file(raw))

    def test_raw_data_without_manifest_is_an_argument_error(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self.inspect("--raw-data", str(self.csv))
        self.assertEqual(raised.exception.code, 2)

    def test_csv_change_during_read_is_rejected(self):
        from xau_trader.data import read_quote_bars_csv

        def changing_read(path):
            bars = read_quote_bars_csv(path)
            Path(path).write_bytes(Path(path).read_bytes() + b"\n")
            return bars

        with patch("xau_trader.cli.read_quote_bars_csv", side_effect=changing_read):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.inspect("--manifest", str(self.manifest))
        self.assertEqual(raised.exception.code, 2)

    def test_raw_alias_retargeted_during_read_is_rejected(self):
        from xau_trader.data import read_quote_bars_csv

        raw_alias = self.root / "raw-alias.csv"
        raw_alias.symlink_to(self.csv)
        other_raw = self.root / "other-raw.txt"
        other_raw.write_bytes(b"different synthetic source")

        def changing_read(path):
            bars = read_quote_bars_csv(path)
            raw_alias.unlink()
            raw_alias.symlink_to(other_raw)
            return bars

        error = io.StringIO()
        with patch("xau_trader.cli.read_quote_bars_csv", side_effect=changing_read):
            with redirect_stderr(error):
                with self.assertRaises(SystemExit) as raised:
                    self.inspect(
                        "--manifest", str(self.manifest), "--raw-data", str(raw_alias),
                    )
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("raw data changed", error.getvalue())


if __name__ == "__main__":
    unittest.main()
