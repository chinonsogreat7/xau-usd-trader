from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from structural_exit_fixture import structural_exit_m15_bars
from session_feature_fixture import write_session_fixture
from xau_trader.cli import _synthetic_m15_manifest, main
from xau_trader.data import sha256_file, write_quote_bars_csv
from xau_trader.paper_execution_io import (
    load_paper_scenario, synthetic_execution_scenario, write_new_json,
)


PRICE_FIELDS = tuple(side + "_" + part for side in ("bid", "ask")
                     for part in ("open", "high", "low", "close"))


def coherent_scenario(bars):
    """An explicitly supplied synthetic OHLC path, never claimed as real ticks."""
    scenario = synthetic_execution_scenario()
    scenario["name"] = "Synthetic strategy integration: supplied open-low-high-close path"
    scenario["intents"] = []
    scenario["quotes"] = []
    for bar in bars:
        points = (
            ("open", bar.start_time, bar.start_time + timedelta(microseconds=1)),
            ("low", bar.start_time + timedelta(minutes=5), bar.start_time + timedelta(minutes=5)),
            ("high", bar.start_time + timedelta(minutes=10), bar.start_time + timedelta(minutes=10)),
            ("close", bar.timestamp - timedelta(microseconds=1), bar.timestamp),
        )
        for part, timestamp, available_at in points:
            scenario["quotes"].append({
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "available_at": available_at.isoformat().replace("+00:00", "Z"),
                "bid": format(getattr(bar, "bid_" + part), ".2f"),
                "ask": format(getattr(bar, "ask_" + part), ".2f"),
            })
    return scenario


class StrategyPaperCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.csv = self.root / "synthetic-m15.csv"
        self.manifest = self.root / "synthetic-m15.manifest.json"
        self.scenario = self.root / "synthetic-quotes.json"
        self.output = self.root / "report.json"
        self.bars = tuple(replace(bar, **{
            name: round(getattr(bar, name), 2) for name in PRICE_FIELDS
        }) for bar in structural_exit_m15_bars())
        write_quote_bars_csv(self.csv, self.bars)
        manifest = _synthetic_m15_manifest(self.bars, sha256_file(self.csv))
        self.manifest.write_text(manifest.canonical_json + "\n", encoding="utf-8")
        self.scenario_payload = coherent_scenario(self.bars)
        self.scenario.write_text(json.dumps(self.scenario_payload), encoding="utf-8")

    def run_cli(self, *extra, output=None):
        stream = io.StringIO()
        arguments = (
            "replay-strategy-paper", str(self.csv),
            "--manifest", str(self.manifest), "--quote-scenario", str(self.scenario),
            "--m15-left-wing", "3", "--m15-right-wing", "3", "--stop-atr-multiple", "0.15",
            "--report-json", str(self.output if output is None else output),
        ) + extra
        with redirect_stdout(stream):
            status = main(arguments)
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
        self.assertEqual(list(self.root.glob(".paper-*.tmp")), [])

    def edit_manifest(self, change):
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        change(value)
        self.manifest.write_text(json.dumps(value), encoding="utf-8")

    def test_genuine_strategy_is_bound_and_repeatable_without_profit_stdout(self):
        protected = {path: path.read_bytes() for path in (self.csv, self.manifest, self.scenario)}
        status, summary = self.run_cli()
        self.assertEqual(status, 0)
        self.assertEqual(summary["status"], "synthetic_strategy_paper_replay_complete")
        self.assertEqual(summary["counts"]["decisions"], 224)
        self.assertEqual(summary["counts"]["pre_roll"], 96)
        self.assertEqual(summary["counts"]["generated_intents"], 1)
        self.assertEqual(summary["counts"]["quotes"], 896)
        self.assertFalse(summary["broker_connected"])
        self.assertFalse(summary["promotion_eligible"])
        self.assertEqual(summary["real_orders_submitted"], 0)
        self.assertNotIn("net_pnl", summary)
        self.assertNotIn("final_balance", summary)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["fingerprint"], summary["fingerprint"])
        self.assertEqual(report["quote_scenario"]["input_sha256"], sha256_file(self.scenario))
        self.assertEqual(report["quote_scenario"]["inputs"], self.scenario_payload)
        self.assertEqual(report["dataset"]["manifest_input_sha256"], sha256_file(self.manifest))
        self.assertEqual(report["dataset"]["normalized_csv_sha256"], sha256_file(self.csv))
        self.assertEqual(report["strategy_execution"]["dataset_fingerprint"], report["dataset"]["manifest_fingerprint"])
        self.assertTrue(report["strategy_execution"]["strategy_generated"])
        self.assertFalse(report["strategy_execution"]["source_risk_policy_complete"])
        self.assertEqual(protected, {path: path.read_bytes() for path in protected})
        other = self.root / "same-inputs.json"
        self.run_cli(output=other)
        self.assertEqual(self.output.read_bytes(), other.read_bytes())

    def test_fingerprint_binds_all_inputs_and_nested_execution(self):
        self.run_cli()
        report = json.loads(self.output.read_text(encoding="utf-8"))
        fingerprint = report.pop("fingerprint")
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(fingerprint, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        execution = report["strategy_execution"]
        nested_fingerprint = execution.pop("fingerprint")
        nested = json.dumps(execution, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(nested_fingerprint, hashlib.sha256(nested.encode("utf-8")).hexdigest())

    def test_v2_quote_scenario_binds_and_enables_loss_limits(self):
        self.scenario_payload.update(version=2, loss_limits={
            "max_losses_per_day": 2, "max_weekly_drawdown_fraction": "0.03",
        })
        self.scenario.write_text(json.dumps(self.scenario_payload), encoding="utf-8")
        _, summary = self.run_cli()
        self.assertTrue(summary["loss_limits_configured"])
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertTrue(report["loss_limits"]["enabled"])
        self.assertEqual(report["loss_limits"]["max_losses_per_day"], 2)
        self.assertEqual(report["loss_limits"]["max_weekly_drawdown_fraction"], "0.03")
        self.assertEqual(report["strategy_execution"]["execution"]["loss_limits"], report["loss_limits"])
        self.assertFalse(report["source_risk_policy_complete"])
        self.assertFalse(report["broker_connected"])

    def test_whitespace_changes_exact_snapshot_hash_not_core_result(self):
        self.run_cli()
        first = json.loads(self.output.read_text(encoding="utf-8"))
        self.scenario.write_text(json.dumps(self.scenario_payload, indent=2), encoding="utf-8")
        second_path = self.root / "different-snapshot.json"
        self.run_cli(output=second_path)
        second = json.loads(second_path.read_text(encoding="utf-8"))
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["quote_scenario"]["input_sha256"], second["quote_scenario"]["input_sha256"])
        self.assertEqual(first["quote_scenario"]["canonical_sha256"], second["quote_scenario"]["canonical_sha256"])
        self.assertEqual(first["strategy_execution"], second["strategy_execution"])

    def test_scripted_intents_are_rejected_not_silently_discarded(self):
        self.scenario_payload["intents"] = synthetic_execution_scenario()["intents"]
        self.scenario.write_text(json.dumps(self.scenario_payload), encoding="utf-8")
        with patch("xau_trader.cli.run_diagnostic_replay") as replay:
            self.assert_rejected(message="intents must be empty")
        replay.assert_not_called()

    def test_real_data_manifest_is_rejected_before_replay(self):
        self.edit_manifest(lambda value: value["source"].update(acquisition_basis="user_export"))
        with patch("xau_trader.cli.run_diagnostic_replay") as replay:
            self.assert_rejected(message="requires a synthetic_generation manifest")
        replay.assert_not_called()

    def test_quote_scenario_must_itself_be_synthetic(self):
        for basis in ("historical", "live"):
            with self.subTest(basis=basis):
                self.scenario_payload["data_basis"] = basis
                self.scenario.write_text(json.dumps(self.scenario_payload), encoding="utf-8")
                self.assert_rejected(message="synthetic fixtures only")

    def test_v2_is_explicitly_not_supported(self):
        session_root = self.root / "session"
        session_root.mkdir()
        self.csv, self.manifest, _ = write_session_fixture(session_root)
        self.assert_rejected(message="manifest v1 only")

    def test_manifest_csv_hash_mismatch_is_rejected(self):
        self.edit_manifest(lambda value: value["hashes"].update(normalized_csv_sha256="0" * 64))
        self.assert_rejected(message="normalized_csv_sha256")

    def test_separate_raw_source_is_explicitly_required_and_bound(self):
        raw = self.root / "raw-source.txt"
        raw.write_bytes(b"A distinct synthetic generation artifact")
        self.edit_manifest(lambda value: value["hashes"].update(raw_sha256=sha256_file(raw)))
        self.assert_rejected(message="raw_sha256")
        status, _ = self.run_cli("--raw-data", str(raw))
        self.assertEqual(status, 0)
        report = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(report["dataset"]["raw_sha256"], sha256_file(raw))

    def test_incoherent_quote_tape_is_rejected_without_artifacts(self):
        self.scenario_payload["quotes"][1]["bid"] = "1900.00"
        self.scenario.write_text(json.dumps(self.scenario_payload), encoding="utf-8")
        self.assert_rejected(message="does not reconstruct")

    def test_all_structural_choices_are_required_and_validated(self):
        for field, value in (("--m15-left-wing", "0"), ("--m15-right-wing", "-1"),
                             ("--stop-atr-multiple", "NaN"), ("--stop-atr-multiple", "-0.15"),
                             ("--stop-atr-multiple", "not-a-decimal")):
            with self.subTest(field=field, value=value):
                self.assert_rejected(field, value)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(("replay-strategy-paper", str(self.csv)))
        self.assertEqual(raised.exception.code, 2)

    def test_existing_output_input_aliases_and_links_are_preserved(self):
        existing = self.root / "existing.json"
        existing.write_bytes(b"Keep evidence")
        self.assert_rejected(output=existing, message="already exists")
        self.assertEqual(existing.read_bytes(), b"Keep evidence")
        raw = self.root / "raw-source.txt"
        raw.write_bytes(b"Keep raw source")
        for path in (self.csv, self.manifest, self.scenario, raw):
            with self.subTest(path=path):
                before = path.read_bytes()
                self.assert_rejected("--raw-data", str(raw), output=path, message="cannot replace an input")
                symlink = self.root / (path.name + ".symlink")
                symlink.symlink_to(path)
                self.assert_rejected("--raw-data", str(raw), output=symlink, message="cannot replace an input")
                hardlink = self.root / (path.name + ".hardlink")
                os.link(path, hardlink)
                self.assert_rejected("--raw-data", str(raw), output=hardlink, message="already exists")
                self.assertEqual(path.read_bytes(), before)
        dangling = self.root / "dangling.json"
        missing = self.root / "missing.json"
        dangling.symlink_to(missing)
        self.assert_rejected(output=dangling, message="already exists")
        self.assertFalse(missing.exists())

    def test_changed_input_during_scenario_load_is_rejected(self):
        def changing_load(path):
            result = load_paper_scenario(path)
            self.csv.write_bytes(self.csv.read_bytes() + b"\n")
            return result
        with patch("xau_trader.cli.load_paper_scenario", side_effect=changing_load):
            self.assert_rejected(message="inputs changed")

    def test_late_publication_collision_does_not_overwrite(self):
        target = self.root / "late-collision.json"
        def collide(path, payload):
            Path(path).write_bytes(b"Concurrent evidence")
            write_new_json(path, payload)
        with patch("xau_trader.cli.write_new_json", side_effect=collide):
            self.assert_rejected(output=target)
        self.assertEqual(target.read_bytes(), b"Concurrent evidence")


if __name__ == "__main__":
    unittest.main()
