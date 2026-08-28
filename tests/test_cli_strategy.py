import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from strategy_spec_fixture import valid_strategy_spec
from xau_trader.cli import main


class StrategyCliTests(unittest.TestCase):
    def _document_path(self, document):
        temporary = tempfile.TemporaryDirectory()
        path = Path(temporary.name) / "strategy.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        self.addCleanup(temporary.cleanup)
        return path

    def test_validate_strategy_reports_hash_and_readiness(self):
        path = self._document_path(valid_strategy_spec())
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(("validate-strategy", str(path), "--promotable"))
        payload = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertEqual(payload["status"], "valid_strategy_spec_v1")
        self.assertTrue(payload["compilation_ready"])
        self.assertTrue(payload["promotion_ready"])
        self.assertEqual(len(payload["spec_hash"]), 64)

    def test_compile_strategy_emits_diagnostic_plan_metadata_only(self):
        path = self._document_path(valid_strategy_spec())
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(("compile-strategy", str(path)))
        payload = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertTrue(payload["diagnostic_only"])
        self.assertEqual(payload["strategy_id"], "xau-sma-cross-atr")
        self.assertEqual(len(payload["plan_hash"]), 64)
        self.assertNotIn("canonical_plan_json", payload)


if __name__ == "__main__":
    unittest.main()
