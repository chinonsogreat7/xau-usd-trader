"""Loss-limit configuration stays explicit across JSON, strategy, and CLI."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest

from xau_trader.cli import main
from xau_trader.paper_execution import PaperLossLimits
from xau_trader.paper_execution_io import (
    parse_paper_scenario, run_paper_scenario, synthetic_execution_scenario,
)
from xau_trader.strategy_paper import run_strategy_paper_replay
from strategy_paper_fixture import (
    STRUCTURAL_POLICY, coherent_quotes, execution_policy, instrument, replay, rounded_bars,
)


def limits_scenario():
    value = synthetic_execution_scenario()
    value.update(version=2, loss_limits=dict(max_losses_per_day=2, max_weekly_drawdown_fraction="0.03"))
    return value


def parse(value):
    return parse_paper_scenario(json.dumps(value).encode("utf-8"))


class LossLimitsScenarioTests(unittest.TestCase):
    def test_v1_stays_explicitly_disabled_and_v2_enables_limits(self):
        legacy = parse(synthetic_execution_scenario())
        self.assertIsNone(legacy.loss_limits)
        self.assertFalse(run_paper_scenario(legacy)["loss_limits"]["enabled"])
        scenario = parse(limits_scenario())
        self.assertEqual(scenario.loss_limits, PaperLossLimits(2, Decimal("0.03")))
        report = run_paper_scenario(scenario)
        self.assertEqual(report["loss_limits"], scenario.loss_limits.as_dict())
        self.assertEqual(report["losses_today"], 1)
        self.assertFalse(report["broker_connected"])
        self.assertFalse(report["promotion_eligible"])

    def test_v2_requires_complete_closed_non_null_configuration(self):
        invalid = (None, {}, {"max_losses_per_day": 2},
                   {"max_weekly_drawdown_fraction": "0.03"},
                   dict(max_losses_per_day=2, max_weekly_drawdown_fraction="0.03", timezone="local"))
        for value in invalid:
            with self.subTest(value=value):
                scenario = limits_scenario()
                scenario["loss_limits"] = value
                with self.assertRaises(ValueError):
                    parse(scenario)
        scenario = limits_scenario()
        del scenario["loss_limits"]
        with self.assertRaises(ValueError):
            parse(scenario)

    def test_v1_does_not_silently_accept_or_ignore_v2_fields(self):
        value = limits_scenario()
        value["version"] = 1
        with self.assertRaises(ValueError):
            parse(value)

    def test_limit_numbers_reject_boolean_float_string_count_and_bad_fractions(self):
        for field, values in (
            ("max_losses_per_day", (True, False, "2", 2.0, 0, -1)),
            ("max_weekly_drawdown_fraction", (True, 0.03, "NaN", "Infinity", "1", "0", "-0.1", "3e-2")),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    scenario = limits_scenario()
                    scenario["loss_limits"][field] = value
                    with self.assertRaises(ValueError):
                        parse(scenario)

    def test_limits_bind_kernel_and_full_report_hashes(self):
        first = run_paper_scenario(parse(limits_scenario()))
        value = limits_scenario()
        value["loss_limits"]["max_losses_per_day"] = 3
        second = run_paper_scenario(parse(value))
        self.assertNotEqual(first["input_fingerprint"], second["input_fingerprint"])
        self.assertNotEqual(first["execution_fingerprint"], second["execution_fingerprint"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_python_api_cannot_disable_or_change_embedded_limits(self):
        scenario = parse(limits_scenario())
        for changed in (replace(scenario, loss_limits=None),
                        replace(scenario, loss_limits=PaperLossLimits(3, Decimal("0.03"))),
                        replace(scenario, intents=()),
                        replace(scenario, input_sha256="not-a-hash")):
            with self.assertRaises(ValueError):
                run_paper_scenario(changed)

    def test_v2_still_rejects_real_data_and_unknown_versions(self):
        for change in (dict(data_basis="historical"), dict(data_basis="live"), dict(version=3)):
            with self.assertRaises(ValueError):
                parse(dict(limits_scenario(), **change))


class LossLimitsCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scenario = self.root / "scenario.json"

    def call(self, args):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(args), 0)
        return json.loads(stdout.getvalue())

    def test_explicit_export_and_simulate_carry_limits(self):
        result = self.call(("export-paper-scenario", str(self.scenario),
                            "--max-losses-per-day", "2", "--max-weekly-drawdown-fraction", "0.03"))
        self.assertTrue(result["loss_limits_configured"])
        value = json.loads(self.scenario.read_text())
        self.assertEqual(value["version"], 2)
        report = self.root / "report.json"
        summary = self.call(("simulate-paper", str(self.scenario), "--report-json", str(report)))
        self.assertTrue(summary["loss_limits"]["enabled"])
        self.assertEqual(summary["losses_today"], 1)

    def test_partial_or_invalid_export_options_create_nothing(self):
        for extra in (("--max-losses-per-day", "2"), ("--max-weekly-drawdown-fraction", "0.03"),
                      ("--max-losses-per-day", "0", "--max-weekly-drawdown-fraction", "0.03"),
                      ("--max-losses-per-day", "2", "--max-weekly-drawdown-fraction", "1"),
                      ("--max-losses-per-day", "2", "--max-weekly-drawdown-fraction", "1e-1000000")):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    main(("export-paper-scenario", str(self.scenario)) + extra)
                self.assertEqual(raised.exception.code, 2)
                self.assertFalse(self.scenario.exists())

    def test_explicit_guarded_export_cannot_overwrite_file(self):
        self.scenario.write_bytes(b"keep")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(("export-paper-scenario", str(self.scenario), "--max-losses-per-day", "2",
                      "--max-weekly-drawdown-fraction", "0.03"))
        self.assertEqual(self.scenario.read_bytes(), b"keep")


class StrategyLossLimitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = rounded_bars()
        cls.replay = replay(cls.bars)
        cls.quotes = coherent_quotes(cls.bars)

    def run_case(self, limits):
        return run_strategy_paper_replay(self.replay, quotes=self.quotes, instrument=instrument(),
            execution_policy=execution_policy(), structural_policy=STRUCTURAL_POLICY,
            loss_limits=limits).as_dict()

    def test_strategy_propagates_explicit_limits_without_claiming_live_readiness(self):
        limits = PaperLossLimits(2, Decimal("0.03"))
        report = self.run_case(limits)
        self.assertTrue(report["loss_limits_configured"])
        self.assertEqual(report["loss_limits"], limits.as_dict())
        self.assertEqual(report["execution"]["loss_limits"], limits.as_dict())
        self.assertEqual(report["counts"]["generated_intents"], 1)
        self.assertEqual(report["decisions"][221]["execution_reason"], "minimum_reward_risk")
        self.assertFalse(report["source_risk_policy_complete"])
        self.assertFalse(report["promotion_eligible"])
        self.assertFalse(report["broker_connected"])
        disabled = self.run_case(None)
        self.assertFalse(disabled["loss_limits_configured"])
        self.assertNotEqual(disabled["input_fingerprint"], report["input_fingerprint"])

    def test_strategy_rejects_unvalidated_configuration(self):
        with self.assertRaisesRegex(ValueError, "loss_limits"):
            self.run_case(dict(max_losses_per_day=2, max_weekly_drawdown_fraction="0.03"))


if __name__ == "__main__":
    unittest.main()
