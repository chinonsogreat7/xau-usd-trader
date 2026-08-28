import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from xau_trader.cli import main
from xau_trader.data import read_quote_bars_csv, sha256_file
from xau_trader.dataset_manifest import load_dataset_manifest, validate_dataset_binding


class DiagnosticReplayCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.market_csv = self.root / "m15.csv"
        self.manifest_json = self.root / "m15.manifest.json"
        self._export_fixture()

    def _export_fixture(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(
                (
                    "export-demo-replay-data",
                    str(self.market_csv),
                    "--manifest",
                    str(self.manifest_json),
                    "--bars",
                    "100",
                )
            )
        self.assertEqual(status, 0)
        return json.loads(output.getvalue())

    def _replay(self, trace_json, decisions_csv, *extra):
        output = io.StringIO()
        arguments = (
            "replay-diagnostic",
            str(self.market_csv),
            "--manifest",
            str(self.manifest_json),
            "--trace-json",
            str(trace_json),
            "--decisions-csv",
            str(decisions_csv),
        ) + tuple(extra)
        with redirect_stdout(output):
            status = main(arguments)
        self.assertEqual(status, 0)
        return json.loads(output.getvalue())

    def test_fixture_manifest_binds_exact_exported_csv(self):
        bars = read_quote_bars_csv(self.market_csv)
        digest = sha256_file(self.market_csv)
        manifest = load_dataset_manifest(self.manifest_json)
        binding = validate_dataset_binding(
            manifest,
            bars,
            raw_sha256=digest,
            normalized_csv_sha256=digest,
        )

        self.assertEqual(binding.row_count, 100)
        self.assertEqual(binding.known_gap_count, 0)
        self.assertEqual(binding.manifest_fingerprint, manifest.fingerprint)

    def test_replay_writes_deterministic_json_and_one_csv_row_per_m15_bar(self):
        first_json = self.root / "first.json"
        first_csv = self.root / "first.csv"
        second_json = self.root / "second.json"
        second_csv = self.root / "second.csv"

        first = self._replay(first_json, first_csv)
        second = self._replay(second_json, second_csv)

        self.assertTrue(first["diagnostic_only"])
        self.assertIsNone(first["execution"])
        self.assertEqual(first["status"], "diagnostic_replay_complete")
        self.assertEqual(first["counts"]["m15_bars"], 100)
        self.assertEqual(first["counts"]["h1_bars"], 25)
        self.assertEqual(first["counts"]["decisions"], 100)
        self.assertEqual(first["counts"]["pre_roll_decisions"], 96)
        self.assertEqual(first["counts"]["post_pre_roll_decisions"], 4)
        self.assertEqual(
            first["counts"]["all_buy"]
            + first["counts"]["all_sell"]
            + first["counts"]["all_no_trade"],
            100,
        )
        self.assertEqual(
            first["counts"]["post_pre_roll_buy"]
            + first["counts"]["post_pre_roll_sell"]
            + first["counts"]["post_pre_roll_no_trade"],
            4,
        )
        self.assertEqual(
            first["replay"]["readiness"]["history_boundary"],
            "empty_state_at_dataset_start",
        )
        self.assertEqual(first["replay"], second["replay"])
        self.assertEqual(first_json.read_bytes(), second_json.read_bytes())
        self.assertEqual(first_csv.read_bytes(), second_csv.read_bytes())
        self.assertEqual(len(first_csv.read_text(encoding="utf-8").splitlines()), 101)

        trace = json.loads(first_json.read_text(encoding="utf-8"))
        self.assertTrue(trace["diagnostic_only"])
        self.assertIsNone(trace["execution"])
        self.assertEqual(trace["counts"]["decisions"], 100)
        self.assertEqual(len(trace["decisions"]), 100)

    def test_manifest_hash_mismatch_leaves_no_trace_artifacts(self):
        payload = json.loads(self.manifest_json.read_text(encoding="utf-8"))
        payload["hashes"]["normalized_csv_sha256"] = "0" * 64
        self.manifest_json.write_text(json.dumps(payload), encoding="utf-8")
        trace_json = self.root / "must-not-exist.json"
        decisions_csv = self.root / "must-not-exist.csv"

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self._replay(trace_json, decisions_csv)

        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(trace_json.exists())
        self.assertFalse(decisions_csv.exists())

    def test_collision_refuses_both_outputs_without_overwriting_existing_file(self):
        trace_json = self.root / "existing.json"
        decisions_csv = self.root / "must-not-exist.csv"
        trace_json.write_bytes(b"preserve-me")

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self._replay(trace_json, decisions_csv)

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(trace_json.read_bytes(), b"preserve-me")
        self.assertFalse(decisions_csv.exists())

    def test_outputs_cannot_alias_an_input(self):
        decisions_csv = self.root / "must-not-exist.csv"
        before = self.market_csv.read_bytes()

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                self._replay(self.market_csv, decisions_csv)

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.market_csv.read_bytes(), before)
        self.assertFalse(decisions_csv.exists())

    def test_fixture_pair_is_not_partially_created_when_manifest_stage_fails(self):
        market_csv = self.root / "failed-fixture.csv"
        manifest_json = self.root / "failed-fixture.manifest.json"

        with patch(
            "xau_trader.cli._synthetic_m15_manifest",
            side_effect=ValueError("injected manifest failure"),
        ):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(
                        (
                            "export-demo-replay-data",
                            str(market_csv),
                            "--manifest",
                            str(manifest_json),
                            "--bars",
                            "100",
                        )
                    )

        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(market_csv.exists())
        self.assertFalse(manifest_json.exists())


if __name__ == "__main__":
    unittest.main()
