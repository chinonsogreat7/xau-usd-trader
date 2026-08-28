import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xau_trader.ledger import ExperimentLedger, ExperimentRecord


class LedgerTests(unittest.TestCase):
    def test_ledger_is_append_only_by_run_id(self):
        record = ExperimentRecord(
            run_id="run-1",
            strategy_key="baseline:v1",
            data_sha256="a" * 64,
            code_revision="working-tree",
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            config={"units": 1.0},
            metrics={"net_pnl": -2.0},
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = ExperimentLedger(Path(directory) / "experiments.jsonl")
            ledger.append(record)
            self.assertEqual(ledger.read_all()[0]["run_id"], "run-1")
            with self.assertRaisesRegex(ValueError, "already exists"):
                ledger.append(record)

    def test_record_rejects_non_finite_metrics(self):
        with self.assertRaises(ValueError):
            ExperimentRecord(
                run_id="run-1",
                strategy_key="baseline:v1",
                data_sha256="a" * 64,
                code_revision="working-tree",
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                config={"units": 1.0},
                metrics={"net_pnl": float("nan")},
            )

    def test_forward_evidence_must_start_after_knowledge_cutoff(self):
        cutoff = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "after the knowledge cutoff"):
            ExperimentRecord(
                run_id="run-1",
                strategy_key="candidate:v1",
                data_sha256="a" * 64,
                code_revision="working-tree",
                started_at=cutoff + timedelta(days=2),
                config={},
                metrics={},
                run_purpose="forward_evidence",
                knowledge_cutoff=cutoff,
                data_start=cutoff,
                data_end=cutoff + timedelta(days=1),
            )


if __name__ == "__main__":
    unittest.main()
