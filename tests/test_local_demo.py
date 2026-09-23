import hashlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xau_trader.cli import main
from xau_trader.local_demo import run_local_paper_demo
from xau_trader.paper_execution_io import load_paper_scenario, run_paper_scenario


class LocalDemoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "reports with spaces"

    def test_creates_three_readable_artifacts_and_exact_reproducible_snapshot(self):
        directory, summary = run_local_paper_demo(self.root)
        self.assertEqual(directory.parent, self.root.resolve())
        self.assertEqual({path.name for path in directory.iterdir()}, {"summary.txt", "report.json", "scenario.json"})
        report = json.loads((directory / "report.json").read_text())
        self.assertEqual(report, run_paper_scenario(load_paper_scenario(directory / "scenario.json")))
        self.assertEqual(report["scenario"]["input_sha256"], hashlib.sha256((directory / "scenario.json").read_bytes()).hexdigest())
        self.assertEqual(summary, (directory / "summary.txt").read_text())
        self.assertIn("Closed trades: 2", summary)
        self.assertIn("Broker connection: NONE", summary)
        self.assertIn("NOT your Exness balance", summary)
        self.assertIn("No process was left running", summary)

    def test_repeat_uses_unique_directories_and_never_changes_previous_run(self):
        first, _ = run_local_paper_demo(self.root)
        snapshot = {path.name: path.read_bytes() for path in first.iterdir()}
        second, _ = run_local_paper_demo(self.root)
        self.assertNotEqual(first, second)
        self.assertEqual(snapshot, {path.name: path.read_bytes() for path in first.iterdir()})
        self.assertEqual((first / "report.json").read_bytes(), (second / "report.json").read_bytes())

    def test_cli_prints_summary_and_uses_explicit_output_root(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(("run-paper-demo", "--output-root", str(self.root)))
        self.assertEqual(status, 0)
        self.assertIn("OFFLINE PAPER DEMO", output.getvalue())
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_failed_simulation_creates_no_output_folder(self):
        with patch("xau_trader.local_demo.run_paper_scenario", side_effect=ValueError("invalid test scenario")):
            with self.assertRaises(ValueError):
                run_local_paper_demo(self.root)
        self.assertFalse(self.root.exists())

    def test_partial_publication_error_identifies_preserved_directory(self):
        with patch("xau_trader.local_demo.write_new_json", side_effect=OSError("disk test")):
            with self.assertRaisesRegex(OSError, "Partial files may be in"):
                run_local_paper_demo(self.root)

    def test_launcher_rejects_unexpected_broker_or_real_data_result(self):
        for result in (
            {"broker_connected": True},
            {"broker_connected": False, "real_orders_submitted": 1},
            {"broker_connected": False, "real_orders_submitted": False},
            {"broker_connected": False, "real_orders_submitted": 0, "scenario": {"data_basis": "real"}},
        ):
            with self.subTest(result=result):
                with patch("xau_trader.local_demo.run_paper_scenario", return_value=result):
                    with self.assertRaisesRegex(ValueError, "disconnected synthetic"):
                        run_local_paper_demo(self.root)
                self.assertFalse(self.root.exists())

    def test_existing_file_as_output_root_is_a_cli_error_and_not_replaced(self):
        self.root.write_bytes(b"keep")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(("run-paper-demo", "--output-root", str(self.root)))
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.root.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
