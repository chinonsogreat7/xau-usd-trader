import copy
from decimal import Decimal, localcontext
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from xau_trader.cli import main
from xau_trader.paper_execution_io import (
    MAX_BYTES, load_paper_scenario, parse_paper_scenario, run_paper_scenario,
    synthetic_execution_scenario, write_new_json,
)


def encode(value):
    return json.dumps(value).encode("utf-8")


class PaperScenarioTests(unittest.TestCase):
    def setUp(self):
        self.fixture = synthetic_execution_scenario()

    def test_known_round_trip_accounting_and_guard_cases(self):
        raw = encode(self.fixture)
        scenario = parse_paper_scenario(raw)
        report = run_paper_scenario(scenario)
        self.assertEqual(scenario.input_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(report["scenario"]["inputs"], self.fixture)
        self.assertFalse(report["scenario"]["strategy_generated"])
        self.assertFalse(report["broker_connected"])
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(report["real_orders_submitted"], 0)
        self.assertEqual(report["counts"]["trades"], 2)
        self.assertEqual(report["counts"]["fills"], 4)
        self.assertEqual(report["counts"]["rejected_intents"], 3)
        self.assertEqual(report["counts"]["cancelled_intents"], 1)
        self.assertEqual([Decimal(t["net_pnl"]) for t in report["trades"]], [Decimal("9.92"), Decimal("-6.88")])
        self.assertEqual(Decimal(report["final_balance"]), Decimal("503.04"))
        self.assertEqual(Decimal(report["total_commission"]), Decimal("0.16"))
        self.assertEqual(report["trades"][1]["exit_reason"], "stop_loss")
        self.assertGreater(abs(Decimal(report["trades"][1]["net_pnl"])), Decimal(report["trades"][1]["modeled_stop_risk"]))

    def test_repeat_is_deterministic_and_independent_of_decimal_precision(self):
        scenario = parse_paper_scenario(encode(self.fixture))
        first = run_paper_scenario(scenario)
        with localcontext() as context:
            context.prec = 6
            second = run_paper_scenario(scenario)
        self.assertEqual(first, second)

    def test_raw_snapshot_hash_distinguishes_whitespace_canonical_hash_does_not(self):
        first = run_paper_scenario(parse_paper_scenario(encode(self.fixture)))
        second = run_paper_scenario(parse_paper_scenario(json.dumps(self.fixture, indent=2).encode()))
        self.assertNotEqual(first["scenario"]["input_sha256"], second["scenario"]["input_sha256"])
        self.assertEqual(first["scenario"]["canonical_sha256"], second["scenario"]["canonical_sha256"])
        self.assertEqual(first["execution_fingerprint"], second["execution_fingerprint"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_report_fingerprint_binds_the_entire_report_except_itself(self):
        report = run_paper_scenario(parse_paper_scenario(encode(self.fixture)))
        fingerprint = report.pop("fingerprint")
        canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(fingerprint, hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    def test_every_object_is_closed_and_requires_all_fields(self):
        for key in (None, "instrument", "policy", "quote", "intent"):
            for operation in ("extra", "missing"):
                with self.subTest(key=key, operation=operation):
                    fixture = copy.deepcopy(self.fixture)
                    target = fixture if key is None else fixture[key + "s"][0] if key in ("quote", "intent") else fixture[key]
                    if operation == "extra":
                        target["password"] = "forbidden-extra-field"
                    else:
                        del target[next(iter(target))]
                    with self.assertRaises(ValueError):
                        parse_paper_scenario(encode(fixture))

    def test_float_bool_exponent_nan_and_oversized_decimals_rejected(self):
        for value in (500, 500.0, True, "NaN", "Infinity", "1e3", "01", "1" * 17, "0." + "1" * 13):
            with self.subTest(value=value):
                self.fixture["policy"]["initial_balance"] = value
                with self.assertRaises(ValueError):
                    parse_paper_scenario(encode(self.fixture))

    def test_duplicate_keys_even_in_nested_object_rejected(self):
        raw = encode(self.fixture).replace(b'"symbol": "XAU_USD"', b'"symbol": "XAU_USD", "symbol": "XAU_USD"')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_paper_scenario(raw)

    def test_invalid_encoding_nonfinite_and_deep_json_rejected(self):
        for raw in (b"\xff", b'{"x": NaN}', b'1' * 10000, b"[" * 1500 + b"]" * 1500, b"", b" " * (MAX_BYTES + 1)):
            with self.subTest(raw_length=len(raw)):
                with self.assertRaises(ValueError):
                    parse_paper_scenario(raw)

    def test_only_synthetic_version_one_is_accepted(self):
        for key, value in (("data_basis", "historical"), ("data_basis", "live"), ("version", True), ("version", 2), ("schema", "other")):
            with self.subTest(key=key, value=value):
                fixture = copy.deepcopy(self.fixture)
                fixture[key] = value
                with self.assertRaises(ValueError):
                    parse_paper_scenario(encode(fixture))

    def test_time_requires_explicit_utc_and_valid_calendar_date(self):
        for value in ("2026-01-05T12:00:00", "2026-01-05T13:00:00+01:00", "2026-02-30T12:00:00Z", 1):
            with self.subTest(value=value):
                self.fixture["quotes"][0]["timestamp"] = value
                with self.assertRaises(ValueError):
                    parse_paper_scenario(encode(self.fixture))

    def test_integer_boolean_confusion_and_missing_kill_switch_rejected(self):
        for field, value in (("max_trades_per_day", True), ("max_quote_age_ms", 1.0), ("max_holding_seconds", 0), ("force_flat_at_end", 1), ("halt_at", False)):
            fixture = copy.deepcopy(self.fixture)
            fixture["policy"][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    parse_paper_scenario(encode(fixture))

    def test_empty_intents_are_valid_no_trade_scenario(self):
        self.fixture["intents"] = []
        report = run_paper_scenario(parse_paper_scenario(encode(self.fixture)))
        self.assertEqual(report["counts"]["trades"], 0)
        self.assertEqual(Decimal(report["final_balance"]), Decimal("500"))

    def test_order_grid_and_duplicate_intent_validation_are_enforced_before_simulation(self):
        changes = (
            lambda f: f["quotes"].reverse(),
            lambda f: f["quotes"][0].update(bid="2000.001"),
            lambda f: f["intents"][1].update(intent_id=f["intents"][0]["intent_id"]),
        )
        for change in changes:
            fixture = copy.deepcopy(self.fixture)
            change(fixture)
            with self.assertRaises(ValueError):
                run_paper_scenario(parse_paper_scenario(encode(fixture)))


class PaperExecutionCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scenario = self.root / "scenario.json"
        self.report = self.root / "report.json"

    def invoke(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(args)
        self.assertEqual(status, 0)
        return json.loads(output.getvalue())

    def assert_cli_error(self, *args):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(args)
        self.assertEqual(caught.exception.code, 2)

    def test_demo_writes_auditable_report_and_concise_summary(self):
        summary = self.invoke("demo-paper-execution", "--report-json", str(self.report))
        report = json.loads(self.report.read_text())
        self.assertEqual(summary["counts"], report["counts"])
        self.assertEqual(summary["counts"]["trades"], 2)
        self.assertFalse(summary["broker_connected"])
        self.assertNotIn("fills", summary)
        self.assertIn("fills", report)
        self.assertEqual(list(self.root.iterdir()), [self.report])

    def test_export_then_custom_run_reproduces_demo(self):
        self.invoke("export-paper-scenario", str(self.scenario))
        self.assertEqual(load_paper_scenario(self.scenario).name, synthetic_execution_scenario()["name"])
        before = self.scenario.read_bytes()
        summary = self.invoke("simulate-paper", str(self.scenario), "--report-json", str(self.report))
        self.assertEqual(self.scenario.read_bytes(), before)
        self.assertEqual(Decimal(summary["net_pnl"]), Decimal("3.04"))

    def test_existing_output_never_overwritten(self):
        self.report.write_bytes(b"keep-me")
        self.assert_cli_error("demo-paper-execution", "--report-json", str(self.report))
        self.assertEqual(self.report.read_bytes(), b"keep-me")
        self.assertEqual(list(self.root.iterdir()), [self.report])

    def test_export_refuses_existing_output(self):
        self.scenario.write_bytes(b"keep-me")
        self.assert_cli_error("export-paper-scenario", str(self.scenario))
        self.assertEqual(self.scenario.read_bytes(), b"keep-me")

    def test_input_and_symlink_alias_are_protected(self):
        self.invoke("export-paper-scenario", str(self.scenario))
        before = self.scenario.read_bytes()
        alias = self.root / "alias.json"
        alias.symlink_to(self.scenario)
        for output in (self.scenario, alias):
            self.assert_cli_error("simulate-paper", str(self.scenario), "--report-json", str(output))
        self.assertEqual(self.scenario.read_bytes(), before)

    def test_dangling_link_and_hardlink_are_not_replaced(self):
        self.report.symlink_to(self.root / "missing.json")
        self.assert_cli_error("demo-paper-execution", "--report-json", str(self.report))
        self.assertTrue(self.report.is_symlink())
        self.assertFalse((self.root / "missing.json").exists())
        self.invoke("export-paper-scenario", str(self.scenario))
        import os
        hardlink = self.root / "hardlink.json"
        os.link(str(self.scenario), str(hardlink))
        before = self.scenario.read_bytes()
        self.assert_cli_error("simulate-paper", str(self.scenario), "--report-json", str(hardlink))
        self.assertEqual(self.scenario.read_bytes(), before)

    def test_invalid_scenario_creates_no_report(self):
        self.scenario.write_bytes(b'{"schema":"bad"}')
        self.assert_cli_error("simulate-paper", str(self.scenario), "--report-json", str(self.report))
        self.assertFalse(self.report.exists())

    def test_publication_failure_cleans_staging_and_preserves_other_files(self):
        with patch("xau_trader.paper_execution_io.os.link", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                write_new_json(self.report, {"test": True})
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
